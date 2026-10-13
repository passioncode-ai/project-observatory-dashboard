import sys, json, os, collections, traceback, re
module_name = os.environ.get("ADV_MOD", "adv_j1_property")
if module_name not in {"adv_j1_property", "adv_j1_property_forget", "adv_norot", "adv_norot_forget"}:
    raise SystemExit("unknown synthetic adversary variant")
mod = __import__(module_name)
arg = sys.argv[1]
incomplete = False
if arg.endswith(".json"):
    saved = json.load(open(arg))
    if saved.get("schema") == "synthetic-adversary/1":
        incomplete = saved.get("status") == "ERROR" or saved.get("completed", 0) != saved.get("requested", 0)
    seeds = [row["seed"] for row in saved["cases"] if row["status"] != "PASS"] if saved.get("schema") == "synthetic-adversary/1" else [int(seed) for seed in saved]
else:
    seeds = list(range(int(arg), int(arg) + int(sys.argv[2])))
by = collections.Counter(); ex = {}; nfail = 0
ROT = ("rotate", "rotate-kill")
for s in seeds:
    try:
        b = mod.case(s)
    except Exception:
        b = ["CRASH " + traceback.format_exc().splitlines()[-1]]
    if b: nfail += 1
    first = {}
    for m in b:
        mm = re.match(r"\(L\) live project key unknown to the ledger after ([\w-]+): (\S+)", m)
        if mm: first.setdefault(mm.group(2), mm.group(1))
    for m in b:
        if m.startswith("(L) live project key"):
            continue                                   # counted once, at rest, by the event that caused it
        if m.startswith("(L) at rest:"):
            k = m.split(": ", 1)[1].split(" ")[0]
            ev = first.get(k, "convergence")
            verb = "rotate" if ev in ROT or "-next" in k else ("concurrent" if ev == "concurrent" else ev)
            key = f"{verb}: (L) untracked live key (first seen after {ev})"
        elif m.startswith("(a-rot)"):
            key = "rotate: (a-rot) rotate left the total past the limit"
        elif m.startswith("(R)"):
            key = "revoke: " + m[:70]
        elif m.startswith("(c')"):
            key = "convergence: " + m[:60]
        else:
            key = "issue: " + m[:60]
        by[key] += 1; ex.setdefault(key, (s, m[:180]))
print("seeds", len(seeds), "failing", nfail)
for k, n in by.most_common():
    print(f"{n:5d} {k} | e.g. {ex[k][0]}: {ex[k][1]}")

raise SystemExit(1 if nfail or incomplete else 0)
