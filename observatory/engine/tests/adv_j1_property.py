#!/usr/bin/env python3
"""Adversarial extension of test_or_policy_property (J-1, PR #246 @ 6f30de4). Scratch only.

Fakes only: no network, no provider, no secret values (each minted key has a distinct synthetic plaintext and its real SHA256).

Invariants, measured on an EXACT provider (Decimal of what it stores, sub-cent included):
 (a)  a write that raises the project's provider total leaves it within the limit — except inside
      rotate, whose overlap is documented; after rotate ends (returned, failed or killed) the
      total is within max(limit, total before rotate).
 (L)  no live project key is unknown to the ledger (hash, predecessor hash, or a pending row
      without a hash that names it).
 (b)  whole-cent planned ceilings that fit are never refused on a clean project.
 (c') after the failures stop, applying the remedies the door itself names (revoke X; delete
      at the provider then revoke X for a hashless pending row) serves every slot, the run exits 0
      and the run after it writes nothing.
 (R)  revoke never deletes a key a consumer reads per the scan (by hash, by name, or by label).
"""
from __future__ import annotations
import contextlib
import io
import hashlib
import json
import pathlib
import random
import sys
import traceback
from decimal import Decimal

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import test_credential_doors as T          # noqa: E402  (sandbox + loader)
import private_io                           # noqa: E402
import tmp as tmpdir                        # noqa: E402

LIMIT_INF = Decimal("Infinity")


class Killed(BaseException):
    pass


class Prov:
    """Exact fake OpenRouter with offset paging, foreign filler keys, page drift, injected faults."""

    def __init__(self, rng, unreachable, echo, page, pages_max, fillers, drift, newest_first):
        self.U = unreachable
        self.echo, self.page, self.drift, self.newest_first = echo, page, drift, newest_first
        self.keys: dict[str, dict] = {}
        self.order: list[str] = []
        for i in range(fillers):
            h = f"f{i}"
            self.keys[h] = {"hash": h, "name": f"user-{i}", "limit": 1.0, "limit_reset": "monthly",
                            "disabled": False, "label": f"sk-or-v1-fff...{i:03d}", "_proj": False}
            self.order.append(h)
        self.n, self.unread, self.fail = 0, set(), {}
        self.calls, self.writes = 0, 0
        self.limit = None                 # Decimal cents of the run in progress
        self.in_rotate = 0
        self.violations: list[str] = []
        self.hook = None                  # (call_no, fn): run fn re-entrantly at that call

    # exact project total in Decimal dollars
    def total(self) -> Decimal:
        t = Decimal(0)
        for r in self.keys.values():
            if not r["_proj"]:
                continue
            lim = r.get("_asked", r["limit"])
            if lim is None:
                return LIMIT_INF
            t += Decimal(repr(float(lim)))
        return t

    def _fault(self, kind):
        f = self.fail.get(kind)
        if not f:
            return None
        f[0] -= 1
        if f[0] == 0:
            del self.fail[kind]
            return f[1]
        return None

    def listing(self, offset):
        order = list(reversed(self.order)) if self.newest_first else list(self.order)
        start = offset
        if self.drift and offset:
            start = max(0, offset + self.drift)   # -1: a duplicate across pages; +1: a skipped row
        return [self.public(self.keys[h]) for h in order[start:start + self.page]]

    @staticmethod
    def public(r):
        return {k: v for k, v in r.items() if not k.startswith("_")}

    def __call__(self, path, key, payload=None, method=None):
        self.calls += 1
        if self.hook and self.hook[0] == self.calls:
            fn, self.hook = self.hook[1], None
            fn()
        before = self.total()
        if path.startswith("/keys?"):
            off = int(path.rsplit("offset=", 1)[-1])
            return {"data": self.listing(off)}
        if path == "/keys" and payload:
            mode = self._fault("mint")
            if mode == "500-before":
                raise RuntimeError("openrouter answered HTTP 500; provider response withheld")
            if mode == "unreach-before":
                raise self.U("openrouter unreachable: TimeoutError")
            self.n += 1
            value = f"synthetic-adversary-key-{self.n:08d}"
            h = hashlib.sha256(value.encode()).hexdigest()
            self.keys[h] = {"hash": h, "name": payload["name"], "limit": self.echo(payload["limit"]),
                            "limit_reset": payload["limit_reset"], "disabled": False,
                            "label": f"sk-or-v1-abc...{self.n:03d}", "_proj": payload["name"].startswith("project-a"),
                            "_asked": payload["limit"]}
            self.order.append(h)
            self._after("POST /keys", before)
            if mode == "502-after":
                raise RuntimeError("openrouter answered HTTP 502; provider response withheld")
            if mode == "unreach-after":
                raise self.U("openrouter unreachable: TimeoutError")
            return {"data": self.public(self.keys[h]), "key": value}
        h = path.rsplit("/", 1)[-1]
        if h in self.unread and method is None:
            raise self.U("openrouter unreachable: TimeoutError")
        if h not in self.keys:
            raise RuntimeError("openrouter answered HTTP 404; provider response withheld")
        if method == "DELETE":
            if self._fault("delete"):
                raise RuntimeError("openrouter answered HTTP 502; provider response withheld")
            del self.keys[h]
            self.order.remove(h)
            self.writes += 1
            return {}
        if method == "PATCH":
            if "limit" in payload:
                old = self.keys[h]["limit"]
                kind = "lower" if old is None or payload["limit"] < old else "raise"
                if self._fault(kind):
                    raise RuntimeError("openrouter answered HTTP 500; provider response withheld")
                self.keys[h]["limit"] = self.echo(payload["limit"])
                self.keys[h]["_asked"] = payload["limit"]
                self.keys[h]["limit_reset"] = payload["limit_reset"]
            for k in ("name", "disabled"):
                if k in payload:
                    self.keys[h][k] = payload[k]
            self._after(f"PATCH {path}", before)
        return {"data": self.public(self.keys[h])}

    def _after(self, what, before):
        self.writes += 1
        after = self.total()
        if self.in_rotate or self.limit is None:
            return
        lim = Decimal(self.limit) / 100
        if after > before and after > lim:
            self.violations.append(f"(a) {what} took the project total to {after} past {lim}")


ECHOES = [("id", lambda x: x), ("+1e-9", lambda x: x + 1e-9), ("round2", lambda x: round(x, 2)),
          ("+0.004", lambda x: round(x + 0.004, 3)), ("+0.0004", lambda x: round(x + 0.0004, 4)),
          ("-0.004", lambda x: max(0.001, round(x - 0.004, 3))), ("x1.0001", lambda x: x * 1.0001)]


def make_door(rng):
    m = T.orr()
    d = pathlib.Path(tmpdir.mkdtemp()).resolve()
    m.ADMIN_STORE = d / "admin"
    m.ADMIN_STORE.mkdir(parents=True)
    private_io.write(m.ADMIN_STORE / "example", "prov\n")
    m.LEDGER = d / "ledger.json"
    m._project_folder = lambda project, env, name: {"a": "project-a"}.get(project, project)
    m.vault_destination = lambda to: to
    journal = []
    m._journal = lambda *a, **k: journal.append((a,k))
    m.POLICY_LOCK_WAIT_S = 0.01
    scratch = d / "scratch"
    scratch.mkdir()
    m.paths.SCRATCH = scratch
    big = rng.random() < 0.4
    m.PAGE, m.PAGES_MAX = rng.choice([(3, 2), (5, 3), (4, 4)])
    fillers = (m.PAGE * m.PAGES_MAX + rng.randint(0, 4)) if big else rng.randint(0, m.PAGE)
    ename, echo = rng.choice(ECHOES)
    p = Prov(rng, m.Unreachable, echo, m.PAGE, m.PAGES_MAX, fillers, rng.choice([0, 0, -1, 1]),
             rng.random() < 0.5)
    m._request = p
    door = {"deliver": None, "save": None, "on_deliver": None, "journal": journal}
    real_save = m.save_ledger

    def deliver(value, to, **k):
        if door["on_deliver"]:
            fn, door["on_deliver"] = door["on_deliver"], None
            fn()
        f, door["deliver"] = door["deliver"], None
        if f == "deliver":
            raise OSError("the vault refused the put")
        if f == "deliver-timeout":
            raise __import__("subprocess").TimeoutExpired(["vault.py", "put"], 60)
        if f == "kill":
            raise Killed()
        return f"vault {to}"

    def save(doc):
        f = door["save"]
        if f is not None:
            f[0] -= 1
            if f[0] == 0:
                door["save"] = None
                raise OSError("the disk refused the ledger write")
        return real_save(doc)
    m.deliver, m.save_ledger = deliver, save
    return m, p, door, {"echo": ename, "big": big, "drift": p.drift, "page": (m.PAGE, m.PAGES_MAX),
                        "fillers": fillers, "newest_first": p.newest_first}


def policy(rng, refs, limit_cents):
    n = len(refs)
    mode = rng.choice(["equal", "equal", "split", "limit"])
    kw, planned = {}, None
    if mode == "equal" and limit_cents // n >= 1:
        planned = [limit_cents // n] * n
    elif mode == "split" and limit_cents >= n:
        cuts = sorted(rng.sample(range(1, limit_cents + 1), n)) if limit_cents > n else list(range(1, n + 1))
        planned = [cuts[0]] + [b - a for a, b in zip(cuts, cuts[1:])]
        kw["split"] = {r: c / 100 for r, c in zip(refs, planned)}
    elif mode == "limit" and limit_cents // n >= 1:
        c = rng.randint(1, limit_cents // n)
        planned, kw["limit_override"] = [c] * n, c / 100
    pol = {"kind": "spend-policy", "project": "urn:project:a", "mode": "limited",
           "limitUsd": limit_cents / 100, "period": "month",
           "credentialSources": [{"kind": "project-key", "ref": r} for r in refs]}
    return pol, kw, planned


def invalid_policy(pol, limit_cents):
    # The generator's invalid-input partition: every source needs at least one cent.
    return limit_cents < len(pol["credentialSources"])


def run(m, p, pol, kw, limit_cents):
    p.limit = limit_cents
    invalid = invalid_policy(pol, limit_cents)
    before = (json.dumps(p.keys, sort_keys=True), json.dumps(m.ledger(), sort_keys=True), p.writes) if invalid else None
    try:
        result = m.issue_for_policy(pol, "example", **kw), None
    except Killed:
        result = None, "killed"
    except Exception as exc:                # noqa: BLE001
        result = getattr(exc, "plan", None), f"{type(exc).__name__}: {exc}"
    if invalid:
        after = (json.dumps(p.keys, sort_keys=True), json.dumps(m.ledger(), sort_keys=True), p.writes)
        if before != after:
            p.violations.append("(invalid) rejected sub-cent policy changed provider or ledger state")
        if result[0] is not None or not (result[1] or "").startswith("ValueError:"):
            p.violations.append(f"(invalid) sub-cent policy did not refuse with ValueError: {result[1]}")
    return result


def rotate(m, p, name, limit_cents):
    before = p.total()
    p.in_rotate += 1
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = m.cmd_rotate(name, False)
        out = f"rc={rc}"
    except Killed:
        out = "killed"
    finally:
        p.in_rotate -= 1
    lim = Decimal(limit_cents) / 100
    after = p.total()
    if after > max(lim, before):
        p.violations.append(f"(a-rot) rotate {name} ({out}) left the total {after}, past the limit {lim} "
                            f"and above {before} before it")
    return out


def revoke(m, name):
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return m.cmd_revoke(name)
    except Killed:
        return "killed"



def refresh_consumers(m, p, consumers, *, listed=True):
    """Run the real producer over bounded, read-only synthetic observations.

    Only explicit consumer-label events populate this model's destination survey;
    delivery alone does not assert a running holder. Keep observations through
    convergence. The finite fake snapshot is not real pagination/remote coverage.
    No door calls, RNG draws, hooks, writes or credentials enter this adapter.
    """
    import configuration
    from unittest.mock import patch
    scanner = T.load("collectors/scan_openrouter.py", "j3_consumer_producer")
    rows = [p.public(row) for row in p.keys.values()]
    if len(rows) > 1000 or len(consumers) > 1000:
        raise RuntimeError("ENVIRONMENT synthetic consumer inventory exceeds model bound")
    def by_hash(_admin, handle):
        row = p.keys.get(handle)
        return (p.public(row), None, None) if row else (None, 404, "HTTP 404")
    out = m.paths.SCRATCH / "openrouter.json"
    with patch.object(configuration, "enabled", return_value=True), \
         patch.object(scanner, "DESTINATIONS", {"provisioning": m.ADMIN_STORE / "example"}), \
         patch.object(scanner, "destinations", return_value=(dict(consumers), [], [])), \
         patch.object(scanner, "listing", return_value=(rows if listed else [], None)), \
         patch.object(scanner, "key_by_hash", side_effect=by_hash), \
         patch.object(scanner, "_get", side_effect=AssertionError("synthetic source only")), \
         contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        if scanner.main(["scan_openrouter.py", str(out)]) != 0:
            raise RuntimeError("ENVIRONMENT synthetic consumer producer failed")
    return json.loads(out.read_text())


def remedy_revoke(m, p, name, consumers):
    refresh_consumers(m, p, consumers)
    return revoke(m, name)



def _fixture_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def limit_remedies(m, p, plan):
    """Translate only the door's emitted decrease into a source-bound test proposal.

    These checks are not a production CAS: there is no nested policy lock, and
    external writers between the check and command remain outside this fixture.
    Reported public metadata is authoritative here; never inspect hidden _asked.
    """
    import math, re
    try:
        doc = m.ledger(); ledger_digest = _fixture_digest(doc); plan_digest = _fixture_digest(plan)
        if not isinstance(plan, list): return []
        proposals = []
        for warning in plan:
            if warning.get('action') != 'over-limit': continue
            tail = warning.get('why', '').partition('it ends after: ')[2]
            for command in tail.split(', '):
                match = re.fullmatch(r'limit ([a-z0-9_-]+) --set ([0-9]+(?:\.[0-9]+)?(?:e[+-]?[0-9]+)?)', command)
                if not match: continue
                name, amount = match.group(1), float(match.group(2))
                slots = [r for r in plan if r.get('name') == name and r.get('action') == 'in-place']
                rec = doc['issued'].get(name); handle = rec.get('hash') if isinstance(rec, dict) else None
                row = p.keys.get(handle)
                if len(slots) != 1 or type(slots[0].get('limit')) not in (int,float) or slots[0]['limit'] != amount: continue
                if not isinstance(handle,str) or not handle or not isinstance(row,dict) or row.get('hash') != handle: continue
                current = row.get('limit')
                if type(current) not in (int,float) or not math.isfinite(current) or not 0 < amount < current: continue
                if not math.isfinite(amount): continue
                proposal = {'kind':'emitted-limit-decrease','name':name,'hash':handle,'amount':amount,
                            'ledger_digest':ledger_digest,'plan_digest':plan_digest}
                if proposal not in proposals: proposals.append(proposal)
        return proposals
    except (ValueError,TypeError,KeyError,AttributeError): return []


def apply_limit_remedy(m, p, plan, proposal):
    if proposal not in limit_remedies(m,p,plan): return {'status':'refused'}
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = m.cmd_limit(proposal['name'],proposal['amount'],None)
    except Exception:
        return {'status':'unresolved'}
    return {'status':'operator-applied' if rc == 0 else 'unresolved'}


def pending_absence_attestation(m, p, name, consumers, *, complete):
    """An explicit synthetic operator observation, never a bounded-lookup inference.

    The finite model's complete inventory is an assumption for this operator
    event only. Production accepts an attestation, not this simulated proof.
    """
    try:
        doc = m.ledger(); rec = doc['issued'].get(name)
        if complete is not True or consumers or not isinstance(consumers,dict): return None
        if not isinstance(rec,dict) or rec.get('pending') is not True or rec.get('hash'): return None
        if rec.get('predecessor_hash') or rec.get('successor_hash') or rec.get('rotation_status'): return None
        if not isinstance(rec.get('account'),str) or not rec['account']: return None
        if not isinstance(p.keys,dict) or len(p.keys) > 1000: return None
        if any(not isinstance(row,dict) or not isinstance(row.get('name'),str) or
               not isinstance(row.get('hash'),str) or row['hash'] != handle
               for handle,row in p.keys.items()): return None
        if any(row['name'] == name for row in p.keys.values()): return None
        inventory = {handle:p.public(row) for handle,row in p.keys.items()}
        return {'kind':'synthetic-operator-absence','name':name,'account':rec['account'],
                'ledger_digest':_fixture_digest(doc),'inventory_digest':_fixture_digest(inventory),
                'reason':'Synthetic operator observed complete finite inventory after manual cleanup; matching identity absent'}
    except (ValueError,TypeError,KeyError,AttributeError): return None


def apply_pending_attestation(m, p, name, consumers, proof, *, complete):
    if proof is None or proof != pending_absence_attestation(m,p,name,consumers,complete=complete):
        return {'status':'refused'}
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = m.cmd_revoke(name,forget_pending=True,reason=proof['reason'])
    except Exception:
        return {'status':'unresolved'}
    return {'status':'operator-attested' if rc == 0 else 'unresolved'}


def representation(m, p):
    """Synthetic observer: explicit identities differ from bounded unknown reservations.

    A reservation matches at most one provider row within its declared capacity.
    It accounts for uncertainty, not identity proof, delivery or convergence. No
    production recovery action is authorized by this observer's name comparison.
    """
    try:
        issued = m.ledger()["issued"]
        if not isinstance(issued, dict) or any(not isinstance(r, dict) for r in issued.values()):
            raise ValueError
    except Exception:
        raise RuntimeError("ENVIRONMENT ledger could not be inspected; invariant unjudged") from None
    known = {r.get(field) for r in issued.values() for field in ("hash", "predecessor_hash", "successor_hash")
             if isinstance(r.get(field), str) and r.get(field)}
    reservations = []
    for name, row in issued.items():
        if row.get("pending") and not row.get("hash"):
            reservations.append((name, row.get("limit_usd")))
        if row.get("rotation_status") == "mint-pending" and not row.get("successor_hash"):
            reservations.append((name + "-next", row.get("successor_limit_usd")))
    reserved, missing = [], []
    for handle, row in p.keys.items():
        if not row["_proj"] or handle in known: continue
        match = None
        for index, (name, capacity) in enumerate(reservations):
            if name != row['name'] or type(capacity) not in (int, float): continue
            try:
                cap = Decimal(str(capacity)); actual = Decimal(str(row.get('_asked', row['limit'])))
                fits = cap.is_finite() and actual.is_finite() and cap > 0 and 0 <= actual <= cap
            except Exception: fits = False
            if fits: match = index; break
        if match is not None:
            reservations.pop(match); reserved.append(handle)
        else:
            missing.append(f"{handle}({row['name']}, ${row['limit']})")
    return {"explicit": sorted(known), "reserved_unknown": reserved, "untracked": missing}


def untracked(m, p):
    return representation(m, p)["untracked"]


def serves_every_slot(plan, refs):
    """A returned plan is not CLI success: held-rotation returns a plan, too."""
    if not isinstance(plan, list) or any(not isinstance(row, dict) for row in plan): return False
    if any(row.get('action') not in ('in-place', 'ceiling-moved', 'issued') for row in plan): return False
    return set(refs) <= {row.get('ref') for row in plan if row.get('to')}


def case(seed, trace=False):
    rng = random.Random(seed)
    broke: list[str] = []
    log: list[str] = []
    m, p, door, shape = make_door(rng)
    log.append(f"shape {shape}")
    scale = rng.choice([100, 10_000, 1_000_000])
    limit_cents = rng.randint(1, scale)
    names = [f"vault:project-a/prod/K{j}" for j in range(8)]
    refs = rng.sample(names, rng.randint(1, 6))
    pol, kw, planned = policy(rng, refs, limit_cents)
    plan, err = run(m, p, pol, kw, limit_cents)
    log.append(f"run1 limit={limit_cents} refs={len(refs)} kw={kw} -> {err} {[s.get('action') for s in plan or []]}")
    if planned is not None and sum(planned) <= limit_cents:
        if err is not None or any(s.get("action") == "blocked" for s in plan or []):
            broke.append(f"(b) {sum(planned)} planned under {limit_cents} refused: {err}")
    protected: set[str] = set()
    consumers: dict[str, str] = {}

    def lname(ref):
        return T.orr.__globals__ and m.slug(f"project-a-{ref.split('/')[1]}-{ref.split('/')[2]}")

    for step in range(rng.randint(3, 7)):
        ev = rng.choice(["rename", "subcent", "unread", "delete404", "edit", "failrun", "failrun",
                         "rotate", "rotate-kill", "concurrent", "consumer-label", "revoke-remedy"])
        issued = m.ledger()["issued"]
        live = [h for h, r in p.keys.items() if r["_proj"]]
        if ev == "rename" and live:
            h = rng.choice(live)
            p.keys[h]["name"] = f"renamed-{h}"
            log.append(f"rename {h}")
        elif ev == "subcent" and live:
            h = rng.choice(live)
            if p.keys[h]["limit"] is not None:
                p.keys[h]["limit"] = round(p.keys[h]["limit"] + rng.choice([0.004, -0.004, 0.0001, 0.006]), 4)
                if p.keys[h]["limit"] <= 0:
                    p.keys[h]["limit"] = 0.001
                p.keys[h]["_asked"] = p.keys[h]["limit"]
            log.append(f"subcent {h} -> {p.keys[h]['limit']}")
        elif ev == "unread" and live:
            h = rng.choice(live)
            p.unread.add(h)
            log.append(f"unread {h}")
        elif ev == "delete404" and live:
            h = rng.choice(live)
            del p.keys[h]
            p.order.remove(h)
            log.append(f"provider deleted {h}")
        elif ev == "edit":
            refs = [r for r in refs if rng.random() > 0.25] or refs[:1]
            refs += [r for r in rng.sample(names, rng.randint(0, 2)) if r not in refs]
            limit_cents = max(1, min(scale, int(limit_cents * rng.choice([0.5, 1, 2]))))
            pol, kw, _ = policy(rng, refs, limit_cents)
            log.append(f"edit limit={limit_cents} refs={len(refs)} kw={kw}")
        elif ev == "failrun":
            kind = rng.choice(["lower", "raise", "mint:500-before", "mint:502-after", "mint:unreach-before",
                               "mint:unreach-after", "deliver", "deliver-timeout", "kill", "save", "delete"])
            if kind.startswith("mint:"):
                p.fail["mint"] = [rng.randint(1, 2), kind[5:]]
            elif kind in ("lower", "raise", "delete"):
                p.fail[kind] = [rng.randint(1, 2), True]
            elif kind == "save":
                door["save"] = [rng.randint(1, 4)]
            else:
                door["deliver"] = kind
            plan, err = run(m, p, pol, kw, limit_cents)
            log.append(f"failrun {kind} -> {err} {[s.get('action') for s in plan or []]}")
        elif ev in ("rotate", "rotate-kill") and issued:
            n = rng.choice(sorted(issued))
            if ev == "rotate-kill":
                k = rng.choice(["kill", "deliver", "mint:502-after", "mint:unreach-after", "delete"])
                if k.startswith("mint:"):
                    p.fail["mint"] = [1, k[5:]]
                elif k == "delete":
                    p.fail["delete"] = [rng.randint(1, 2), True]
                else:
                    door["deliver"] = k
            else:
                k = "-"
            out = rotate(m, p, rng.choice([n, None]), limit_cents)
            log.append(f"{ev} {n} fault={k} -> {out}")
        elif ev == "concurrent":
            other = rng.choice(["policy", "rotate", "issue", "limit"])
            free = [r for r in names if r not in refs]

            def interleave(other=other):
                cur = m.ledger()["issued"]
                if other == "policy":
                    r = run(m, p, pol, kw, limit_cents)
                    log.append(f"  concurrent policy -> {r[1]}")
                elif other == "rotate" and cur:
                    log.append(f"  concurrent rotate -> {rotate(m, p, rng.choice(sorted(cur)), limit_cents)}")
                elif other == "issue" and free:
                    try:
                        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                            rc = m.cmd_issue(f"side-{rng.randint(0, 9)}", 0.01, "example",
                                             "vault:other/prod/X", "other")
                        log.append(f"  concurrent issue (another project) rc={rc}")
                    except Killed:
                        pass
                elif other == "limit" and cur:
                    n = rng.choice(sorted(cur))
                    try:
                        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                            rc = m.cmd_limit(n, 0.01, None)
                        log.append(f"  concurrent limit {n} 0.01 rc={rc}")
                    except Killed:
                        pass
            if rng.random() < 0.5:
                door["on_deliver"] = interleave
            else:
                p.hook = (p.calls + rng.randint(1, 6), interleave)
            first = rng.choice(["policy", "rotate"])
            if first == "policy" or not issued:
                plan, err = run(m, p, pol, kw, limit_cents)
                log.append(f"concurrent[{other}] during policy -> {err}")
            else:
                log.append(f"concurrent[{other}] during rotate -> {rotate(m, p, rng.choice(sorted(issued)), limit_cents)}")
            p.hook, door["on_deliver"] = None, None
        elif ev == "consumer-label" and live:
            h = rng.choice(live)
            rec_name = next((n for n, r in issued.items() if r.get("hash") == h), None)
            if rec_name:
                lab = p.keys[h]["label"]
                listed = rng.choice([True, False])
                consumers[f"worker-{rec_name}"] = lab
                scan = refresh_consumers(m, p, consumers, listed=listed)
                rc = revoke(m, rec_name)
                still = h in p.keys
                log.append(f"consumer reads {h} by label ({'listed' if scan['keys'] else 'unlisted'}); revoke -> rc={rc} still={still}")
                if not still:
                    broke.append(f"(R) revoke deleted {h} which a consumer reads by label "
                                 f"({'listed' if scan['keys'] else 'past the listing: scan.destinations only'})")
                protected.add(rec_name)
        elif ev == "revoke-remedy":
            plan, err = run(m, p, pol, kw, limit_cents)
            for s in plan or []:
                if s.get("action") in ("held-pending", "not-in-policy", "held-missing", "held-disabled") \
                        and s.get("name") not in protected:
                    log.append(f"remedy revoke {s['name']} -> {remedy_revoke(m, p, s['name'], consumers)}")
        for u in untracked(m, p):
            msg = f"(L) live project key unknown to the ledger after {ev}: {u}"
            if msg not in broke:
                broke.append(msg)
    # ---- convergence: failures off, apply the door's own remedies ----
    p.fail.clear()
    p.unread.clear()
    door["deliver"] = door["save"] = None
    # Each remedy refreshes the real producer; known consumers remain observed.
    last = None
    invalid_at_rest = invalid_policy(pol, limit_cents)
    for attempt in range(5):
        plan, err = run(m, p, pol, kw, limit_cents)
        acts = [s.get("action") for s in plan or []]
        log.append(f"converge#{attempt} -> {err} {acts}")
        if invalid_at_rest:
            # run() independently proves the rejection and unchanged state. An
            # invalid policy needs a corrected policy, not key deletion/reminting.
            log.append("invalid-input partition: expected rejection, state checked")
            break
        if any(s.get("action") == "held-rotation" for s in plan or []):
            log.append("held-rotation: no implemented reconciliation remedy; reservation is not convergence")
        todo = [s for s in plan or [] if s.get("action") in
                ("held-pending", "not-in-policy", "held-missing", "held-disabled")]
        if not todo and err is None and serves_every_slot(plan, refs):
            last = plan
            break
        for s in todo:
            rec = m.ledger()["issued"].get(s["name"]) or {}
            if s["action"] == "held-pending" and not rec.get("hash") and not consumers:
                # Explicit synthetic operator cleanup, never clear observed holders.
                # Complete finite model inventory is not a real bounded listing.
                for h in [h for h, r in p.keys.items() if r["name"] == s["name"]]:
                    del p.keys[h]
                    p.order.remove(h)
            rc = remedy_revoke(m, p, s["name"], consumers)
            log.append(f"  remedy revoke {s['name']} ({s['action']}) -> {rc}")
            if rc != 0 and s["action"] == "held-pending" and not rec.get("hash"):
                proof = pending_absence_attestation(m,p,s["name"],consumers,complete=True)
                outcome = apply_pending_attestation(m,p,s["name"],consumers,proof,complete=True)
                log.append(f"  explicit operator attestation {s['name']} -> {outcome['status']}")
        for proposal in limit_remedies(m,p,plan):
            outcome = apply_limit_remedy(m,p,plan,proposal)
            log.append(f"  explicit operator limit {proposal['name']} -> {outcome['status']}")
    if invalid_at_rest:
        pass  # the rejection property above replaces convergence only for invalid inputs
    elif last is None:
        broke.append(f"(c') no convergence after the door's own remedies: {log[-1][:300]}")
    else:
        w = p.writes
        plan2, err2 = run(m, p, pol, kw, limit_cents)
        if (p.writes != w or err2) and shape["echo"] != "x1.0001":
            broke.append(f"(c') the run after convergence wrote {p.writes - w}: {err2}")
        if any(s.get("action") == "over-limit" for s in last):
            # Over-limit at rest is only legitimate when a provider echo puts ceilings past it.
            if shape["echo"] in ("id", "+1e-9", "round2", "-0.004") and not any(l.startswith("subcent") for l in log):
                broke.append(f"(c') converged OVER LIMIT with an exact provider: {[s.get('why') for s in last if s.get('action') == 'over-limit']}")
    for u in untracked(m, p):
        msg = f"(L) at rest: {u}"
        broke.append(msg)
    broke += p.violations[:2]
    if trace:
        print("\n".join(log))
    return broke


if __name__ == "__main__":
    from adversarial_runner import main
    raise SystemExit(main(case, sys.argv[1:], variant=pathlib.Path(__file__).stem,
                          source_paths=[__file__, T.__file__, T.ROOT / "tools/openrouter.py",
                                        T.ROOT / "collectors/scan_openrouter.py"]))
