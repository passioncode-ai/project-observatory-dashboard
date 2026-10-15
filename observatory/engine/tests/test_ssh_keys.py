#!/usr/bin/env python3
"""The SSH-key door (OSS-62–67, docs/design/SSH-KEYS.md) against fakes in a temporary HOME.

Every case runs `tools/ssh_keys.py` in a subprocess with HOME pointing at a scratch folder,
so the real `~/.ssh` is never read. `ssh-keygen`, `ssh-add`, `ssh`, `gh` and the clipboard
are fakes that record their argv; one case uses the system's real `ssh-keygen`, still inside
the scratch HOME. The private keys planted here carry a marker after their header, so every
output a person or an agent can see — stdout, stderr, the saved inventory, the clipboard, the
fakes' argv, the receipts journal and the MCP answer — is searched for it.

Private-key header lines are composed at run time: the commit gate refuses a literal one in
the engine's tracked files (tools/check_secrets.py).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
DOOR = ROOT / "tools" / "ssh_keys.py"
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))

BEGIN = "-----BEGIN "
END = "-----END "
OPENSSH = "OPENSSH " + "PRIVATE" + " KEY-----"
#: The bytes no output may ever carry. Composed, so no literal in this file is key-shaped.
MARKER = b"PRIVATEMATERIAL" + b"-zq7" * 8


def _string(b: bytes) -> bytes:
    return len(b).to_bytes(4, "big") + b


def _mpint(n: int) -> bytes:
    raw = n.to_bytes((n.bit_length() + 8) // 8, "big")
    return _string(raw)


def ed25519_blob(seed: str) -> bytes:
    return _string(b"ssh-ed25519") + _string(hashlib.sha256(seed.encode()).digest())


def rsa_blob(bits: int, seed: str) -> bytes:
    n = int.from_bytes(hashlib.sha512(seed.encode()).digest() * (bits // 512 + 1), "big")
    n = (n >> (n.bit_length() - bits)) | (1 << (bits - 1)) | 1
    return _string(b"ssh-rsa") + _mpint(65537) + _mpint(n)


def dss_blob(seed: str) -> bytes:
    p = (int.from_bytes(hashlib.sha512(seed.encode()).digest() * 2, "big") | (1 << 1023)) & ((1 << 1024) - 1)
    return _string(b"ssh-dss") + _mpint(p) + _mpint(7) + _mpint(5) + _mpint(3)


def fingerprint(blob: bytes) -> str:
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def pub_line(blob: bytes, comment: str = "") -> str:
    ktype = blob[4:4 + int.from_bytes(blob[:4], "big")].decode()
    return f"{ktype} {base64.b64encode(blob).decode()}" + (f" {comment}" if comment else "")


def private_text(cipher: str = "none", marker: bytes = MARKER) -> str:
    kdf = b"none" if cipher == "none" else b"bcrypt"
    body = (b"openssh-key-v1\0" + _string(cipher.encode()) + _string(kdf) + _string(b"")
            + (1).to_bytes(4, "big") + _string(b"public-part") + _string(marker * 4))
    encoded = base64.b64encode(body).decode()
    lines = [encoded[i:i + 70] for i in range(0, len(encoded), 70)]
    return BEGIN + OPENSSH + "\n" + "\n".join(lines) + "\n" + END + OPENSSH + "\n"


def private_body_lines(text: str) -> list[str]:
    """The base64 lines after the first one: none of them may appear anywhere."""
    lines = [l for l in text.splitlines() if l and not l.startswith("-----")]
    return lines[1:]


# ── the fakes ──────────────────────────────────────────────────────────────

FAKE_KEYGEN = r'''
import base64, hashlib, json, os, pathlib, sys
log = pathlib.Path(os.environ["FAKE_LOG"]); a = sys.argv[1:]
with log.open("a") as f: f.write(json.dumps({"tool": "ssh-keygen", "argv": a, "tty": os.isatty(0)}) + "\n")
def s(b): return len(b).to_bytes(4, "big") + b
def fields(blob):
    out = []
    while blob:
        n = int.from_bytes(blob[:4], "big"); out.append(blob[4:4 + n]); blob = blob[4 + n:]
    return out
if "-l" in a:
    path = pathlib.Path(a[a.index("-f") + 1])
    t, b64, *rest = path.read_text().split(None, 2)
    blob = base64.b64decode(b64); f = fields(blob)
    bits = {"ssh-ed25519": 256}.get(t)
    if t == "ssh-rsa": bits = int.from_bytes(f[2], "big").bit_length()
    if t == "ssh-dss": bits = int.from_bytes(f[1], "big").bit_length()
    fp = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    name = {"ssh-ed25519": "ED25519", "ssh-rsa": "RSA", "ssh-dss": "DSA"}[t]
    print(f"{bits} {fp} {(rest[0].strip() if rest else '') or 'no comment'} ({name})")
    sys.exit(0)
path = pathlib.Path(a[a.index("-f") + 1]); comment = a[a.index("-C") + 1]
if os.environ.get("FAKE_KEYGEN_FAIL"):
    print("ssh-keygen: generation failed", file=sys.stderr); sys.exit(1)
race = os.environ.get("FAKE_RACE_DEST")
if race:
    pathlib.Path(race).write_text("another writer's key\n")
cipher = b"none" if ("-N" in a and a[a.index("-N") + 1] == "") else b"aes256-ctr"
marker = os.environ["FAKE_MARKER"].encode()
body = b"openssh-key-v1\0" + s(cipher) + s(b"none" if cipher == b"none" else b"bcrypt") + s(b"") + (1).to_bytes(4, "big") + s(marker * 4)
enc = base64.b64encode(body).decode()
head = "-----BEGIN " + "OPENSSH PRIVATE" + " KEY-----"
path.write_text(head + "\n" + "\n".join(enc[i:i + 70] for i in range(0, len(enc), 70)) + "\n")
blob = s(b"ssh-ed25519") + s(hashlib.sha256(str(path).encode()).digest())
pathlib.Path(str(path) + ".pub").write_text("ssh-ed25519 " + base64.b64encode(blob).decode() + " " + comment + "\n")
'''

FAKE_AGENT = r'''
import json, os, pathlib, sys
log = pathlib.Path(os.environ["FAKE_LOG"]); a = sys.argv[1:]
with log.open("a") as f: f.write(json.dumps({"tool": "ssh-add", "argv": a}) + "\n")
state = os.environ.get("FAKE_AGENT", "")
if "--apple-use-keychain" in a:
    sys.exit(1 if os.environ.get("FAKE_KEYCHAIN_FAIL") else 0)
if state == "down":
    print("Could not open a connection to your authentication agent.", file=sys.stderr); sys.exit(2)
lines = [l for l in state.split("|") if l]
if not lines:
    print("The agent has no identities."); sys.exit(1)
print("\n".join(lines))
'''

FAKE_GH = r'''
import json, os, pathlib, sys
log = pathlib.Path(os.environ["FAKE_LOG"]); a = sys.argv[1:]
with log.open("a") as f: f.write(json.dumps({"tool": "gh", "argv": a}) + "\n")
st = pathlib.Path(os.environ["FAKE_GH_STATE"]); s = json.loads(st.read_text())
if a[:1] == ["api"]:
    endpoint = a[-1].split("?")[0]
    use = "signing" if endpoint == "user/ssh_signing_keys" else "authentication"
    if s.get("list_fail", {}).get(use):
        print(s["list_fail"][use], file=sys.stderr); sys.exit(1)
    print(json.dumps(s[use])); sys.exit(0)
if a[:2] == ["ssh-key", "add"]:
    if s.get("add_fail"):
        print(s["add_fail"], file=sys.stderr); sys.exit(1)
    use = a[a.index("--type") + 1]; title = a[a.index("--title") + 1]
    key = " ".join(pathlib.Path(a[2]).read_text().split()[:2])
    s[use].append({"id": 100 + len(s[use]), "title": title, "key": key, "created_at": "2026-10-15T00:00:00Z"})
    st.write_text(json.dumps(s)); print("Public key added to your account"); sys.exit(0)
print("unknown command", file=sys.stderr); sys.exit(2)
'''

FAKE_SSH = r'''
import json, os, pathlib, subprocess, sys
log = pathlib.Path(os.environ["FAKE_LOG"]); a = sys.argv[1:]
with log.open("a") as f: f.write(json.dumps({"tool": "ssh", "argv": a}) + "\n")
opts = []; identity = None; i = 0
while i < len(a) and a[i] != "--":
    if a[i] == "-o": opts.append(a[i + 1]); i += 2; continue
    if a[i] == "-i": identity = a[i + 1]; i += 2; continue
    i += 1
host, command = a[i + 1], a[i + 2:]
remote = pathlib.Path(os.environ["FAKE_REMOTE_HOME"])
if os.environ.get("FAKE_SSH_PASSWORD_ONLY") and "BatchMode=yes" in opts:
    print(f"{host}: Permission denied (publickey,password).", file=sys.stderr); sys.exit(255)
if os.environ.get("FAKE_SSH_UNREACHABLE"):
    print(f"ssh: connect to host {host} port 22: Connection refused", file=sys.stderr); sys.exit(255)
if command == ["true"]:
    if os.environ.get("FAKE_SSH_VERIFY_FAIL"):
        print(f"{host}: Permission denied (publickey).", file=sys.stderr); sys.exit(255)
    blob = pathlib.Path(identity + ".pub").read_text().split()[1]
    ak = remote / ".ssh" / "authorized_keys"
    ok = ak.is_file() and any(blob in line.split() for line in ak.read_text().splitlines())
    if not ok:
        print(f"{host}: Permission denied (publickey).", file=sys.stderr)
    sys.exit(0 if ok else 255)
env = dict(os.environ, HOME=str(remote))
p = subprocess.run(["/bin/sh", "-c", " ".join(command)], env=env, stdin=sys.stdin)
sys.exit(p.returncode)
'''

FAKE_CLIP = r'''
import json, os, pathlib, sys
log = pathlib.Path(os.environ["FAKE_LOG"])
with log.open("a") as f: f.write(json.dumps({"tool": "clipboard", "argv": sys.argv[1:]}) + "\n")
pathlib.Path(os.environ["FAKE_CLIP"]).write_bytes(sys.stdin.buffer.read())
'''


class Door(unittest.TestCase):
    """A scratch HOME, a scratch workspace and the fakes, per test."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="observatory-ssh-")
        self.root = pathlib.Path(self.temp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.ssh = self.home / ".ssh"
        self.remote = self.root / "remote-home"
        self.remote.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "fake-argv.jsonl"
        self.clip = self.root / "clipboard"
        self.gh_state = self.root / "gh.json"
        self.gh_state.write_text(json.dumps({"authentication": [], "signing": []}))
        self.vault = self.root / "vault"
        self.scratch = self.root / "scratch"
        self.scratch.mkdir()
        fakes = {"ssh-keygen": FAKE_KEYGEN, "ssh-add": FAKE_AGENT, "gh": FAKE_GH, "ssh": FAKE_SSH,
                 "clip": FAKE_CLIP}
        for name, body in fakes.items():
            path = self.bin / name
            path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
            path.chmod(0o755)
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("OBSERVATORY_", "SSH_AUTH_SOCK", "GH_", "GITHUB_"))}
        self.env.update({
            "HOME": str(self.home), "USER": "tester",
            "OBSERVATORY_HOME": str(self.root / "workspace"),
            "OBSERVATORY_SCRATCH": str(self.scratch), "OBSERVATORY_STATE": str(self.root / "state"),
            "OBSERVATORY_VAULT_DIR": str(self.vault),
            "OBSERVATORY_SSH_KEYGEN": str(self.bin / "ssh-keygen"),
            "OBSERVATORY_SSH_ADD": str(self.bin / "ssh-add"),
            "OBSERVATORY_SSH": str(self.bin / "ssh"),
            "OBSERVATORY_GH": str(self.bin / "gh"),
            "OBSERVATORY_CLIPBOARD": str(self.bin / "clip"),
            "FAKE_LOG": str(self.log), "FAKE_CLIP": str(self.clip), "FAKE_GH_STATE": str(self.gh_state),
            "FAKE_REMOTE_HOME": str(self.remote), "FAKE_MARKER": MARKER.decode(),
            "FAKE_AGENT": "",
        })

    def tearDown(self):
        self.temp.cleanup()

    # ── helpers ──────────────────────────────────────────────────────────

    def door(self, *args, env=None, stdin=subprocess.DEVNULL):
        return subprocess.run([sys.executable, str(DOOR), *args], capture_output=True, text=True,
                              env={**self.env, **(env or {})}, stdin=stdin, timeout=120)

    def plant(self, name: str, *, blob: bytes | None = None, cipher: str = "none",
              comment: str = "", private: bool = True, public: bool = True, mode: int = 0o600) -> bytes:
        self.ssh.mkdir(mode=0o700, exist_ok=True)
        blob = blob or ed25519_blob(name)
        if private:
            (self.ssh / name).write_text(private_text(cipher))
            (self.ssh / name).chmod(mode)
        if public:
            (self.ssh / f"{name}.pub").write_text(pub_line(blob, comment or name) + "\n")
            (self.ssh / f"{name}.pub").chmod(0o644)
        return blob

    def listing(self, *args, env=None) -> dict:
        p = self.door("list", "--json", *args, env=env)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def key(self, doc: dict, name: str) -> dict:
        return next(k for k in doc["keys"] if k["name"] == name)

    def calls(self, tool: str) -> list[list[str]]:
        if not self.log.is_file():
            return []
        rows = [json.loads(l) for l in self.log.read_text().splitlines() if l]
        return [r["argv"] for r in rows if r["tool"] == tool]

    def receipts(self) -> list[dict]:
        path = self.vault / "movements.jsonl"
        return [json.loads(l) for l in path.read_text().splitlines() if l] if path.is_file() else []


class Inventory(Door):
    def test_empty_home_names_the_next_step(self):
        p = self.door("list")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("No SSH keys yet", p.stdout)
        self.assertIn("ssh-keys create", p.stdout)

    def test_pairs_are_listed_with_fingerprint_type_comment_and_passphrase(self):
        alpha = self.plant("id_alpha", cipher="aes256-ctr", comment="alpha key")
        self.plant("id_beta", blob=rsa_blob(2048, "beta"))
        doc = self.listing("--no-github")
        a, b = self.key(doc, "id_alpha"), self.key(doc, "id_beta")
        self.assertEqual((a["type"], a["bits"], a["fingerprint"], a["comment"], a["passphrase"]),
                         ("ED25519", 256, fingerprint(alpha), "alpha key", "yes"))
        self.assertEqual((b["type"], b["bits"], b["passphrase"]), ("RSA", 2048, "no"))
        self.assertEqual((a["path"], a["private_mode"], a["public_mode"]), ("~/.ssh/id_alpha", "0600", "0644"))
        self.assertTrue(all("-l" in c for c in self.calls("ssh-keygen")), "ssh-keygen ran for anything but -l")
        self.assertTrue(all(c[-1].endswith(".pub") for c in self.calls("ssh-keygen")),
                        "ssh-keygen was given a private key")

    def test_private_without_public_and_public_without_private(self):
        self.plant("id_lonely", public=False)
        self.plant("id_token", private=False)
        doc = self.listing("--no-github")
        lonely, token = self.key(doc, "id_lonely"), self.key(doc, "id_token")
        self.assertIsNone(lonely["fingerprint"])
        self.assertTrue(lonely["private"] and not lonely["public"])
        self.assertIn("ssh-keygen -y", lonely["hint"])
        self.assertTrue(token["public"] and not token["private"])
        self.assertIsNotNone(token["fingerprint"])

    def test_config_hosts_includes_and_missing_identity_files(self):
        self.plant("id_alpha")
        (self.ssh / "config.d").mkdir()
        (self.ssh / "config").write_text(
            "Include config.d/*\n\nHost alpha alpha-backup\n  HostName alpha.example.invalid\n"
            "  IdentityFile ~/.ssh/id_alpha\n")
        (self.ssh / "config.d" / "beta").write_text(
            "Host beta\n  IdentityFile=%d/.ssh/id_missing\n")
        doc = self.listing("--no-github")
        self.assertEqual(self.key(doc, "id_alpha")["hosts"], ["alpha", "alpha-backup"])
        missing = [f for f in doc["identity_files"] if not f["exists"]]
        self.assertEqual([(f["host"], f["path"]) for f in missing], [("beta", "~/.ssh/id_missing")])

    def test_agent_keys_loaded_and_agent_only(self):
        alpha = self.plant("id_alpha")
        loaded = f"256 {fingerprint(alpha)} id_alpha (ED25519)|256 {fingerprint(ed25519_blob('x'))} card (ED25519)"
        doc = self.listing("--no-github", env={"FAKE_AGENT": loaded})
        self.assertTrue(self.key(doc, "id_alpha")["in_agent"])
        self.assertEqual([k["fingerprint"] for k in doc["agent_only"]], [fingerprint(ed25519_blob("x"))])

    def test_agent_not_running_is_degraded_not_empty(self):
        self.plant("id_alpha")
        doc = self.listing("--no-github", env={"FAKE_AGENT": "down"})
        self.assertEqual(len(doc["keys"]), 1)
        self.assertTrue(any(d["source"] == "ssh-agent" for d in doc["degraded"]), doc["degraded"])
        self.assertIsNone(self.key(doc, "id_alpha")["in_agent"])

    def test_github_registration_by_fingerprint_and_orphans(self):
        alpha = self.plant("id_alpha")
        orphan = ed25519_blob("elsewhere")
        self.gh_state.write_text(json.dumps({
            "authentication": [{"id": 1, "title": "alpha", "key": pub_line(alpha), "created_at": "2026-01-01T00:00:00Z"},
                               {"id": 2, "title": "old laptop", "key": pub_line(orphan), "created_at": "2025-01-01T00:00:00Z"}],
            "signing": [{"id": 3, "title": "alpha", "key": pub_line(alpha), "created_at": "2026-01-01T00:00:00Z"}]}))
        doc = self.listing()
        self.assertEqual(self.key(doc, "id_alpha")["github"], ["authentication", "signing"])
        orphans = [k for k in doc["github"]["keys"] if not k["on_machine"]]
        self.assertEqual([(k["id"], k["fingerprint"]) for k in orphans], [(2, fingerprint(orphan))])
        self.assertEqual(doc["degraded"], [])

    def test_github_unreadable_is_degraded_with_scope_command(self):
        self.plant("id_alpha")
        self.gh_state.write_text(json.dumps({"authentication": [], "signing": [],
                                             "list_fail": {"signing": "gh: Not Found (HTTP 404)"}}))
        doc = self.listing()
        self.assertIsNone(self.key(doc, "id_alpha")["github"], "unread GitHub must not read as unregistered")
        reasons = " ".join(d["reason"] + d.get("fix", "") for d in doc["degraded"])
        self.assertIn("read:ssh_signing_key", reasons)

    def test_gh_missing_is_degraded(self):
        self.plant("id_alpha")
        doc = self.listing(env={"OBSERVATORY_GH": str(self.root / "no-such-gh")})
        self.assertTrue(any(d["source"].startswith("github") for d in doc["degraded"]))

    def test_a_link_is_reported_not_followed(self):
        self.plant("id_alpha")
        outside = self.root / "outside-key"
        outside.write_text(private_text())
        (self.ssh / "id_linked").symlink_to(outside)
        doc = self.listing("--no-github")
        linked = self.key(doc, "id_linked")
        self.assertTrue(linked["link"])
        self.assertEqual(linked["passphrase"], "unknown", "a link was followed to read the key")

    def test_access_time_is_put_back_after_the_header_read(self):
        self.plant("id_alpha")
        old = time.time() - 400 * 86400
        os.utime(self.ssh / "id_alpha", (old, old))
        self.listing("--no-github")
        self.assertAlmostEqual(os.stat(self.ssh / "id_alpha").st_atime, old, delta=2)

    def test_list_saves_an_owner_only_inventory_without_the_home_path(self):
        self.plant("id_alpha")
        self.assertEqual(self.door("list", "--no-github").returncode, 0)
        saved = self.scratch / "ssh-keys.json"
        self.assertEqual(stat.S_IMODE(saved.stat().st_mode), 0o600)
        text = saved.read_text()
        self.assertNotIn(str(self.home), text)
        self.assertEqual(json.loads(text)["schema"], "ssh-keys/1")

    @unittest.skipUnless(shutil.which("ssh-keygen"), "the system's ssh-keygen is not installed")
    def test_the_real_ssh_keygen_agrees(self):
        """The fakes model the real tool; this case holds them to it, inside the scratch HOME."""
        self.ssh.mkdir(mode=0o700)
        real = shutil.which("ssh-keygen")
        for name, phrase in (("id_open", ""), ("id_locked", "a test passphrase")):
            subprocess.run([real, "-q", "-t", "ed25519", "-N", phrase, "-C", name, "-f", str(self.ssh / name)],
                           check=True, capture_output=True, timeout=60)
        doc = self.listing("--no-github", env={"OBSERVATORY_SSH_KEYGEN": real})
        expected = subprocess.run([real, "-l", "-E", "sha256", "-f", str(self.ssh / "id_open.pub")],
                                  capture_output=True, text=True, check=True).stdout.split()[1]
        self.assertEqual(self.key(doc, "id_open")["fingerprint"], expected)
        self.assertEqual((self.key(doc, "id_open")["passphrase"], self.key(doc, "id_locked")["passphrase"]),
                         ("no", "yes"))
        body = private_body_lines((self.ssh / "id_open").read_text())
        out = json.dumps(doc)
        self.assertFalse(any(line in out for line in body))


class PrivateBytesNeverLeave(Door):
    def everything_seen(self) -> str:
        parts = []
        for path in (self.scratch / "ssh-keys.json", self.clip, self.log, self.vault / "movements.jsonl"):
            if path.is_file():
                parts.append(path.read_text(errors="replace"))
        return "\n".join(parts)

    def assert_clean(self, text: str):
        self.assertNotIn(MARKER.decode(), text)
        self.assertNotIn(base64.b64encode(MARKER * 4).decode()[:40], text)
        for line in private_body_lines(private_text()):
            self.assertNotIn(line, text)

    def test_no_private_byte_in_any_output(self):
        self.plant("id_alpha", comment="alpha")
        (self.ssh / "config").write_text("Host alpha\n  IdentityFile ~/.ssh/id_alpha\n")
        outputs = []
        for args in (("list",), ("list", "--json"), ("show", "id_alpha"), ("copy", "id_alpha"),
                     ("send", "id_alpha", "--to", "github", "--as", "both"),
                     ("send", "id_alpha", "--to", "server:alpha", "--verify"), ("findings",),
                     ("findings", "--json"), ("create", "id_new", "--no-passphrase")):
            p = self.door(*args)
            outputs += [p.stdout, p.stderr]
        import ssh_keys
        old = dict(os.environ)
        try:
            os.environ.update(self.env)
            outputs.append(json.dumps(ssh_keys.agent_view()))
            outputs.append(json.dumps(ssh_keys.agent_view(name="id_alpha")))
        finally:
            os.environ.clear()
            os.environ.update(old)
        self.assert_clean("\n".join(outputs) + self.everything_seen())
        # The fakes' argv: no private byte, and ssh-keygen never pointed at a private file to read.
        for argv in self.calls("ssh-keygen"):
            if "-l" in argv:
                self.assertTrue(argv[-1].endswith(".pub"), argv)

    def test_a_private_key_saved_as_pub_is_refused_and_not_echoed(self):
        self.ssh.mkdir(mode=0o700)
        (self.ssh / "id_wrong.pub").write_text(private_text())
        for args in (("copy", "id_wrong"), ("show", "id_wrong"), ("send", "id_wrong", "--to", "github")):
            p = self.door(*args)
            self.assertEqual(p.returncode, 2, args)
            self.assertIn("not one public key line", p.stderr)
            self.assert_clean(p.stdout + p.stderr)
        self.assertFalse(self.clip.exists(), "something reached the clipboard")
        self.assertEqual(self.calls("gh"), [])


class Create(Door):
    def test_creates_ed25519_with_modes_and_an_empty_passphrase_only_when_asked(self):
        p = self.door("create", "id_new", "--no-passphrase", "--comment", "new key")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(stat.S_IMODE(self.ssh.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.ssh / "id_new").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.ssh / "id_new.pub").stat().st_mode), 0o644)
        argv = self.calls("ssh-keygen")[0]
        self.assertEqual(argv[argv.index("-t") + 1], "ed25519")
        self.assertEqual(argv[argv.index("-N") + 1], "")
        self.assertEqual(argv[argv.index("-C") + 1], "new key")
        self.assertIn("SHA256:", p.stdout)
        self.assertIn("ssh-keys copy id_new", p.stdout)
        self.assertEqual([r["event"] for r in self.receipts()], ["ssh.created"])
        leftovers = [x.name for x in self.ssh.iterdir() if x.name not in ("id_new", "id_new.pub")]
        self.assertEqual(leftovers, [], "the temporary folder stayed behind")

    def test_never_overwrites_an_existing_key(self):
        self.plant("id_taken")
        before = (self.ssh / "id_taken").read_bytes()
        p = self.door("create", "id_taken", "--no-passphrase")
        self.assertEqual(p.returncode, 2)
        self.assertEqual((self.ssh / "id_taken").read_bytes(), before)
        self.assertEqual(self.calls("ssh-keygen"), [], "ssh-keygen ran although the name was taken")
        (self.ssh / "id_halfpub.pub").write_text("x\n")
        self.assertEqual(self.door("create", "id_halfpub", "--no-passphrase").returncode, 2)
        (self.ssh / "id_dangling").symlink_to(self.root / "nowhere")
        self.assertEqual(self.door("create", "id_dangling", "--no-passphrase").returncode, 2)

    def test_invalid_names_are_refused_before_anything_runs(self):
        for name in ("../escape", ".hidden", "id.pub", "a/b", "", "x" * 65, "-rf"):
            p = self.door("create", name, "--no-passphrase")
            self.assertEqual(p.returncode, 2, name)
        self.assertEqual(self.calls("ssh-keygen"), [])

    def test_a_passphrase_without_a_terminal_is_refused(self):
        p = self.door("create", "id_new")
        self.assertEqual(p.returncode, 2)
        self.assertIn("typed by a person", p.stderr)
        self.assertEqual(self.calls("ssh-keygen"), [])

    def test_a_passphrase_is_never_an_argument(self):
        import pty
        pid, fd = pty.fork()
        if pid == 0:
            os.execve(sys.executable, [sys.executable, str(DOOR), "create", "id_tty", "--comment", "tty key"],
                      {**self.env, "FAKE_KEYCHAIN_FAIL": ""})
        output = b""
        while True:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            output += chunk
        _, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 0, output.decode(errors="replace"))
        argv = self.calls("ssh-keygen")[0]
        self.assertNotIn("-N", argv)
        keychain = [c for c in self.calls("ssh-add") if "--apple-use-keychain" in c]
        if sys.platform == "darwin":
            self.assertEqual(len(keychain), 1)
        else:
            self.assertEqual(keychain, [])
            self.assertIn(b"not stored", output)

    def test_a_host_block_is_appended_and_a_taken_alias_refused_before_generation(self):
        p = self.door("create", "id_alpha", "--no-passphrase", "--host", "alpha",
                      "--hostname", "alpha.example.invalid", "--user", "deploy", "--port", "2222")
        self.assertEqual(p.returncode, 0, p.stderr)
        config = (self.ssh / "config").read_text()
        self.assertIn("Host alpha\n", config)
        self.assertIn("IdentityFile ~/.ssh/id_alpha", config)
        self.assertIn("Port 2222", config)
        self.assertEqual("UseKeychain yes" in config, sys.platform == "darwin")
        self.assertEqual(stat.S_IMODE((self.ssh / "config").stat().st_mode), 0o600)
        p = self.door("create", "id_alpha2", "--no-passphrase", "--host", "alpha", "--hostname", "x.example.invalid")
        self.assertEqual(p.returncode, 2)
        self.assertFalse((self.ssh / "id_alpha2").exists())
        self.assertEqual(len(self.calls("ssh-keygen")), 1)

    def test_a_race_loser_keeps_the_other_writers_file(self):
        p = self.door("create", "id_race", "--no-passphrase", env={"FAKE_RACE_DEST": str(self.home / ".ssh" / "id_race")})
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertEqual((self.ssh / "id_race").read_text(), "another writer's key\n")
        self.assertFalse((self.ssh / "id_race.pub").exists())
        self.assertEqual(self.receipts(), [])

    def test_a_failed_ssh_keygen_leaves_nothing(self):
        p = self.door("create", "id_fail", "--no-passphrase", env={"FAKE_KEYGEN_FAIL": "1"})
        self.assertEqual(p.returncode, 1)
        self.assertEqual(sorted(x.name for x in self.ssh.iterdir()), [])


class CopyPublic(Door):
    def test_copy_puts_exactly_the_public_line_on_the_clipboard(self):
        blob = self.plant("id_alpha", comment="alpha")
        p = self.door("copy", "id_alpha")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.clip.read_text(), pub_line(blob, "alpha"))
        self.assertIn(pub_line(blob, "alpha"), p.stdout)
        self.assertIn(fingerprint(blob), p.stdout)
        self.assertEqual(self.calls("clipboard"), [[]], "the clipboard tool got arguments")

    def test_no_clipboard_tool_prints_the_line_and_exits_3(self):
        blob = self.plant("id_alpha")
        p = self.door("copy", "id_alpha", env={"OBSERVATORY_CLIPBOARD": "none"})
        self.assertEqual(p.returncode, 3)
        self.assertIn(pub_line(blob, "id_alpha"), p.stdout)
        self.assertIn("clipboard was not set", p.stderr)

    def test_show_does_not_touch_the_clipboard(self):
        self.plant("id_alpha")
        self.assertEqual(self.door("show", "id_alpha").returncode, 0)
        self.assertFalse(self.clip.exists())

    def test_a_missing_public_half_is_refused_with_the_recreate_command(self):
        self.plant("id_alpha", public=False)
        p = self.door("copy", "id_alpha")
        self.assertEqual(p.returncode, 2)
        self.assertIn("ssh-keygen -y -f ~/.ssh/id_alpha", p.stderr)

    def test_two_keys_in_one_file_are_refused(self):
        self.ssh.mkdir(mode=0o700)
        (self.ssh / "id_two.pub").write_text(pub_line(ed25519_blob("a")) + "\n" + pub_line(ed25519_blob("b")) + "\n")
        self.assertEqual(self.door("copy", "id_two").returncode, 2)


class SendGithub(Door):
    def test_adds_with_type_and_title_and_writes_a_receipt(self):
        blob = self.plant("id_alpha", comment="alpha laptop")
        p = self.door("send", "id_alpha", "--to", "github")
        self.assertEqual(p.returncode, 0, p.stderr)
        add = [c for c in self.calls("gh") if c[:2] == ["ssh-key", "add"]]
        self.assertEqual(len(add), 1)
        self.assertEqual(add[0][2], str(self.ssh / "id_alpha.pub"))
        self.assertEqual(add[0][add[0].index("--type") + 1], "authentication")
        self.assertEqual(add[0][add[0].index("--title") + 1], "alpha laptop")
        rows = self.receipts()
        self.assertEqual([(r["event"], r["secret"], r["to"]) for r in rows],
                         [("ssh.registered", f"ssh/{fingerprint(blob)}", "github:authentication")])
        self.assertIn("ssh -T git@github.com", p.stdout)

    def test_a_second_send_is_already_registered_and_adds_nothing(self):
        self.plant("id_alpha")
        self.assertEqual(self.door("send", "id_alpha", "--to", "github").returncode, 0)
        p = self.door("send", "id_alpha", "--to", "github")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("already registered", p.stdout)
        self.assertEqual(len([c for c in self.calls("gh") if c[:2] == ["ssh-key", "add"]]), 1)
        self.assertEqual([r["event"] for r in self.receipts()], ["ssh.registered", "ssh.already_registered"])

    def test_both_uses(self):
        self.plant("id_alpha")
        self.assertEqual(self.door("send", "id_alpha", "--to", "github", "--as", "both").returncode, 0)
        types = sorted(c[c.index("--type") + 1] for c in self.calls("gh") if c[:2] == ["ssh-key", "add"])
        self.assertEqual(types, ["authentication", "signing"])

    def test_a_missing_scope_is_refused_before_anything_is_sent(self):
        self.plant("id_alpha")
        self.gh_state.write_text(json.dumps({"authentication": [], "signing": [],
                                             "list_fail": {"signing": "gh: Not Found (HTTP 404)"}}))
        p = self.door("send", "id_alpha", "--to", "github", "--as", "signing")
        self.assertEqual(p.returncode, 2)
        self.assertIn("admin:ssh_signing_key", p.stderr)
        self.assertEqual([c for c in self.calls("gh") if c[:2] == ["ssh-key", "add"]], [])

    def test_a_provider_refusal_is_shown_without_credentials_and_receipted(self):
        self.plant("id_alpha")
        token = "gh" + "p_" + "Q7m2Xz9R4tLbK8wN1vPc5jD6sAu9f3Qm2XzR"
        self.gh_state.write_text(json.dumps({"authentication": [], "signing": [],
                                             "add_fail": f"HTTP 422: key is already in use ({token})"}))
        p = self.door("send", "id_alpha", "--to", "github")
        self.assertEqual(p.returncode, 1)
        self.assertIn("key is already in use", p.stderr)
        self.assertNotIn(token, p.stderr + p.stdout)
        self.assertEqual([r["event"] for r in self.receipts()], ["ssh.register_failed"])
        self.assertNotIn(token, json.dumps(self.receipts()))

    def test_gh_missing_is_refused(self):
        self.plant("id_alpha")
        p = self.door("send", "id_alpha", "--to", "github", env={"OBSERVATORY_GH": str(self.root / "no-gh")})
        self.assertEqual(p.returncode, 2)
        self.assertIn("gh", p.stderr)


class SendServer(Door):
    def authorized(self) -> list[str]:
        return (self.remote / ".ssh" / "authorized_keys").read_text().splitlines()

    def test_appends_once_and_a_second_send_is_a_no_op(self):
        blob = self.plant("id_alpha")
        (self.remote / ".ssh").mkdir(mode=0o700)
        (self.remote / ".ssh" / "authorized_keys").write_text("ssh-ed25519 OTHERKEY other")  # no newline
        p = self.door("send", "id_alpha", "--to", "server:deploy@alpha.example.invalid")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.authorized(), ["ssh-ed25519 OTHERKEY other", pub_line(blob, "id_alpha")])
        p = self.door("send", "id_alpha", "--to", "server:deploy@alpha.example.invalid")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("already", p.stdout)
        self.assertEqual(len(self.authorized()), 2)
        self.assertEqual([r["event"] for r in self.receipts()], ["ssh.registered", "ssh.already_registered"])
        argv = self.calls("ssh")[0]
        self.assertIn("BatchMode=yes", argv)
        self.assertEqual(argv[argv.index("--") + 1], "deploy@alpha.example.invalid")

    def test_a_fresh_server_gets_private_modes(self):
        self.plant("id_alpha")
        self.assertEqual(self.door("send", "id_alpha", "--to", "server:alpha").returncode, 0)
        self.assertEqual(stat.S_IMODE((self.remote / ".ssh").stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.remote / ".ssh" / "authorized_keys").stat().st_mode), 0o600)

    def test_a_key_already_present_with_options_is_already(self):
        blob = self.plant("id_alpha")
        (self.remote / ".ssh").mkdir(mode=0o700)
        (self.remote / ".ssh" / "authorized_keys").write_text(f'no-pty,from="10.0.0.1" {pub_line(blob, "x")}\n')
        p = self.door("send", "id_alpha", "--to", "server:alpha")
        self.assertIn("already", p.stdout)
        self.assertEqual(len(self.authorized()), 1)

    def test_verify_logs_in_with_the_new_key_alone(self):
        self.plant("id_alpha")
        p = self.door("send", "id_alpha", "--to", "server:alpha", "--verify")
        self.assertEqual(p.returncode, 0, p.stderr)
        verify = self.calls("ssh")[-1]
        self.assertIn("IdentitiesOnly=yes", verify)
        self.assertEqual(verify[verify.index("-i") + 1], str(self.ssh / "id_alpha"))
        self.assertEqual(verify[-1], "true")
        self.assertEqual([r["event"] for r in self.receipts()], ["ssh.registered", "ssh.verified"])

    def test_a_failed_verification_says_the_key_was_added(self):
        self.plant("id_alpha")
        p = self.door("send", "id_alpha", "--to", "server:alpha", "--verify",
                      env={"FAKE_SSH_VERIFY_FAIL": "1"})
        self.assertEqual(p.returncode, 1)
        self.assertIn("added", p.stdout)
        self.assertIn("Permission denied", p.stderr)
        self.assertEqual([r["event"] for r in self.receipts()], ["ssh.registered", "ssh.verify_failed"])

    def test_a_password_prompt_in_batch_mode_names_interactive(self):
        self.plant("id_alpha")
        p = self.door("send", "id_alpha", "--to", "server:alpha", env={"FAKE_SSH_PASSWORD_ONLY": "1"})
        self.assertEqual(p.returncode, 1)
        self.assertIn("--interactive", p.stderr)
        self.assertEqual([r["event"] for r in self.receipts()], ["ssh.register_failed"])

    def test_an_unreachable_server_names_ssh_reason(self):
        self.plant("id_alpha")
        p = self.door("send", "id_alpha", "--to", "server:alpha", env={"FAKE_SSH_UNREACHABLE": "1"})
        self.assertEqual(p.returncode, 1)
        self.assertIn("Connection refused", p.stderr)

    def test_an_option_shaped_alias_is_refused(self):
        self.plant("id_alpha")
        for alias in ("-oProxyCommand=touch x", "alpha;id", "al pha", ""):
            p = self.door("send", "id_alpha", "--to", f"server:{alias}")
            self.assertEqual(p.returncode, 2, alias)
        self.assertEqual(self.calls("ssh"), [])


class SendCloud(Door):
    def test_a_cloud_is_refused_with_the_gap_named(self):
        self.plant("id_alpha")
        for target in ("digitalocean", "hetzner"):
            p = self.door("send", "id_alpha", "--to", target)
            self.assertEqual(p.returncode, 2, target)
            self.assertIn("OBS-112", p.stderr)
            self.assertIn("ssh-keys copy id_alpha", p.stderr)


class Findings(Door):
    def doc(self) -> dict:
        now = "2026-10-15T00:00:00Z"
        return {"schema": "ssh-keys/1", "measured_at": now,
                "folder": {"path": "~/.ssh", "exists": True, "mode": "0755", "link": False},
                "keys": [
                    {"name": "id_open", "path": "~/.ssh/id_open", "private": True, "public": True, "link": False,
                     "type": "ED25519", "bits": 256, "fingerprint": "SHA256:open", "passphrase": "no",
                     "private_mode": "0644", "hosts": [], "in_agent": False, "github": [],
                     "last_access": "2025-01-01T00:00:00Z"},
                    {"name": "id_old", "path": "~/.ssh/id_old", "private": True, "public": True, "link": False,
                     "type": "RSA", "bits": 2048, "fingerprint": "SHA256:old", "passphrase": "yes",
                     "private_mode": "0600", "hosts": ["alpha"], "in_agent": True, "github": ["authentication"],
                     "last_access": now},
                    {"name": "id_dsa", "path": "~/.ssh/id_dsa", "private": True, "public": True, "link": False,
                     "type": "DSA", "bits": 1024, "fingerprint": "SHA256:dsa", "passphrase": "yes",
                     "private_mode": "0600", "hosts": ["beta"], "in_agent": False, "github": [],
                     "last_access": now},
                    {"name": "id_lonely", "path": "~/.ssh/id_lonely", "private": True, "public": False, "link": False,
                     "type": None, "bits": None, "fingerprint": None, "passphrase": "unknown",
                     "private_mode": "0600", "hosts": [], "in_agent": None, "github": None, "last_access": now}],
                "agent_only": [],
                "identity_files": [{"host": "gamma", "path": "~/.ssh/id_gone", "exists": False}],
                "github": {"read": {"authentication": True, "signing": True},
                           "keys": [{"id": 9, "title": "old laptop", "use": "authentication",
                                     "fingerprint": "SHA256:gone", "on_machine": False}]},
                "degraded": []}

    def test_every_rule_fires_once_with_an_action_and_no_private_byte(self):
        import ssh_key_findings
        rows = ssh_key_findings.findings(self.doc())
        types = sorted(r["type"] for r in rows)
        self.assertEqual(types, sorted([
            "ssh.key_no_passphrase", "ssh.key_readable", "ssh.key_unused", "ssh.key_unregistered",
            "ssh.key_weak", "ssh.key_weak", "ssh.folder_permissions", "ssh.public_missing",
            "ssh.identity_missing", "ssh.github_orphan"]))
        weak = {r["subject"]: r["severity"] for r in rows if r["type"] == "ssh.key_weak"}
        self.assertEqual(weak, {"ssh:SHA256:old": "warning", "ssh:SHA256:dsa": "critical"})
        for r in rows:
            self.assertTrue(r["action"] and r["title_id"] and r["evidence"], r)
        self.assertEqual(rows, ssh_key_findings.findings(self.doc()), "the same inventory gave other rows")

    def test_unread_github_raises_no_registration_rows(self):
        import ssh_key_findings
        doc = self.doc()
        doc["github"] = None
        for k in doc["keys"]:
            k["github"] = None
        types = {r["type"] for r in ssh_key_findings.findings(doc)}
        self.assertNotIn("ssh.key_unregistered", types)
        self.assertNotIn("ssh.github_orphan", types)

    def test_every_type_has_a_label(self):
        import ssh_key_findings
        sys.path.insert(0, str(ROOT / "dashboard"))
        import finding_types
        self.assertEqual(sorted(set(ssh_key_findings.TYPES) - set(finding_types.LABELS)), [])

    def test_absent_inventory_is_no_rows_and_the_command_says_so(self):
        import ssh_key_findings
        self.assertEqual(ssh_key_findings.from_file(self.scratch / "ssh-keys.json"), [])
        p = self.door("findings")
        self.assertEqual(p.returncode, 0)
        self.assertIn("No inventory yet", p.stdout)
        self.assertIn("ssh-keys list", p.stdout)

    def test_the_command_reads_the_saved_inventory(self):
        (self.scratch / "ssh-keys.json").write_text(json.dumps(self.doc()))
        p = self.door("findings", "--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(len(json.loads(p.stdout)["findings"]), 10)


class Surfaces(Door):
    def test_the_engine_cli_dispatches_ssh_keys(self):
        p = subprocess.run([sys.executable, str(ROOT / "observatory.py"), "ssh-keys", "--help"],
                           capture_output=True, text=True, env=self.env, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        for word in ("list", "create", "copy", "send", "findings"):
            self.assertIn(word, p.stdout)

    def test_the_agent_view_names_one_key_and_its_commands(self):
        blob = self.plant("id_alpha")
        import ssh_keys
        old = dict(os.environ)
        try:
            os.environ.update(self.env)
            one = ssh_keys.agent_view(name="id_alpha")
            unknown = ssh_keys.agent_view(name="id_nope")
            shaped = ssh_keys.agent_view(name="gh" + "p_" + "Q7m2Xz9R4tLbK8wN1vPc5jD6sAu9f3Qm2XzR")
            every = ssh_keys.agent_view()
        finally:
            os.environ.clear()
            os.environ.update(old)
        self.assertEqual(one["public"], pub_line(blob, "id_alpha"))
        self.assertIn("ssh-keys copy id_alpha", one["commands"]["copy"])
        self.assertIn("error", unknown)
        self.assertIn("error", shaped)
        self.assertNotIn("Q7m2Xz9R", json.dumps(shaped))
        self.assertIn("degraded", every)
        self.assertTrue(any("GitHub" in d["reason"] or d["source"].startswith("github") for d in every["degraded"]))


if __name__ == "__main__":
    unittest.main()
