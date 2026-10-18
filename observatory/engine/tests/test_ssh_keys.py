#!/usr/bin/env python3
"""The SSH-key door (OSS-62–67, docs/design/SSH-KEYS.md) against fakes in a temporary HOME.

Every case runs `tools/ssh_keys.py` in a subprocess with HOME pointing at a scratch folder,
so the real `~/.ssh` is never read. `ssh-keygen`, `ssh-add`, `ssh`, `gh` and the clipboard
are fakes that record their argv; one case uses the system's real `ssh-keygen`, still inside
the scratch HOME. The private keys planted here carry a marker in their private section, so
every output a person or an agent can see — stdout, stderr, the saved inventory, the
clipboard, the fakes' argv, the receipts journal and the MCP answer — is searched for it, and
the header reader is spied on to prove it never reads that far.

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
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
DOOR = ROOT / "tools" / "ssh_keys.py"
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))

BEGIN = "-----BEGIN "
END = "-----END "
OPENSSH = "OPENSSH " + "PRIVATE" + " KEY-----"
RSA_PEM = "RSA " + "PRIVATE" + " KEY-----"
#: The bytes no output may ever carry. Composed, so no literal in this file is key-shaped.
MARKER = b"PRIVATEMATERIAL" + b"-zq7" * 8


def _string(b: bytes) -> bytes:
    return len(b).to_bytes(4, "big") + b


def _mpint(n: int) -> bytes:
    raw = n.to_bytes((n.bit_length() + 8) // 8, "big")
    return _string(raw)


def ed25519_blob(seed: str) -> bytes:
    return _string(b"ssh-ed25519") + _string(hashlib.sha256(seed.encode()).digest())


def sk_blob(seed: str) -> bytes:
    return (_string(b"sk-ssh-ed25519@openssh.com") + _string(hashlib.sha256(seed.encode()).digest())
            + _string(b"ssh:"))


def rsa_blob(bits: int, seed: str) -> bytes:
    n = int.from_bytes(hashlib.sha512(seed.encode()).digest() * (bits // 512 + 1), "big")
    n = (n >> (n.bit_length() - bits)) | (1 << (bits - 1)) | 1
    return _string(b"ssh-rsa") + _mpint(65537) + _mpint(n)


def fingerprint(blob: bytes) -> str:
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def pub_line(blob: bytes, comment: str = "") -> str:
    ktype = blob[4:4 + int.from_bytes(blob[:4], "big")].decode()
    return f"{ktype} {base64.b64encode(blob).decode()}" + (f" {comment}" if comment else "")


def openssh_body(blob: bytes, cipher: str = "none", marker: bytes = MARKER) -> bytes:
    """The decoded OpenSSH container: the clear header, the public key, then the private section."""
    kdf, opts = (b"none", b"") if cipher == "none" else (b"bcrypt", _string(b"s" * 16) + (16).to_bytes(4, "big"))
    return (b"openssh-key-v1\0" + _string(cipher.encode()) + _string(kdf) + _string(opts)
            + (1).to_bytes(4, "big") + _string(blob) + _string(marker * 4))


def private_text(cipher: str = "none", blob: bytes | None = None, marker: bytes = MARKER) -> str:
    encoded = base64.b64encode(openssh_body(blob or ed25519_blob("planted"), cipher, marker)).decode()
    lines = [encoded[i:i + 70] for i in range(0, len(encoded), 70)]
    return BEGIN + OPENSSH + "\n" + "\n".join(lines) + "\n" + END + OPENSSH + "\n"


def marker_fragments() -> list[str]:
    """The marker's base64 at the three alignments it can take inside a file."""
    out = []
    for pad in (b"", b"x", b"xx"):
        enc = base64.b64encode(pad + MARKER * 4).decode()
        out.append(enc[8:48])
    return out


def marker_file_offset(text: str, blob: bytes, cipher: str = "none") -> int:
    """Where, in the file's bytes, the first base64 character carrying a private byte sits."""
    body = openssh_body(blob, cipher)
    private_at = body.index(MARKER)          # the first byte of the private section's contents
    char = (private_at // 3) * 4             # the base64 group that carries it
    head = len(BEGIN + OPENSSH) + 1
    return head + char + char // 70           # one newline per 70 base64 characters


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
    bits = {"ssh-ed25519": 256, "sk-ssh-ed25519@openssh.com": 256}.get(t)
    if t == "ssh-rsa": bits = int.from_bytes(f[2], "big").bit_length()
    fp = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    name = {"ssh-ed25519": "ED25519", "ssh-rsa": "RSA", "sk-ssh-ed25519@openssh.com": "ED25519-SK"}[t]
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
blob = s(b"ssh-ed25519") + s(hashlib.sha256(str(path).encode()).digest())
body = (b"openssh-key-v1\0" + s(cipher) + s(b"none" if cipher == b"none" else b"bcr" + b"ypt") + s(b"")
        + (1).to_bytes(4, "big") + s(blob) + s(marker * 4))
enc = base64.b64encode(body).decode()
head = "-----BEGIN " + "OPENSSH PRIVATE" + " KEY-----"
path.write_text(head + "\n" + "\n".join(enc[i:i + 70] for i in range(0, len(enc), 70)) + "\n")
pathlib.Path(str(path) + ".pub").write_text("ssh-ed25519 " + base64.b64encode(blob).decode() + " " + comment + "\n")
'''

FAKE_AGENT = r'''
import json, os, pathlib, sys
log = pathlib.Path(os.environ["FAKE_LOG"]); a = sys.argv[1:]
with log.open("a") as f: f.write(json.dumps({"tool": "ssh-add", "argv": a}) + "\n")
sock = os.environ.get("SSH_AUTH_SOCK", "")
if sock == os.environ.get("FAKE_DEFAULT_SOCK"):
    state = os.environ.get("FAKE_AGENT", "")
else:   # an agent the config names (IdentityAgent): its state, or not running
    state = json.loads(os.environ.get("FAKE_AGENT_SOCKS", "{}")).get(sock, "down")
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
stdin = sys.stdin.read() if a[:3] == ["ssh-key", "add", "-"] else None
with log.open("a") as f: f.write(json.dumps({"tool": "gh", "argv": a, "stdin": stdin}) + "\n")
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
    use = a[a.index("--type") + 1]
    title = next(x.split("=", 1)[1] for x in a if x.startswith("--title="))
    text = stdin if a[2] == "-" else pathlib.Path(a[2]).read_text()
    key = " ".join(text.split()[:2])
    s[use].append({"id": 100 + len(s[use]), "title": title, "key": key, "created_at": "2026-10-15T00:00:00Z"})
    st.write_text(json.dumps(s)); print("Public key added to your account"); sys.exit(0)
print("unknown command", file=sys.stderr); sys.exit(2)
'''

FAKE_SSH = r'''
import base64, hashlib, json, os, pathlib, subprocess, sys
log = pathlib.Path(os.environ["FAKE_LOG"]); a = sys.argv[1:]
with log.open("a") as f: f.write(json.dumps({"tool": "ssh", "argv": a}) + "\n")
home = pathlib.Path(os.environ["HOME"]); remote = pathlib.Path(os.environ["FAKE_REMOTE_HOME"])
def blocks():
    out, cur = {}, None
    cfg = home / ".ssh" / "config"
    for line in (cfg.read_text().splitlines() if cfg.is_file() else []):
        parts = line.split(None, 1)
        if len(parts) < 2: continue
        k, v = parts[0].lower(), parts[1].strip()
        if k == "host":
            cur = {"names": v.split(), "identityfile": []}
            for n in cur["names"]: out[n] = cur
        elif cur is not None:
            if k == "identityfile": cur["identityfile"].append(v.replace("~", str(home)))
            else: cur[k] = v
    for c in list(out.values()):
        if "hostname" in c: out.setdefault(c["hostname"], c)
    return out
if a[:1] == ["-G"]:
    alias = a[-1]; c = blocks().get(alias, {})
    print(f"hostname {c.get('hostname', alias)}")
    print(f"user {c.get('user', 'tester')}")
    print(f"port {c.get('port', '22')}")
    for i in c.get("identityfile", []): print(f"identityfile {i}")
    print(f"proxyjump {c.get('proxyjump', 'none')}")
    if "proxycommand" in c: print(f"proxycommand {c['proxycommand']}")
    print("userknownhostsfile ~/.ssh/known_hosts ~/.ssh/known_hosts2")
    print("stricthostkeychecking ask")
    sys.exit(0)
opts = []; identities = []; verbose = False; i = 0
while i < len(a) and a[i] != "--":
    if a[i] == "-o": opts.append(a[i + 1]); i += 2; continue
    if a[i] in ("-i", "-F", "-p", "-l", "-J"):
        if a[i] == "-i": identities.append(a[i + 1])
        if a[i] == "-F": opts.append("F=" + a[i + 1])
        i += 2; continue
    if a[i] == "-v": verbose = True
    i += 1
host, command = a[i + 1], a[i + 2:]
if os.environ.get("FAKE_SSH_PASSWORD_ONLY") and "BatchMode=yes" in opts:
    print(f"{host}: Permission denied (publickey,password).", file=sys.stderr); sys.exit(255)
if os.environ.get("FAKE_SSH_UNREACHABLE"):
    print(f"ssh: connect to host {host} port 22: Connection refused", file=sys.stderr); sys.exit(255)
if command == ["true"]:
    o = dict(x.split("=", 1) for x in opts if "=" in x and not x.startswith("F="))
    # The verify must keep the user's host-key policy and known-hosts file (M18).
    if o.get("StrictHostKeyChecking", "ask") != "ask" or "known_hosts" not in o.get("UserKnownHostsFile", "known_hosts"):
        print("fake ssh: host-key checking was changed", file=sys.stderr); sys.exit(97)
    if "PreferredAuthentications" in o and "publickey" not in o["PreferredAuthentications"].split(","):
        print(f"{host}: Permission denied (publickey).", file=sys.stderr); sys.exit(255)
    if "F=none" not in opts or os.environ.get("FAKE_SSH_IGNORE_F"):
        identities += blocks().get(host, {}).get("identityfile", [])   # OpenSSH accumulates them
    if o.get("IdentityAgent", "") != "none" and o.get("IdentitiesOnly") != "yes":
        identities += [x for x in os.environ.get("FAKE_AGENT_IDENTS", "").split(":") if x]
    ak = remote / ".ssh" / "authorized_keys"
    allowed = ak.read_text().split() if ak.is_file() else []
    agent_blobs = {pathlib.Path(x + ".pub").read_text().split()[1]
                   for x in os.environ.get("FAKE_AGENT_IDENTS", "").split(":") if x}
    usable = []
    for ident in identities:
        first = pathlib.Path(ident).read_text().split("\n", 1)[0] if pathlib.Path(ident).is_file() else ""
        blob = pathlib.Path(ident + ".pub").read_text().split()[1]
        if "ENCRYPTED" in first:   # the file cannot be decrypted without a person
            if o.get("IdentityAgent", "") != "none" and blob in agent_blobs:
                usable.append(ident)   # the agent holds that public key and signs for it
            continue
        usable.append(ident)
    for ident in usable:
        b64 = pathlib.Path(ident + ".pub").read_text().split()[1]
        fp = "SHA256:" + base64.b64encode(hashlib.sha256(base64.b64decode(b64)).digest()).decode().rstrip("=")
        if verbose:
            print(f"debug1: Offering public key: {ident} ED25519 {fp} explicit", file=sys.stderr)
        if b64 in allowed:
            if verbose and not os.environ.get("FAKE_SSH_NO_ACCEPT_LINE"):
                print(f"debug1: Server accepts key: {ident} ED25519 {fp} explicit", file=sys.stderr)
            sys.exit(0)
    print(f"{host}: Permission denied (publickey).", file=sys.stderr); sys.exit(255)
if os.environ.get("FAKE_SSH_WORDS_LIE"):   # a login shell that broke the script but echoed
    sys.stdin.read(); print("added"); sys.exit(1)
if os.environ.get("FAKE_SSH_SWALLOW"):   # a server whose sshd reads keys from elsewhere
    sys.stdin.read(); print("added"); sys.exit(0)
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
            "SSH_AUTH_SOCK": str(self.root / "default-agent.sock"),
            "FAKE_DEFAULT_SOCK": str(self.root / "default-agent.sock"),
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
            (self.ssh / name).write_text(private_text(cipher, blob))
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

    def rows(self, tool: str) -> list[dict]:
        if not self.log.is_file():
            return []
        rows = [json.loads(l) for l in self.log.read_text().splitlines() if l]
        return [r for r in rows if r["tool"] == tool]

    def calls(self, tool: str) -> list[list[str]]:
        return [r["argv"] for r in self.rows(tool)]

    def receipts(self) -> list[dict]:
        path = self.vault / "movements.jsonl"
        return [json.loads(l) for l in path.read_text().splitlines() if l] if path.is_file() else []

    def in_process(self, fn, *args, **kw):
        """Run `fn` with this test's environment, in this process."""
        old = dict(os.environ)
        try:
            os.environ.update(self.env)
            return fn(*args, **kw)
        finally:
            os.environ.clear()
            os.environ.update(old)


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
        self.assertEqual((a["path"], a["private_mode"], a["public_mode"], a["private_ok"]),
                         ("~/.ssh/id_alpha", "0600", "0644", True))
        self.assertTrue(all("-l" in c for c in self.calls("ssh-keygen")), "ssh-keygen ran for anything but -l")
        self.assertTrue(all(c[-1].endswith(".pub") for c in self.calls("ssh-keygen")),
                        "ssh-keygen was given a private key")

    def test_a_private_key_without_its_public_half_is_fingerprinted_from_its_header(self):
        """F2: the OpenSSH header carries the public key in clear, so the fingerprint is known."""
        lonely = self.plant("id_lonely", public=False)
        self.plant("id_token", private=False)
        doc = self.listing("--no-github")
        row, token = self.key(doc, "id_lonely"), self.key(doc, "id_token")
        self.assertEqual((row["fingerprint"], row["type"], row["bits"]), (fingerprint(lonely), "ED25519", 256))
        self.assertTrue(row["private"] and not row["public"])
        self.assertIn("ssh-keygen -y", row["hint"])
        self.assertTrue(token["public"] and not token["private"])
        self.assertIsNotNone(token["fingerprint"])

    def test_a_pem_key_without_public_half_stays_unknown(self):
        self.ssh.mkdir(mode=0o700)
        (self.ssh / "id_pem").write_text(BEGIN + RSA_PEM + "\nMIIEow" + "A" * 60 + "\n" + END + RSA_PEM + "\n")
        (self.ssh / "id_pem").chmod(0o600)
        row = self.key(self.listing("--no-github"), "id_pem")
        self.assertIsNone(row["fingerprint"])
        self.assertEqual((row["format"], row["passphrase"]), ("pem", "no"))

    def test_an_encrypted_pem_key_has_a_passphrase(self):
        """F12 M17: `Proc-Type: 4,ENCRYPTED` in a PEM header means a passphrase."""
        self.ssh.mkdir(mode=0o700)
        (self.ssh / "id_pem").write_text(BEGIN + RSA_PEM + "\nProc-Type: 4,ENCRYPTED\nDEK-Info: AES-128-CBC,00\n\n"
                                         "MIIEow" + "A" * 60 + "\n" + END + RSA_PEM + "\n")
        self.assertEqual(self.key(self.listing("--no-github"), "id_pem")["passphrase"], "yes")

    def test_a_pub_that_does_not_match_its_private_key_is_flagged(self):
        self.plant("id_alpha")
        (self.ssh / "id_alpha.pub").write_text(pub_line(ed25519_blob("someone else"), "x") + "\n")
        row = self.key(self.listing("--no-github"), "id_alpha")
        self.assertEqual(row["fingerprint"], fingerprint(ed25519_blob("id_alpha")))
        self.assertTrue(row["mismatch"])

    def test_config_hosts_includes_and_missing_identity_files(self):
        self.plant("id_alpha")
        (self.ssh / "config.d").mkdir()
        (self.ssh / "config").write_text(
            "Include config.d/*\n\nHost alpha alpha-backup\n  HostName alpha.example.invalid\n"
            "  IdentityFile ~/.ssh/id_alpha\n")
        (self.ssh / "config.d" / "beta").write_text(
            "Host beta\n  IdentityFile=%d/.ssh/id_missing\n  IdentityFile ~/keys/%u/id_user\n")
        doc = self.listing("--no-github")
        self.assertEqual(self.key(doc, "id_alpha")["hosts"], ["alpha", "alpha-backup"])
        missing = [f for f in doc["identity_files"] if not f["exists"]]
        self.assertEqual([(f["host"], f["path"]) for f in missing],
                         [("beta", "~/.ssh/id_missing"), ("beta", "~/keys/%u/id_user")])
        saved = (self.scratch / "ssh-keys.json").read_text()
        self.assertNotIn("tester", saved, "F10: the user name reached the saved inventory")

    def test_a_fifo_or_huge_include_is_refused_not_read(self):
        """F5: an Include of a FIFO hung `list`; a huge one was read whole."""
        self.plant("id_alpha")
        (self.ssh / "config").write_text("Include ~/.ssh/pipe\nInclude ~/.ssh/huge\n")
        os.mkfifo(self.ssh / "pipe")
        with open(self.ssh / "huge", "w") as f:
            f.write("# filler\n" * (1024 * 1024 // 9 + 10))
        started = time.monotonic()
        doc = self.listing("--no-github")
        self.assertLess(time.monotonic() - started, 30)
        reasons = " ".join(d["reason"] for d in doc["degraded"] if d["source"] == "ssh-config")
        self.assertIn("~/.ssh/pipe", reasons)
        self.assertIn("~/.ssh/huge", reasons)

    def test_agent_keys_loaded_and_agent_only(self):
        alpha = self.plant("id_alpha")
        loaded = f"256 {fingerprint(alpha)} id_alpha (ED25519)|256 {fingerprint(ed25519_blob('x'))} card (ED25519)"
        doc = self.listing("--no-github", env={"FAKE_AGENT": loaded})
        self.assertTrue(self.key(doc, "id_alpha")["in_agent"])
        self.assertEqual([k["fingerprint"] for k in doc["agent_only"]], [fingerprint(ed25519_blob("x"))])

    def test_an_agent_that_does_not_answer_is_named_as_such(self):
        import ssh_keys
        degraded = []
        with mock.patch.object(ssh_keys, "run", side_effect=subprocess.TimeoutExpired("ssh-add", 15)):
            self.assertIsNone(ssh_keys.agent_fingerprints(degraded))
        self.assertIn("did not answer within 15 s", degraded[0]["reason"])
        self.assertNotIn("install OpenSSH", json.dumps(degraded))

    def test_all_agent_reads_share_one_budget(self):
        import ssh_keys
        degraded = []
        with mock.patch.object(ssh_keys, "run") as run:
            self.assertIsNone(ssh_keys.agent_fingerprints(degraded, sock="/x", deadline=time.monotonic() - 1))
            run.assert_not_called()
        self.assertIn("was spent", degraded[0]["reason"])
        calls = []
        with mock.patch.object(ssh_keys, "run", side_effect=lambda *a, **k: calls.append(k["timeout"])
                               or subprocess.CompletedProcess(a, 1, "", "")):
            ssh_keys.agent_fingerprints([], deadline=time.monotonic() + 4)
        self.assertLessEqual(calls[0], 4)

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

    def test_a_key_without_pub_is_not_an_orphan_on_github(self):
        """F2: this machine's own key, its .pub missing, must not be offered for deletion."""
        alpha = self.plant("id_alpha", public=False)
        self.gh_state.write_text(json.dumps({"authentication": [
            {"id": 42, "title": "alpha", "key": pub_line(alpha), "created_at": "2026-01-01T00:00:00Z"}], "signing": []}))
        self.assertEqual(self.door("list").returncode, 0)
        import ssh_key_findings
        rows = ssh_key_findings.from_file(self.scratch / "ssh-keys.json")
        self.assertEqual([r for r in rows if r["type"] == "ssh.github_orphan"], [])
        self.assertNotIn("gh ssh-key delete 42", json.dumps(rows))

    def orphan_rows(self) -> list[dict]:
        import ssh_key_findings
        return [r for r in ssh_key_findings.from_file(self.scratch / "ssh-keys.json")
                if r["type"] == "ssh.github_orphan"]

    def github_holds(self, blob: bytes, key_id: int = 42):
        self.gh_state.write_text(json.dumps({"authentication": [
            {"id": key_id, "title": "in use", "key": pub_line(blob), "created_at": "2026-01-01T00:00:00Z"}],
            "signing": []}))

    def assert_no_delete(self, key_id: int = 42) -> dict:
        rows = self.orphan_rows()
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["severity"], "info")
        self.assertNotIn("action", rows[0])
        self.assertNotIn("gh ssh-key delete", json.dumps(rows))
        return rows[0]

    def test_the_mcp_answer_carries_no_delete_for_a_github_key(self):
        """Round 3: the MCP view merged a stale saved on_machine with live gaps; it must never
        hand an agent a delete."""
        import ssh_keys
        self.ssh.mkdir(mode=0o700)
        self.github_holds(ed25519_blob("elsewhere"))
        self.assertEqual(self.door("list").returncode, 0)
        view = self.in_process(ssh_keys.agent_view)
        text = json.dumps(view)
        self.assertNotIn("gh ssh-key delete", text)
        rows = [f for f in view["findings"] if f["type"] == "ssh.github_orphan"]
        self.assertEqual([(r["severity"], "action" in r) for r in rows], [("info", False)])

    def test_list_never_crashes_on_an_unreadable_identity_file_or_include(self):
        """Round 3 (a): EACCES on an IdentityFile's folder, a link loop, and an unreadable key
        become lines of `unseen`; `list` exits 0 on 3.11 and 3.14 alike."""
        self.plant("id_alpha")
        locked = self.home / "locked"
        locked.mkdir()
        (locked / "k").write_text(private_text("none", ed25519_blob("locked")))
        (self.ssh / "loop").symlink_to(self.ssh / "loop")
        (self.ssh / "config").write_text("Host a\n  IdentityFile ~/locked/k\nHost b\n  IdentityFile ~/.ssh/loop/k\n"
                                          "Include ~/locked/more.conf\n")
        (self.ssh / "id_noread").write_text(private_text())
        (self.ssh / "id_noread").chmod(0o000)
        locked.chmod(0o000)
        try:
            p = self.door("list", "--json", "--no-github")
        finally:
            locked.chmod(0o700)
            (self.ssh / "id_noread").chmod(0o600)
        self.assertEqual(p.returncode, 0, p.stderr)
        doc = json.loads(p.stdout)
        unseen = " ".join(doc["unseen"])
        self.assertIn("~/locked/k", unseen)
        if os.geteuid() != 0:
            self.assertIn("~/.ssh/id_noread (could not be read", unseen)
        self.assertEqual(self.key(doc, "id_alpha")["fingerprint"], fingerprint(ed25519_blob("id_alpha")))

    def test_a_tilde_user_identity_file_is_resolved_or_unseen(self):
        """Round 3 (b): ~user is that account's home (pwd), or a line of `unseen`."""
        import pwd
        me = pwd.getpwuid(os.getuid()).pw_name
        self.ssh.mkdir(mode=0o700)
        (self.ssh / "config").write_text(f"Host a\n  IdentityFile ~{me}/no-such-key-pb163\n"
                                          "Host b\n  IdentityFile ~no_such_user_pb163/k\n")
        doc = self.listing("--no-github")
        files = {f["path"]: f["exists"] for f in doc["identity_files"]}
        self.assertIs(files[f"~{me}/no-such-key-pb163"], False)
        self.assertIsNone(files["~no_such_user_pb163/k"])
        self.assertTrue(any("~no_such_user_pb163/k" in u for u in doc["unseen"]))

    def test_no_ssh_keys_yet_is_not_said_while_the_config_is_unread(self):
        """A degraded local source that is not a line of `unseen` (an Include that is a FIFO)."""
        self.ssh.mkdir(mode=0o700)
        (self.ssh / "config").write_text("Include ~/.ssh/pipe\n")
        os.mkfifo(self.ssh / "pipe")
        p = self.door("list", "--no-github")
        self.assertNotIn("No SSH keys yet", p.stdout)
        self.assertIn("~/.ssh/pipe", p.stdout)

    def test_no_ssh_keys_yet_is_not_said_while_the_agent_is_unread(self):
        self.ssh.mkdir(mode=0o700)
        p = self.door("list", "--no-github", env={"FAKE_AGENT": "down"})
        self.assertNotIn("No SSH keys yet", p.stdout)
        self.assertIn("No SSH keys found where Observatory could look", p.stdout)

    def test_a_key_in_an_agent_the_config_names_is_not_offered_for_deletion(self):
        """(a) `Host *` + IdentityAgent (1Password, Secretive): the key lives only in that agent."""
        self.ssh.mkdir(mode=0o700)
        (self.ssh / "config").write_text("Host *\n  IdentityAgent ~/agent-app.sock\n")
        self.github_holds(ed25519_blob("in the app"))
        p = self.door("list")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn("No SSH keys yet", p.stdout)
        self.assertIn("~/agent-app.sock", p.stdout)
        doc = json.loads((self.scratch / "ssh-keys.json").read_text())
        self.assertTrue(any(d["source"].startswith("identity-agent") for d in doc["degraded"]), doc["degraded"])
        self.assertIn("~/agent-app.sock", self.assert_no_delete()["detail"])

    def test_a_key_the_named_agent_holds_is_on_the_machine(self):
        """(a), read: the named agent answers and holds the GitHub key, so it is not an orphan."""
        held = ed25519_blob("in the app")
        self.ssh.mkdir(mode=0o700)
        (self.ssh / "config").write_text("Host *\n  IdentityAgent ~/agent-app.sock\n")
        self.github_holds(held)
        socks = {str(self.home / "agent-app.sock"): f"256 {fingerprint(held)} app key (ED25519)"}
        self.assertEqual(self.door("list", env={"FAKE_AGENT_SOCKS": json.dumps(socks)}).returncode, 0)
        self.assertEqual(self.orphan_rows(), [])

    def test_a_linked_key_is_not_offered_for_deletion(self):
        """(b) id_ed25519 and its .pub are links into a dotfiles folder: fingerprint unknown."""
        blob = ed25519_blob("dotfiles")
        dots = self.root / "dotfiles"
        dots.mkdir()
        (dots / "id_ed25519").write_text(private_text("none", blob))
        (dots / "id_ed25519.pub").write_text(pub_line(blob, "dots") + "\n")
        self.ssh.mkdir(mode=0o700)
        (self.ssh / "id_ed25519").symlink_to(dots / "id_ed25519")
        (self.ssh / "id_ed25519.pub").symlink_to(dots / "id_ed25519.pub")
        self.github_holds(blob)
        self.assertEqual(self.door("list").returncode, 0)
        self.assertIn("a link", self.assert_no_delete()["detail"])

    def test_a_key_two_folders_deep_is_inventoried(self):
        """(c) IdentityFile ~/.ssh/keys/work/deep_k: read, fingerprinted, not an orphan."""
        blob = ed25519_blob("deep")
        deep = self.ssh / "keys" / "work"
        deep.mkdir(parents=True)
        (deep / "deep_k").write_text(private_text("none", blob))
        (deep / "deep_k").chmod(0o600)
        (self.ssh / "config").write_text("Host work\n  IdentityFile ~/.ssh/keys/work/deep_k\n")
        self.github_holds(blob)
        doc = self.listing()
        self.assertEqual(self.key(doc, "keys/work/deep_k")["fingerprint"], fingerprint(blob))
        self.assertEqual(self.orphan_rows(), [])

    def test_a_host_dependent_identity_file_is_not_offered_for_deletion(self):
        """(d) IdentityFile ~/keys/%h outside ~/.ssh: which file depends on the host."""
        self.ssh.mkdir(mode=0o700)
        (self.home / "keys").mkdir()
        (self.home / "keys" / "box.example.invalid").write_text(private_text("none", ed25519_blob("h")))
        (self.ssh / "config").write_text("Host box\n  HostName box.example.invalid\n  IdentityFile ~/keys/%h\n")
        self.github_holds(ed25519_blob("h"))
        self.assertEqual(self.door("list").returncode, 0)
        self.assertIn("~/keys/%h", self.assert_no_delete()["detail"])

    def test_an_identity_file_with_an_environment_variable_is_read(self):
        """(e) IdentityFile ${HOME}/keys/gh: expanded, inventoried, not a missing file, not an orphan."""
        blob = ed25519_blob("env")
        (self.home / "keys").mkdir()
        (self.home / "keys" / "gh").write_text(private_text("none", blob))
        (self.home / "keys" / "gh").chmod(0o600)
        self.ssh.mkdir(mode=0o700)
        (self.ssh / "config").write_text("Host github.com\n  IdentityFile ${HOME}/keys/gh\n")
        self.github_holds(blob)
        doc = self.listing()
        self.assertEqual([f for f in doc["identity_files"] if f["exists"] is False], [])
        self.assertIn(fingerprint(blob), {k["fingerprint"] for k in doc["keys"]})
        self.assertEqual(self.orphan_rows(), [])
        self.assertNotIn(str(self.home), (self.scratch / "ssh-keys.json").read_text())

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

    def test_terminal_control_sequences_never_reach_the_terminal(self):
        """F6: names and config values are printed through the printable filter."""
        self.plant("id_alpha")
        (self.ssh / "config").write_text("Host evil\x1b]0;owned\x07\n  IdentityFile ~/.ssh/id_\x1b[2Jgone\n")
        p = self.door("list", "--no-github")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn("\x1b", p.stdout + p.stderr)

    def test_an_escape_sequence_in_a_file_name_never_reaches_the_terminal(self):
        """M12: a key file named with an escape sequence; `list` and `findings` print its name."""
        self.plant("id_\x1b]0;owned\x07x", mode=0o640)
        for args in (("list", "--no-github"), ("findings",)):
            p = self.door(*args)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertNotIn("\x1b", p.stdout + p.stderr, args)
            self.assertNotIn("\x07", p.stdout + p.stderr, args)

    @unittest.skipUnless(shutil.which("ssh-keygen"), "the system's ssh-keygen is not installed")
    def test_the_real_ssh_keygen_agrees(self):
        """The fakes model the real tool; this case holds them to it, inside the scratch HOME."""
        self.ssh.mkdir(mode=0o700)
        real = shutil.which("ssh-keygen")
        for name, phrase in (("id_open", ""), ("id_locked", "a test passphrase")):
            subprocess.run([real, "-q", "-t", "ed25519", "-N", phrase, "-C", name, "-f", str(self.ssh / name)],
                           check=True, capture_output=True, timeout=60)
        (self.ssh / "id_locked.pub").unlink()   # its fingerprint must come from the header alone
        doc = self.listing("--no-github", env={"OBSERVATORY_SSH_KEYGEN": real})
        for name in ("id_open", "id_locked"):
            expected = subprocess.run([real, "-l", "-E", "sha256", "-f", str(self.ssh / name)],
                                      capture_output=True, text=True, check=True).stdout.split()[1]
            self.assertEqual(self.key(doc, name)["fingerprint"], expected, name)
        self.assertEqual((self.key(doc, "id_open")["passphrase"], self.key(doc, "id_locked")["passphrase"]),
                         ("no", "yes"))
        text = (self.ssh / "id_open").read_text()
        out = json.dumps(doc)
        body = [l for l in text.splitlines() if l and not l.startswith("-----")]
        self.assertFalse(any(line in out for line in body[2:]))


class HeaderReadIsBounded(Door):
    """F4: the private section is never read. os.read is spied on, in this process."""

    def read_bytes(self, path: pathlib.Path) -> tuple[dict | None, int]:
        import ssh_keys
        total = [0]
        real = os.read

        def spy(fd, n):
            data = real(fd, n)
            total[0] += len(data)
            return data
        with mock.patch.object(ssh_keys.os, "read", spy):
            header = self.in_process(ssh_keys.private_header, path)
        return header, total[0]

    def test_an_openssh_header_stops_before_the_private_section(self):
        for cipher, blob in (("none", ed25519_blob("a")), ("aes256-ctr", ed25519_blob("b")),
                             ("none", rsa_blob(4096, "c")), ("none", sk_blob("d"))):
            self.ssh.mkdir(mode=0o700, exist_ok=True)
            path = self.ssh / "id_spy"
            text = private_text(cipher, blob)
            path.write_text(text)
            header, n = self.read_bytes(path)
            self.assertEqual(header["fingerprint"], fingerprint(blob))
            self.assertLessEqual(n, marker_file_offset(text, blob, cipher),
                            f"{n} bytes read reach the private section ({cipher}, {len(blob)}-byte key)")
            self.assertLessEqual(n, len(BEGIN + OPENSSH) + 1 + 4 * ((160 + len(blob)) // 3 + 1) + 16)

    def test_a_lying_public_key_length_does_not_carry_the_read_further(self):
        """M23: the header says the public key is 4000 bytes; an ed25519 key is 51. The read stops
        at the honest ed25519 bound and gives no fingerprint."""
        blob = ed25519_blob("liar")
        honest = private_text("none", blob)
        body = bytearray(openssh_body(blob))
        at = body.index(blob) - 4
        body[at:at + 4] = (4000).to_bytes(4, "big")
        encoded = base64.b64encode(bytes(body)).decode()
        self.ssh.mkdir(mode=0o700)
        path = self.ssh / "id_liar"
        path.write_text(BEGIN + OPENSSH + "\n" + "\n".join(encoded[i:i + 70] for i in range(0, len(encoded), 70))
                        + "\n" + END + OPENSSH + "\n")
        header, n = self.read_bytes(path)
        self.assertNotIn("fingerprint", header)
        self.assertLessEqual(n, marker_file_offset(honest, blob))

    def test_a_pem_key_reads_only_the_header_line_and_the_next_ten_bytes(self):
        self.ssh.mkdir(mode=0o700)
        path = self.ssh / "id_pem"
        path.write_text(BEGIN + RSA_PEM + "\n" + "MIIEow" + "Q" * 600 + "\n" + END + RSA_PEM + "\n")
        _, n = self.read_bytes(path)
        self.assertLessEqual(n, len(BEGIN + RSA_PEM) + 1 + len("Proc-Type:"))

    def test_a_file_that_is_not_a_key_costs_one_line_at_most(self):
        self.ssh.mkdir(mode=0o700)
        path = self.ssh / "notes"
        path.write_text("x" * 5000)
        header, n = self.read_bytes(path)
        self.assertIsNone(header)
        self.assertLessEqual(n, 64)


class PrivateBytesNeverLeave(Door):
    def everything_seen(self) -> str:
        parts = []
        for path in (self.scratch / "ssh-keys.json", self.clip, self.log, self.vault / "movements.jsonl"):
            if path.is_file():
                parts.append(path.read_text(errors="replace"))
        return "\n".join(parts)

    def assert_clean(self, text: str):
        self.assertNotIn(MARKER.decode(), text)
        for fragment in marker_fragments():
            self.assertNotIn(fragment, text)

    def test_no_private_byte_in_any_output(self):
        self.plant("id_alpha", comment="alpha")
        self.plant("id_lonely", public=False)
        (self.ssh / "config").write_text("Host alpha\n  IdentityFile ~/.ssh/id_alpha\n")
        outputs = []
        for args in (("list",), ("list", "--json"), ("show", "id_alpha"), ("copy", "id_alpha"),
                     ("send", "id_alpha", "--to", "github", "--as", "both"),
                     ("send", "id_alpha", "--to", "server:alpha", "--verify"), ("findings",),
                     ("findings", "--json"), ("create", "id_new", "--no-passphrase"), ("show", "id_lonely")):
            p = self.door(*args)
            outputs += [p.stdout, p.stderr]
        import ssh_keys
        outputs.append(json.dumps(self.in_process(ssh_keys.agent_view)))
        outputs.append(json.dumps(self.in_process(ssh_keys.agent_view, name="id_alpha")))
        self.assert_clean("\n".join(outputs) + self.everything_seen())
        for argv in self.calls("ssh-keygen"):
            if "-l" in argv:
                self.assertTrue(argv[-1].endswith(".pub"), argv)
        for row in self.rows("gh"):
            if row["stdin"] is not None:
                self.assert_clean(row["stdin"])

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

    def test_a_private_key_saved_as_pub_is_a_critical_finding(self):
        """F9: detected from the header line alone."""
        self.ssh.mkdir(mode=0o700)
        (self.ssh / "id_wrong.pub").write_text(private_text())
        self.assertEqual(self.door("list", "--no-github").returncode, 0)
        import ssh_key_findings
        rows = [r for r in ssh_key_findings.from_file(self.scratch / "ssh-keys.json")
                if r["type"] == "ssh.private_in_pub"]
        self.assertEqual([(r["severity"], r["subject"]) for r in rows], [("critical", "ssh:~/.ssh/id_wrong.pub")])
        self.assert_clean((self.scratch / "ssh-keys.json").read_text())


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

    def test_reserved_names_are_refused(self):
        """F7: a private key written over config, rc or authorized_keys, and hidden from the inventory."""
        for name in ("config", "rc", "environment", "authorized_keys", "authorized_keys2", "known_hosts",
                     "known_hosts2", "known_hosts.old", "id_alpha-cert"):
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
            self.assertIn(b"loaded into the running agent", output)
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

    def test_a_header_inside_the_comment_is_refused(self):
        """F12 M1: one valid line whose comment carries a private-key header line."""
        self.ssh.mkdir(mode=0o700)
        (self.ssh / "id_odd.pub").write_text(pub_line(ed25519_blob("a"), BEGIN + OPENSSH) + "\n")
        self.assertEqual(self.door("copy", "id_odd").returncode, 2)
        self.assertFalse(self.clip.exists())

    def test_a_linked_pub_is_not_followed(self):
        """F12 M2."""
        self.ssh.mkdir(mode=0o700)
        elsewhere = self.root / "elsewhere.pub"
        elsewhere.write_text(pub_line(ed25519_blob("a"), "a") + "\n")
        (self.ssh / "id_link.pub").symlink_to(elsewhere)
        p = self.door("show", "id_link")
        self.assertEqual(p.returncode, 2)
        self.assertNotIn("ssh-ed25519", p.stdout)

    def test_a_name_that_walks_out_of_the_ssh_folder_is_refused(self):
        """F12 M12: `show ../x` would read ~/x.pub. ~/.ssh and ~/.ssh/keys exist, so a traversal
        would resolve; only the name check stands in the way."""
        self.ssh.mkdir(mode=0o700)
        (self.ssh / "keys").mkdir()
        (self.home / "x.pub").write_text(pub_line(ed25519_blob("a"), "a") + "\n")
        (self.root / "y.pub").write_text(pub_line(ed25519_blob("b"), "b") + "\n")
        for name in ("../x", "keys/../../x", str(self.root / "y"), "..", "keys/./../../x", "./../x"):
            p = self.door("show", name)
            self.assertEqual(p.returncode, 2, name)
            self.assertNotIn("ssh-ed25519", p.stdout, name)

    def test_a_pub_swapped_for_a_link_after_the_check_is_not_read(self):
        """F12 M2b: the lstat check and the open are two steps; O_NOFOLLOW closes the gap."""
        import ssh_keys
        self.ssh.mkdir(mode=0o700)
        elsewhere = self.root / "elsewhere.pub"
        elsewhere.write_text(pub_line(ed25519_blob("a"), "a") + "\n")
        link = self.ssh / "id_swap.pub"
        link.symlink_to(elsewhere)
        regular = os.stat(elsewhere)   # what lstat saw before the swap
        with mock.patch.object(ssh_keys.os, "lstat", lambda path, *a, **k: regular):
            with self.assertRaises((OSError, ssh_keys.Refused)):
                self.in_process(ssh_keys.read_public, link)

    def test_a_pub_that_belongs_to_another_key_is_refused(self):
        self.plant("id_alpha")
        (self.ssh / "id_alpha.pub").write_text(pub_line(ed25519_blob("someone else"), "x") + "\n")
        p = self.door("copy", "id_alpha")
        self.assertEqual(p.returncode, 2)
        self.assertIn("does not belong to", p.stderr)
        self.assertFalse(self.clip.exists())


class SendGithub(Door):
    def test_adds_through_stdin_with_type_and_title_and_writes_a_receipt(self):
        blob = self.plant("id_alpha", comment="alpha laptop")
        p = self.door("send", "id_alpha", "--to", "github")
        self.assertEqual(p.returncode, 0, p.stderr)
        add = [r for r in self.rows("gh") if r["argv"][:2] == ["ssh-key", "add"]]
        self.assertEqual(len(add), 1)
        argv = add[0]["argv"]
        self.assertEqual(argv[2], "-", "F14: the key goes on stdin")
        self.assertEqual(add[0]["stdin"].strip(), pub_line(blob, "alpha laptop"))
        self.assertEqual(argv[argv.index("--type") + 1], "authentication")
        self.assertIn("--title=alpha laptop", argv, "F14: the title is one argv element")
        rows = self.receipts()
        self.assertEqual([(r["event"], r["secret"], r["to"]) for r in rows],
                         [("ssh.registered", f"ssh/{fingerprint(blob)}", "github:authentication")])
        self.assertIn("ssh -T git@github.com", p.stdout)

    def test_a_title_that_looks_like_an_option_stays_a_title(self):
        self.plant("id_alpha")
        p = self.door("send", "id_alpha", "--to", "github", "--title=--type=signing")
        self.assertEqual(p.returncode, 0, p.stderr)
        argv = [r["argv"] for r in self.rows("gh") if r["argv"][:2] == ["ssh-key", "add"]][0]
        self.assertIn("--title=--type=signing", argv)
        self.assertEqual(argv[argv.index("--type") + 1], "authentication")

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
        self.assertTrue(argv[-1].startswith("sh -s -- "), "F11: the script runs under sh, not the login shell")

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

    def test_the_words_added_with_a_failing_exit_code_are_a_failure(self):
        """F11: a remote whose shell broke the script still printed 'added'."""
        self.plant("id_alpha")
        p = self.door("send", "id_alpha", "--to", "server:alpha", env={"FAKE_SSH_WORDS_LIE": "1"})
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertNotIn("added to", p.stdout)
        self.assertEqual([r["event"] for r in self.receipts()], ["ssh.register_failed"])

    def test_added_is_decided_by_the_exit_code_not_the_words(self):
        """F11: a broken remote that still prints 'added' is a failure."""
        self.plant("id_alpha")
        (self.remote / ".ssh").mkdir(mode=0o700)
        (self.remote / ".ssh" / "authorized_keys").write_text("")
        (self.remote / ".ssh" / "authorized_keys").chmod(0o400)   # the append fails
        try:
            p = self.door("send", "id_alpha", "--to", "server:alpha")
        finally:
            (self.remote / ".ssh" / "authorized_keys").chmod(0o600)
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertEqual([r["event"] for r in self.receipts()], ["ssh.register_failed"])

    def test_verify_logs_in_with_the_new_key_alone(self):
        blob = self.plant("id_alpha")
        p = self.door("send", "id_alpha", "--to", "server:alpha", "--verify")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.calls("ssh")[1][:1], ["-G"])
        verify = self.calls("ssh")[-1]
        for flag in ("IdentitiesOnly=yes", "PreferredAuthentications=publickey", "IdentityAgent=none"):
            self.assertIn(flag, verify)
        self.assertEqual(verify[verify.index("-F") + 1], "none")
        self.assertEqual(verify[verify.index("-i") + 1], str(self.ssh / "id_alpha"))
        self.assertEqual(verify[-1], "true")
        self.assertEqual([r["event"] for r in self.receipts()], ["ssh.registered", "ssh.verified"])
        self.assertIn(fingerprint(blob), p.stdout)

    def test_verify_cannot_pass_on_another_key_from_the_config(self):
        """F1: `Host box` names the old key; the server holds only the old key. The new key must
        not be reported as accepted."""
        self.plant("id_old")
        self.plant("id_new")
        (self.ssh / "config").write_text("Host box\n  HostName box.example.invalid\n  User deploy\n"
                                          "  IdentityFile ~/.ssh/id_old\n  IdentitiesOnly yes\n")
        (self.remote / ".ssh").mkdir(mode=0o700)
        (self.remote / ".ssh" / "authorized_keys").write_text(pub_line(ed25519_blob("id_old"), "old") + "\n")
        p = self.door("send", "id_new", "--to", "server:box", "--verify", env={"FAKE_SSH_SWALLOW": "1"})
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertNotIn("Verified", p.stdout)
        self.assertEqual(self.receipts()[-1]["event"], "ssh.verify_failed")
        verify = self.calls("ssh")[-1]
        self.assertEqual(verify[verify.index("-p") + 1], "22")
        self.assertEqual(verify[verify.index("-l") + 1], "deploy")
        self.assertEqual(verify[verify.index("--") + 1], "box.example.invalid")

    def test_verify_requires_the_server_to_name_the_new_key(self):
        """F1, second guard: an ssh that still offers the config's key is caught by fingerprint."""
        self.plant("id_old")
        self.plant("id_new")
        (self.ssh / "config").write_text("Host box\n  HostName box.example.invalid\n  IdentityFile ~/.ssh/id_old\n")
        (self.remote / ".ssh").mkdir(mode=0o700)
        (self.remote / ".ssh" / "authorized_keys").write_text(pub_line(ed25519_blob("id_old"), "old") + "\n")
        p = self.door("send", "id_new", "--to", "server:box", "--verify",
                      env={"FAKE_SSH_SWALLOW": "1", "FAKE_SSH_IGNORE_F": "1"})
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn(fingerprint(ed25519_blob("id_old")), p.stderr)
        self.assertNotIn("Verified", p.stdout)
        self.assertEqual(self.receipts()[-1]["event"], "ssh.verify_failed")

    def test_verify_without_the_servers_word_on_the_key_is_not_verified(self):
        self.plant("id_alpha")
        p = self.door("send", "id_alpha", "--to", "server:alpha", "--verify", env={"FAKE_SSH_NO_ACCEPT_LINE": "1"})
        self.assertEqual(p.returncode, 1)
        self.assertEqual(self.receipts()[-1]["event"], "ssh.verify_failed")

    def test_a_jump_host_keeps_its_user_and_port(self):
        """M22: ProxyJump jumper@hop:2200 reaches ssh as exactly that."""
        self.plant("id_alpha")
        (self.ssh / "config").write_text("Host box\n  HostName box.example.invalid\n"
                                          "  ProxyJump jumper@hop.example.invalid:2200\n")
        p = self.door("send", "id_alpha", "--to", "server:box", "--verify")
        self.assertEqual(p.returncode, 0, p.stderr)
        verify = self.calls("ssh")[-1]
        self.assertEqual(verify[verify.index("-J") + 1], "jumper@hop.example.invalid:2200")

    def test_an_agent_key_is_not_offered_during_verify(self):
        """The agent holds the old key the server accepts; verify of the new key must fail."""
        self.plant("id_old")
        self.plant("id_new")
        (self.remote / ".ssh").mkdir(mode=0o700)
        (self.remote / ".ssh" / "authorized_keys").write_text(pub_line(ed25519_blob("id_old"), "old") + "\n")
        p = self.door("send", "id_new", "--to", "server:box", "--verify",
                      env={"FAKE_SSH_SWALLOW": "1", "FAKE_AGENT_IDENTS": str(self.ssh / "id_old")})
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertNotIn("Verified", p.stdout)

    def test_verify_never_lets_the_agent_sign_for_an_unproven_private_file(self):
        """Round 3 MEDIUM: `new` is PKCS#8-encrypted (no public key in clear), `new.pub` holds key
        B's line and the agent holds B; the server accepts B. Verify must not pass."""
        b = self.plant("id_b")
        self.ssh.mkdir(mode=0o700, exist_ok=True)
        pk8 = "ENCRYPTED " + "PRIVATE" + " KEY-----"
        (self.ssh / "id_new").write_text(BEGIN + pk8 + "\nMIIFHDBOBgkq" + "A" * 60 + "\n" + END + pk8 + "\n")
        (self.ssh / "id_new").chmod(0o600)
        (self.ssh / "id_new.pub").write_text(pub_line(b, "new") + "\n")
        (self.remote / ".ssh").mkdir(mode=0o700)
        (self.remote / ".ssh" / "authorized_keys").write_text(pub_line(b, "b") + "\n")
        p = self.door("send", "id_new", "--to", "server:box", "--verify",
                      env={"FAKE_SSH_SWALLOW": "1", "FAKE_AGENT_IDENTS": str(self.ssh / "id_b")})
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertNotIn("Verified", p.stdout)
        self.assertIn("--interactive", p.stderr)
        verify = self.calls("ssh")[-1]
        self.assertIn("IdentityAgent=none", verify)

    def test_a_proven_key_with_a_passphrase_may_use_the_agent(self):
        """The other side: the header proves the file is the .pub's key, so the agent may sign."""
        blob = self.plant("id_locked", cipher="aes256-ctr")
        p = self.door("send", "id_locked", "--to", "server:box", "--verify",
                      env={"FAKE_AGENT_IDENTS": str(self.ssh / "id_locked")})
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("alone", p.stdout)
        self.assertNotIn("IdentityAgent=none", self.calls("ssh")[-1])
        self.assertIn(fingerprint(blob), p.stdout)

    def test_a_proxy_command_host_is_not_verified_blind(self):
        self.plant("id_alpha")
        (self.ssh / "config").write_text("Host hop\n  ProxyCommand nc %h %p\n")
        p = self.door("send", "id_alpha", "--to", "server:hop", "--verify")
        self.assertEqual(p.returncode, 1)
        self.assertIn("ProxyCommand", p.stderr)

    def test_a_failed_verification_says_the_key_was_added(self):
        self.plant("id_alpha")
        p = self.door("send", "id_alpha", "--to", "server:alpha", "--verify", env={"FAKE_SSH_SWALLOW": "1"})
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
    def assert_information_only(self, row: dict):
        """Round 3 decision: never a delete, never a command, never an action, always info."""
        self.assertEqual(row["severity"], "info")
        self.assertNotIn("action", row)
        text = json.dumps(row)
        self.assertNotIn("gh ssh-key delete", text)
        self.assertNotIn("`", row["detail"], "no command of any kind")
        self.assertIn("remove it in GitHub settings", row["detail"])
        self.assertIn("did not find in the places it read", row["detail"])

    def test_a_complete_inventory_still_never_advises_deleting(self):
        import ssh_key_findings
        doc = self.doc()
        for k in doc["keys"]:
            k.setdefault("fingerprint", "SHA256:x")
        rows = [r for r in ssh_key_findings.findings(doc) if r["type"] == "ssh.github_orphan"]
        self.assertEqual(len(rows), 1)
        self.assert_information_only(rows[0])

    def doc(self) -> dict:
        now = "2026-10-15T00:00:00Z"
        base = {"private": True, "public": True, "link": False, "private_ok": True, "private_mode": "0600",
                "passphrase": "yes", "in_agent": False, "github": [], "last_access": now, "hosts": []}
        return {"schema": "ssh-keys/1", "measured_at": now,
                "folder": {"path": "~/.ssh", "exists": True, "mode": "0755", "private": False, "link": False},
                "keys": [
                    {**base, "name": "id_open", "path": "~/.ssh/id_open", "type": "ED25519", "bits": 256,
                     "fingerprint": "SHA256:open", "passphrase": "no", "private_ok": False,
                     "private_mode": "0640", "last_access": "2025-01-01T00:00:00Z"},
                    {**base, "name": "id_old", "path": "~/.ssh/id_old", "type": "RSA", "bits": 2048,
                     "fingerprint": "SHA256:old", "hosts": ["alpha"], "in_agent": True, "github": ["authentication"]},
                    {**base, "name": "id_dsa", "path": "~/.ssh/id_dsa", "type": "DSA", "bits": 1024,
                     "fingerprint": "SHA256:dsa", "hosts": ["beta"]},
                    {**base, "name": "id_card", "path": "~/.ssh/id_card", "type": "ED25519-SK", "bits": 256,
                     "fingerprint": "SHA256:card", "passphrase": "no", "hosts": ["gamma"]},
                    {**base, "name": "id_lonely", "path": "~/.ssh/id_lonely", "public": False,
                     "type": "ED25519", "bits": 256, "fingerprint": "SHA256:lonely", "hosts": ["delta"],
                     "in_agent": None, "github": None}],
                "agent_only": [], "misplaced": [], "unseen": [],
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
            "ssh.key_no_passphrase", "ssh.key_no_passphrase", "ssh.key_readable", "ssh.key_unused",
            "ssh.key_unregistered", "ssh.key_weak", "ssh.key_weak", "ssh.folder_permissions",
            "ssh.public_missing", "ssh.identity_missing", "ssh.github_orphan"]))
        weak = {r["subject"]: r["severity"] for r in rows if r["type"] == "ssh.key_weak"}
        self.assertEqual(weak, {"ssh:SHA256:old": "warning", "ssh:SHA256:dsa": "critical"})
        bare = {r["subject"]: r for r in rows if r["type"] == "ssh.key_no_passphrase"}
        self.assertEqual(bare["ssh:SHA256:open"]["severity"], "warning")
        self.assertEqual(bare["ssh:SHA256:card"]["severity"], "info", "F8: a FIDO handle is not a usable key")
        self.assertIn("security key", bare["ssh:SHA256:card"]["detail"])
        self.assertNotIn("anyone who copies", bare["ssh:SHA256:card"]["detail"])
        for r in rows:
            self.assertTrue(r["title_id"] and r["evidence"], r)
            self.assertTrue(r.get("action") or r["type"] == "ssh.github_orphan", r)
        orphan = next(r for r in rows if r["type"] == "ssh.github_orphan")
        self.assert_information_only(orphan)
        self.assertEqual(rows, ssh_key_findings.findings(self.doc()), "the same inventory gave other rows")

    def test_an_unknown_local_fingerprint_makes_an_orphan_info_without_a_delete(self):
        """F2: while a private key's fingerprint is unknown, no GitHub key is proven foreign."""
        import ssh_key_findings
        doc = self.doc()
        doc["keys"][4].update(fingerprint=None)
        orphan = next(r for r in ssh_key_findings.findings(doc) if r["type"] == "ssh.github_orphan")
        self.assert_information_only(orphan)
        self.assertIn("id_lonely", orphan["detail"])

    def test_permissions_come_from_osprivacy_not_raw_mode_bits(self):
        """F3: on Windows every file reports 0o666; only osprivacy's answer counts."""
        import osprivacy
        import ssh_keys
        import ssh_key_findings
        self.plant("id_alpha", mode=0o666)
        os.chmod(self.ssh, 0o777)
        try:
            with mock.patch.object(osprivacy, "WINDOWS", True), \
                    mock.patch.object(osprivacy, "private", lambda target: True), \
                    mock.patch.object(osprivacy, "describe", lambda target: "owner only"):
                doc = self.in_process(ssh_keys.inventory, github=False)
        finally:
            os.chmod(self.ssh, 0o700)
        self.assertEqual((self.key(doc, "id_alpha")["private_ok"], doc["folder"]["private"]), (True, True))
        types = {r["type"] for r in ssh_key_findings.findings(doc)}
        self.assertNotIn("ssh.key_readable", types)
        self.assertNotIn("ssh.folder_permissions", types)

    def test_a_group_readable_key_is_flagged(self):
        """F12 M9: 0640 is readable by the group."""
        self.plant("id_alpha", mode=0o640)
        self.assertEqual(self.door("list", "--no-github").returncode, 0)
        import ssh_key_findings
        rows = ssh_key_findings.from_file(self.scratch / "ssh-keys.json")
        self.assertIn("ssh.key_readable", {r["type"] for r in rows})

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
        self.assertEqual(len(json.loads(p.stdout)["findings"]), 11)


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
        one = self.in_process(ssh_keys.agent_view, name="id_alpha")
        unknown = self.in_process(ssh_keys.agent_view, name="id_nope")
        traversal = self.in_process(ssh_keys.agent_view, name="../x")
        shaped = self.in_process(ssh_keys.agent_view, name="gh" + "p_" + "Q7m2Xz9R4tLbK8wN1vPc5jD6sAu9f3Qm2XzR")
        every = self.in_process(ssh_keys.agent_view)
        self.assertEqual(one["public"], pub_line(blob, "id_alpha"))
        self.assertIn("ssh-keys copy id_alpha", one["commands"]["copy"])
        self.assertIn("error", unknown)
        self.assertIn("error", traversal)
        self.assertIn("error", shaped)
        self.assertNotIn("Q7m2Xz9R", json.dumps(shaped))
        self.assertIn("degraded", every)
        self.assertTrue(any("GitHub" in d["reason"] or d["source"].startswith("github") for d in every["degraded"]))


if __name__ == "__main__":
    unittest.main()
