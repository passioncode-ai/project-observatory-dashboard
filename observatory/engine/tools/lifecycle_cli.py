#!/usr/bin/env python3
"""The operator's door to node.lifecycle: what the node may wake, stop and restart, and for whom.

    project-observatory full lifecycle status [--json] [--no-probe]
    project-observatory full lifecycle enroll TARGET_ID --path PATH --caller PRINCIPAL --startup-effect TEXT --kind app|launchd
                                              [--dependency ID] [--timeout SECONDS]
    project-observatory full lifecycle allow PRINCIPAL --binding BINDING_ID [--tool TOOL] [--target TARGET] [--remove]
                                             [--operator-proxy on|off]
    project-observatory full lifecycle policy TARGET_ID [--desired on_demand|always_on] [--idle-minutes N]
                                              [--background-launch on|off] [--readiness KIND ...]
    project-observatory full lifecycle hold TARGET_ID [--release]
    project-observatory full lifecycle revoke PRINCIPAL
    project-observatory full lifecycle build-helper [--dest PATH]

Every verb that changes `config/node-lifecycle.json` (enroll, allow, policy, hold, revoke) needs a terminal, like
`full access-binding`: these are the operator's grants, and a script able to write them could hand itself every app
on the machine. There is no --yes. `status` reads and `build-helper` compiles the macOS helper; neither grants
anything.

`enroll` takes the path the operator names, absolute, and never looks for one: on macOS an app's signature is
verified and its designated requirement (signing team and bundle id) recorded, a background job's definition and
the files it runs are hashed (`macos.MacAdapter.fingerprint`). The startup effects are the operator's own words for
what starting the target does; the record is refused without one. A target acts for a caller only when both agree:
the target lists the caller (`enroll --caller`) and the caller's grant lists the tool and the target (`allow`). The
caller proves who it is with an access binding (`full access-binding issue`); this file names principals, never a
bearer or its hash, and each grant names the ONE binding that proves its caller (`allow --binding ID`): a binding's
scopes and effect ceiling are memory's, so another binding of the same principal, issued to read memory, opens nothing
here.

Every change raises the revision the agents send back (`policyRevision` of a target, `grantRevision` of a caller),
so an agent acting on what it read before the change is refused `policy_changed`, never served the old decision.
The design: docs/design/LIFECYCLE.md (OBS-79, step L3).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import access_binding as AB                                                         # noqa: E402
from node.lifecycle import broker                                                   # noqa: E402
from node.lifecycle import service as S                                             # noqa: E402
from node.lifecycle.broker import TOOLS, LifecycleError                             # noqa: E402

DEFAULT_TIMEOUT = 30
READINESS = ("process", "fabric-service", "fabric-hub", "switchboard-runtime")


def _is_terminal() -> bool:
    return sys.stdin.isatty()


def require_terminal(action: str) -> None:
    if not _is_terminal():
        raise SystemExit(
            f"lifecycle: refusing to {action} without a terminal. It is the operator's grant over the apps on "
            f"this machine, and a script must not be able to give it. Run it yourself; there is no --yes.")


def _adapter():
    return S.default_adapter()


def _fail(exc: LifecycleError) -> int:
    detail = getattr(exc, "detail", "") or S.HINTS.get(exc.code, "")
    print(f"lifecycle: refused, {exc.code}" + (f": {detail}" if detail else ""), file=sys.stderr)
    return 1


def _change(edit) -> int:
    """Read, edit, check and write the document under the operator's lock; nothing is written on a refusal."""
    try:
        with S.config_lock():
            doc = S.read_config()
            result = edit(doc)
            if result is not None:
                S.write_config(doc)
    except LifecycleError as exc:
        return _fail(exc)
    if result is not None:
        print(json.dumps(result, sort_keys=True))
    return 0


def _principal(value: str) -> str:
    if not AB.PRINCIPAL.match(value) or not broker.ID.fullmatch(value):
        raise LifecycleError("invalid_argument")
    return value


def _target(doc: dict, target_id: str) -> dict:
    target = doc["targets"].get(broker.valid_id(target_id))
    if target is None:
        raise LifecycleError("not_enrolled")
    return target


def cmd_enroll(a: argparse.Namespace) -> int:
    require_terminal("enroll a target")
    path = Path(a.path)
    if not path.is_absolute():
        print("lifecycle: refused, invalid_argument: --path is the absolute place the operator names; "
              "nothing is looked up", file=sys.stderr)
        return 1
    if not 1 <= a.timeout <= 60:
        print("lifecycle: refused, invalid_argument: --timeout is 1 to 60 seconds", file=sys.stderr)
        return 1
    try:
        callers = [_principal(c) for c in a.callers]
        # Verified before the lock: a signature check can take a while and changes nothing.
        identity = _adapter().fingerprint(a.kind, str(path))
    except LifecycleError as exc:
        return _fail(exc)

    def edit(doc):
        key = broker.valid_id(a.target_id)
        old = doc["targets"].get(key, {})
        doc["targets"][key] = {
            "targetId": key, "kind": a.kind, "path": str(path), "identity": identity,
            # A revision names one decision: re-enrolling the same id never reuses one an old operation cited.
            "policyRevision": int(old.get("policyRevision", 0)) + 1,
            "desiredState": "on_demand", "dependencies": [broker.valid_id(d) for d in a.dependencies],
            "readiness": {"kind": "process"}, "timeoutSeconds": a.timeout,
            "startupEffects": list(a.startup_effects), "approved": True,
            "allowedCallers": list(dict.fromkeys(callers)),
        }
        return {"targetId": key, "kind": a.kind, "policyRevision": doc["targets"][key]["policyRevision"],
                "enrolled": True, "startupEffects": list(a.startup_effects)}
    return _change(edit)


def cmd_allow(a: argparse.Namespace) -> int:
    require_terminal("change a caller's grant")
    if not a.tools and not a.targets and a.operator_proxy is None and not a.binding:
        print("lifecycle: refused, invalid_argument: name a --tool, a --target, a --binding or --operator-proxy to "
              "change",
              file=sys.stderr)
        return 1

    def edit(doc):
        principal = _principal(a.principal)
        if any(t not in TOOLS for t in a.tools):
            raise LifecycleError("invalid_argument")
        for target_id in a.targets:
            broker.valid_id(target_id)                         # the wildcard is nobody's: valid_id refuses it
        if a.targets and not a.remove and any(t not in doc["targets"] for t in a.targets):
            raise LifecycleError("not_enrolled")               # a grant to nothing is a typo, not a decision
        grant = doc["callers"].get(principal)
        binding_id = _bound(principal, a.binding) if a.binding else None
        if grant is None:
            if a.remove:
                raise LifecycleError("not_found")
            if binding_id is None:
                print("lifecycle: a new grant names the one access binding that proves the caller: --binding ID "
                      "(`project-observatory full access-binding show` lists them)", file=sys.stderr)
                raise LifecycleError("invalid_argument")
            grant = {"bindingId": binding_id, "revision": 0, "revoked": False, "allowedTools": [],
                     "allowedTargets": []}
        rebound = binding_id is not None and binding_id != grant["bindingId"]
        tools, targets = list(grant["allowedTools"]), list(grant["allowedTargets"])
        for items, wanted in ((tools, a.tools), (targets, a.targets)):
            for item in wanted:
                if a.remove and item in items:
                    items.remove(item)
                elif not a.remove and item not in items:
                    items.append(item)
        proxy = grant.get("operatorProxy") is True
        if a.operator_proxy is not None:
            # Only a caller that relays the operator's own commands may hold an always_on target off; an agent never.
            proxy = a.operator_proxy == "on"
        revoked = grant["revoked"] and a.remove                # an allow is a fresh grant; a removal keeps a revoke
        if rebound or grant["revision"] == 0 or (tools, targets, proxy, revoked) != (
                grant["allowedTools"], grant["allowedTargets"], grant.get("operatorProxy") is True, grant["revoked"]):
            grant.update(allowedTools=tools, allowedTargets=targets, revoked=revoked, revision=grant["revision"] + 1,
                         bindingId=binding_id or grant["bindingId"])
            if proxy:
                grant["operatorProxy"] = True
            else:
                grant.pop("operatorProxy", None)
            doc["callers"][principal] = grant
        out = {"caller": principal, "binding": grant["bindingId"], "allowedTools": tools, "allowedTargets": targets,
               "operatorProxy": proxy, "grantRevision": grant["revision"]}
        missing = [t for t in targets if principal not in doc["targets"].get(t, {}).get("allowedCallers", [principal])]
        if missing:
            out["note"] = ("these targets do not list this caller, so it is still denied there; enroll them again "
                           "with --caller " + principal + ": " + ", ".join(missing))
        return out
    return _change(edit)


def cmd_revoke(a: argparse.Namespace) -> int:
    require_terminal("revoke a caller")

    def edit(doc):
        principal = _principal(a.principal)
        grant = doc["callers"].get(principal)
        if grant is None:
            raise LifecycleError("not_found")
        if not grant["revoked"]:
            grant["revoked"] = True
            grant["revision"] += 1
        return {"revoked": principal, "grantRevision": grant["revision"]}
    return _change(edit)


def cmd_hold(a: argparse.Namespace) -> int:
    require_terminal("hold a target")

    def edit(doc):
        target = _target(doc, a.target_id)
        wanted = "on_demand" if a.release else "held_off"
        if target["desiredState"] != wanted:
            target["desiredState"] = wanted
            target["policyRevision"] += 1
        return {"targetId": a.target_id, "desiredState": target["desiredState"],
                "policyRevision": target["policyRevision"]}
    return _change(edit)


def cmd_policy(a: argparse.Namespace) -> int:
    require_terminal("change a target's policy")
    if a.desired is None and a.idle_minutes is None and a.background_launch is None and a.readiness is None:
        print("lifecycle: refused, invalid_argument: name --desired, --idle-minutes, --background-launch or "
              "--readiness", file=sys.stderr)
        return 1

    def edit(doc):
        target = _target(doc, a.target_id)
        if a.desired:
            target["desiredState"] = a.desired
            if a.desired == "always_on" and a.idle_minutes is None:
                target.pop("idleStopMinutes", None)            # an always_on target never idles out
        if a.background_launch is not None:
            if a.background_launch == "on":
                target["backgroundLaunch"] = True
            else:
                target.pop("backgroundLaunch", None)
        if a.idle_minutes is not None:
            if a.idle_minutes == 0:
                target.pop("idleStopMinutes", None)
            else:
                target["idleStopMinutes"] = a.idle_minutes
        if a.readiness is not None:
            # Switched in place: grants, identity, policy stay; the schema refuses a product profile on another app.
            readiness = {"kind": a.readiness}
            if a.readiness == "fabric-service":
                readiness.update(origin=a.origin, serviceId=a.service_id, instance=a.instance,
                                 buildDigest=a.build_digest)
            target["readiness"] = {k: v for k, v in readiness.items() if v is not None}
        target["policyRevision"] += 1
        return {"targetId": a.target_id, "desiredState": target["desiredState"],
                "idleStopMinutes": target.get("idleStopMinutes"),
                "backgroundLaunch": target.get("backgroundLaunch", False),
                "readiness": target["readiness"]["kind"], "policyRevision": target["policyRevision"]}
    return _change(edit)


def _bindings_registry():
    """(the access bindings in force, this workspace's audience); raises AB.BindingError on a broken registry."""
    import memory_access as MA
    return MA.registry(), MA.own_audience()


def binding_state(binding_id: str) -> str:
    """`in force`, `revoked`, `expired`, `another workspace`, `absent` or `unreadable`: names only, never a digest."""
    from datetime import datetime, timezone
    try:
        registry, audience = _bindings_registry()
    except AB.BindingError:
        return "unreadable"
    b = next((b for b in registry.bindings if b.binding_id == binding_id), None)
    if b is None:
        return "absent"
    if b.audience != audience:
        return "another workspace"
    if b.revoked_at is not None:
        return "revoked"
    if AB._when(b.expires_at) <= datetime.now(timezone.utc):
        return "expired"
    return "in force"


def _bound(principal: str, binding_id: str) -> str:
    """The binding a grant may name: one of this workspace, of this principal, not revoked and not expired (the
    runtime refuses an expired one, so a grant naming it would open nothing). A binding's scopes and ceilings are
    memory's; the lifecycle grant is a separate decision, tied to this one binding only."""
    from datetime import datetime, timezone
    if not AB.BINDING_ID.match(binding_id):
        raise LifecycleError("invalid_argument")
    try:
        registry, audience = _bindings_registry()
    except AB.BindingError:
        raise LifecycleError("invalid_argument") from None
    b = next((b for b in registry.bindings if b.binding_id == binding_id), None)
    if (
        b is None
        or b.principal != principal
        or b.audience != audience
        or b.revoked_at is not None
        or AB._when(b.expires_at) <= datetime.now(timezone.utc)
    ):
        print(f"lifecycle: {binding_id} is not a binding of {principal} in force for this workspace "
              "(`project-observatory full access-binding show`)", file=sys.stderr)
        raise LifecycleError("invalid_argument")
    return binding_id


def status(probe: bool = True) -> dict:
    """What the operator sees: the platform, every target with its policy (and, with `probe`, its state now, read
    without starting, stopping or renewing anything), every caller with its grant and whether a binding proves it,
    and the last events."""
    from node.lifecycle import macos
    out: dict = {"platform": sys.platform, "supported": sys.platform == "darwin",
                 "config": str(S.config_path()), "configured": S.config_path().exists(),
                 "helper": {"path": str(macos.default_helper()), "built": macos.default_helper().is_file()},
                 "targets": [], "callers": [], "events": [], "degraded": []}
    try:
        doc = S.read_config()
    except LifecycleError as exc:
        out["degraded"].append({"source": "config", "reason": f"{exc.code}: {getattr(exc, 'detail', '')}".strip(": ")})
        return out
    adapter = _adapter() if probe and doc["targets"] else None
    try:
        for key, t in sorted(doc["targets"].items()):
            row = {k: t[k] for k in ("targetId", "kind", "path", "desiredState", "policyRevision", "allowedCallers",
                                     "startupEffects")}
            row.update(readiness=t["readiness"]["kind"], idleStopMinutes=t.get("idleStopMinutes"),
                       backgroundLaunch=t.get("backgroundLaunch", False))
            if adapter is not None:
                try:
                    row["observed"] = {k: v for k, v in adapter.probe(t).items()
                                       if k in ("state", "pid", "windows", "processReady", "capabilityReady")}
                except LifecycleError as exc:
                    row["observed"] = {"state": exc.code}
            out["targets"].append(row)
    finally:
        close = getattr(adapter, "close", None)
        if close:
            close()
    for principal, g in sorted(doc["callers"].items()):
        out["callers"].append({"caller": principal, "revoked": g["revoked"], "grantRevision": g["revision"],
                               "allowedTools": g["allowedTools"], "allowedTargets": g["allowedTargets"],
                               "operatorProxy": g.get("operatorProxy", False),
                               "binding": g["bindingId"], "bindingState": binding_state(g["bindingId"])})
    try:
        out["events"] = S.EventLog(S.state_dir() / S.EVENTS_FILE).fetch(None, 10)
    except (OSError, ValueError):
        out["degraded"].append({"source": "events", "reason": "the event log could not be read"})
    return out


def cmd_status(a: argparse.Namespace) -> int:
    doc = status(probe=not a.no_probe)
    if a.json:
        print(json.dumps(doc, indent=1, sort_keys=True))
        return 0
    print(f"node.lifecycle on {doc['platform']}" + ("" if doc["supported"] else " (no adapter here: every tool "
                                                    "answers unsupported_platform)"))
    if not doc["configured"]:
        print("  nothing enrolled: the tools are inert. Enroll a target with `project-observatory full lifecycle "
              "enroll TARGET_ID --kind app --path PATH --caller PRINCIPAL --startup-effect TEXT`.")
    for row in doc["degraded"]:
        print(f"  degraded {row['source']}: {row['reason']}")
    if doc["supported"] and not doc["helper"]["built"]:
        print("  the macOS helper is not built, so apps answer setup_required: "
              "`project-observatory full lifecycle build-helper`")
    for t in doc["targets"]:
        seen = (t.get("observed") or {}).get("state", "not probed")
        print(f"  {t['targetId']} ({t['kind']}) {t['desiredState']} rev {t['policyRevision']} · {seen} · "
              f"callers {', '.join(t['allowedCallers'])}")
    for c in doc["callers"]:
        state = "revoked" if c["revoked"] else f"binding {c['binding']} {c['bindingState']}"
        print(f"  {c['caller']} rev {c['grantRevision']} · {state} · tools {', '.join(c['allowedTools']) or 'none'}"
              f" · targets {', '.join(c['allowedTargets']) or 'none'}")
    for e in doc["events"]:
        print(f"  {e['at']} {e['text']}")
    return 0


def cmd_build_helper(a: argparse.Namespace) -> int:
    from node.lifecycle import macos
    return macos.main(["build-helper"] + (["--dest", a.dest] if a.dest else []))


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="project-observatory full lifecycle",
                                description="What node.lifecycle may wake, stop and restart, and for which caller. "
                                            "Changes need a terminal.")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status", help="targets, callers, the helper and the last events; starts nothing")
    s.add_argument("--json", action="store_true")
    s.add_argument("--no-probe", action="store_true", help="do not observe the targets now")
    e = sub.add_parser("enroll", help="enroll an installed app or background job at a path you name (terminal only)")
    e.add_argument("target_id")
    e.add_argument("--kind", choices=("app", "launchd"), required=True)
    e.add_argument("--path", required=True, help="the absolute place: an .app bundle or a LaunchAgent plist")
    e.add_argument("--caller", dest="callers", action="append", required=True,
                   help="agent:<name> or service:<name> that may act on it; repeatable")
    e.add_argument("--startup-effect", dest="startup_effects", action="append", required=True,
                   help="what starting it does, in your words; repeatable")
    e.add_argument("--dependency", dest="dependencies", action="append", default=[],
                   help="a target started first; repeatable")
    e.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="seconds to readiness, 1..60; default 30")
    al = sub.add_parser("allow", help="give or take a caller's tools and targets (terminal only)")
    al.add_argument("principal", help="agent:<name> or service:<name>, as its access binding names it")
    al.add_argument("--binding", help="bnd_...: the one access binding that proves this caller; required for a new "
                                      "grant. Another binding of the same principal opens nothing here")
    al.add_argument("--tool", dest="tools", action="append", default=[], choices=TOOLS, help="repeatable")
    al.add_argument("--target", dest="targets", action="append", default=[], help="an enrolled target id; repeatable")
    al.add_argument("--remove", action="store_true", help="take the named tools and targets away")
    al.add_argument("--operator-proxy", choices=("on", "off"),
                    help="a caller relaying the operator's own commands may hold an always_on target off")
    po = sub.add_parser("policy", help="desired state, idle stop, background launch, readiness (terminal only)")
    po.add_argument("target_id")
    po.add_argument("--desired", choices=("on_demand", "always_on"),
                    help="always_on is kept running by the supervisor (step L4)")
    po.add_argument("--idle-minutes", type=int, help="stop what the node started after N idle minutes "
                                                    f"({broker.IDLE_MINUTES[0]}..{broker.IDLE_MINUTES[1]}); 0 removes")
    po.add_argument("--background-launch", choices=("on", "off"), help="open the app without bringing it forward")
    po.add_argument("--readiness", choices=READINESS)
    po.add_argument("--origin", help="fabric-service: http://127.0.0.1:PORT")
    po.add_argument("--service-id", help="fabric-service: the service id its well-known document names")
    po.add_argument("--instance", help="fabric-service: its instance")
    po.add_argument("--build-digest", help="fabric-service: its build digest")
    h = sub.add_parser("hold", help="switch a target off on purpose: agents get held_off (terminal only)")
    h.add_argument("target_id")
    h.add_argument("--release", action="store_true", help="back to on_demand")
    r = sub.add_parser("revoke", help="refuse a caller from its next call (terminal only)")
    r.add_argument("principal")
    b = sub.add_parser("build-helper", help="compile the macOS helper for a source install (needs swiftc)")
    b.add_argument("--dest", help="default <state>/node-lifecycle/bin/lifecycle-native")
    return p


def main(argv: list[str]) -> int:
    a = parser().parse_args(argv)
    return {"status": cmd_status, "enroll": cmd_enroll, "allow": cmd_allow, "policy": cmd_policy,
            "hold": cmd_hold, "revoke": cmd_revoke, "build-helper": cmd_build_helper}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
