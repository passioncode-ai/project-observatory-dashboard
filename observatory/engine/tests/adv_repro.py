import sys, pathlib, json, io, contextlib
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import test_credential_doors as T
def q(function, *args):
    with contextlib.redirect_stderr(io.StringIO()):
        return function(*args)

def wrap(p, on):
    def f(path, key, payload=None, method=None):
        return on(p, path, key, payload, method)
    return f

def r_a(fault):
    m, p = T._r3_door(); m.issue_for_policy(T._pol(1), "example")
    if fault == "502":
        def on(p, path, key, payload, method):
            r = p(path, key, payload, method)
            if path == "/keys" and payload: raise RuntimeError("openrouter answered HTTP 502")
            return r
        m._request = wrap(p, on)
    else:
        def kill(v, to, **k): raise KeyboardInterrupt
        m.deliver = kill
    try: rc = q(m.cmd_rotate, "project-a-prod-k0", False)
    except KeyboardInterrupt: rc = "killed"
    m._request = p; m.deliver = lambda v, to, **k: "vault"
    rc2, out = T._cmd(m, T._pol(1), "example", None, False)
    print(f"R-A rotate {fault}: rotate={rc}; provider keys={[(r['name'], r['limit']) for r in p.keys.values()]} total=${p.total()} limit $10; "
          f"next issue-for-policy rc={rc2} OVER LIMIT shown={'OVER LIMIT' in out}; ledger hashes={[r.get('hash') for r in m.ledger()['issued'].values()]}")

def r_b():
    m, p = T._r3_door(); m.issue_for_policy(T._pol(1), "example")
    def on(p, path, key, payload, method):
        if method == "DELETE": raise RuntimeError("openrouter answered HTTP 502")
        return p(path, key, payload, method)
    m._request = wrap(p, on); m.deliver = lambda v, to, **k: "vault"
    rc = q(m.cmd_rotate, "project-a-prod-k0", False); m._request = p
    rc2, out = T._cmd(m, T._pol(1), "example", None, False)
    print(f"R-B rotate, predecessor delete fails: rotate={rc}; total=${p.total()} limit $10; issue-for-policy rc={rc2} OVER LIMIT shown={'OVER LIMIT' in out}; "
          f"row={ {k: v for k, v in m.ledger()['issued']['project-a-prod-k0'].items() if k in ('hash','predecessor_hash','rotation_status')} }")

def r_c():
    m, p = T._r3_door(); m.issue_for_policy(T._pol(1), "example")
    real = m.save_ledger; n = {"k": 0}
    def bad(doc):
        raise OSError("disk refused")
    m.save_ledger = bad
    try: m.issue_for_policy(T._pol(1, limit=5), "example")
    except m.PolicyPartial: pass
    m.save_ledger = real
    plan = m.issue_for_policy(T._pol(1, limit=5), "example")
    m.deliver = lambda v, to, **k: "vault"
    rc = q(m.cmd_rotate, "project-a-prod-k0", False)
    print(f"R-C stale ledger ceiling: second run {[s['action'] for s in plan]}, ledger limit_usd={m.ledger()['issued']['project-a-prod-k0']['limit_usd']}; "
          f"after rotate rc={rc} provider={[(r['name'], r['limit']) for r in p.keys.values()]} against limit $5")

def r_d():
    m, p = T._r3_door(); m.issue_for_policy(T._pol(1), "example")
    rec = m.ledger()["issued"]["project-a-prod-k0"]
    sc = pathlib.Path(T.tmpdir.mkdtemp()); m.paths.SCRATCH = sc
    (sc / "openrouter.json").write_text(json.dumps({"keys": [], "destinations": {"worker": rec["label"]},
        "destination_hashes": {}, "unlisted_destinations": ["worker"]}))
    rc = q(m.cmd_revoke, "project-a-prod-k0")
    print(f"R-D consumer known by label only (key past the listing): revoke rc={rc}, key still live={bool(p.keys)}")

def r_e():
    m, p = T._r3_door()
    def too_long(admin, include_disabled=True): raise m.ListingTooLong("past PAGES_MAX", [])
    m._all_keys = too_long
    def on(p, path, key, payload, method):
        if path == "/keys" and payload:
            p(path, key, payload, method); raise m.Unreachable("openrouter unreachable: TimeoutError")
        return p(path, key, payload, method)
    m._request = wrap(p, on)
    try: m.issue_for_policy(T._pol(1), "example")
    except m.PolicyPartial: pass
    m._request = p
    p.keys.clear()                     # the operator follows the remedy: deletes the key at the provider
    rc = q(m.cmd_revoke, "project-a-prod-k0")
    rc2, out = T._cmd(m, T._pol(1), "example", None, False)
    print(f"R-E hashless pending row past PAGES_MAX, key deleted at provider per remedy: revoke rc={rc}; row still={'project-a-prod-k0' in m.ledger()['issued']}; "
          f"issue-for-policy rc={rc2} held-pending={'held-pending' in out} keys={len(p.keys)}")

def r_f():
    m, p = T._r3_door()
    def too_long(admin, include_disabled=True): raise m.ListingTooLong("past PAGES_MAX", [])
    m._all_keys = too_long
    def on(p, path, key, payload, method):
        r = p(path, key, payload, method)
        if path == "/keys" and payload and not on.done:
            on.done = True; raise RuntimeError("openrouter answered HTTP 502; provider response withheld")
        return r
    on.done = False
    m._request = wrap(p, on)
    try: m.issue_for_policy(T._pol(1), "example")
    except m.PolicyPartial as e: pass
    first_rows, first_keys = len(m.ledger()["issued"]), len(p.keys)
    rc2, out = T._cmd(m, T._pol(1), "example", None, False)
    print(f"R-F mint answered 502 after creating the key, account past PAGES_MAX: ledger rows after 1st run={first_rows}; 2nd run rc={rc2}; "
          f"keys={[(r['name'], r['limit']) for r in p.keys.values()]} total=${p.total()} limit $10 max=${p.max_sum}")
    return {"ledger_rows_after_first": first_rows, "keys_after_first": first_keys,
            "keys_after_retry": len(p.keys), "retry_exit": rc2}

def r_g():
    m, p = T._r3_door()
    def kill(v, to, **k): raise KeyboardInterrupt
    m.deliver = kill
    try: m.issue_for_policy(T._pol(1), "example")
    except KeyboardInterrupt: pass
    m.deliver = lambda v, to, **k: "vault"
    rc = q(m.cmd_rotate, "project-a-prod-k0", False)
    rec = m.ledger()["issued"]["project-a-prod-k0"]
    rc2, out = T._cmd(m, T._pol(1), "example", None, False)
    print(f"R-G rotate of a pending row: rotate rc={rc} (successor delivered), row pending={rec.get('pending')}; issue-for-policy rc={rc2} "
          f"held-pending={'held-pending' in out}, remedy names revoke={'revoke project-a-prod-k0' in out}")

if __name__ == "__main__":
    for f in (lambda: r_a("502"), lambda: r_a("kill"), r_b, r_c, r_d, r_e, r_f, r_g):
        try: f()
        except Exception as e: print("ERR", type(e).__name__, e)
