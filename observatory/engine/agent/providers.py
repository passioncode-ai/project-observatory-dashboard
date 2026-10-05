#!/usr/bin/env python3
"""The one boundary where a model, its window and its price are resolved.

Nothing else in this repository may name a vendor id or a price. Both come from
`agent/models.json` (the chain) and OpenRouter's own catalogue (the numbers),
because a price table in source is wrong the day a provider changes one and
nothing notices.

Three things this owns, each earned rather than invented:

* **Selection has three levels** — the caller, the config, the default — resolved
  here and *logged*, because "why did it use that model" is otherwise
  unanswerable.
* **Attempts are capped in TOTAL**, not per model. Three models times three
  retries is nine calls for one prompt.
* **Spend is enforced on the write.** `charge()` records and enforces in the same
  breath; enforcement on a schedule arrives after the money is gone.

The wallet is denominated in **credits**, which is what OpenRouter's usage
accounting reports. One credit tracks one dollar at the time of writing; this
module will not relabel the provider's unit on its behalf.

Failover happens BETWEEN calls, never inside one. That is free here — each call
is a single turn with no accumulated trajectory — and it is the boundary that
matters, because reasoning carries a vendor credential that does not travel and
fails as a 400 rather than as degradation.
"""
from __future__ import annotations
import json, os, sys, pathlib, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import atomic
import paths

CONFIG = paths.config_file('models.json')
CATALOGUE = paths.STATE / "openrouter-catalogue.json"
WALLET = paths.STATE / "wallet.json"
HEALTH = paths.STATE / "provider-health.json"
#: The KEY's own account state, which is neither this project's journal nor a
#: model's health — a third subject, so a third file. `provider-health.json` is
#: keyed by model id and `unhealthy()` reads it by that key, so an entry about
#: the account would sit in a map where every other row means something else.
KEY_USAGE = paths.STATE / "key-usage.json"
KEY_ENV = "OPENROUTER_API_KEY"
EMBED_KEY_ENV = "OPENAI_API_KEY"

#: Where a key may live, in the order it is looked for. The convention is one
#: file per secret at mode 600, read at process start, and this follows it
#: rather than inventing another pattern.
#:
#: The scheduled job deliberately carries NO key in its service definition: such
#: a file is often world-readable, and environment variables declared there
#: would put the secret in plaintext where every process on the machine can read
#: it.
#: OBSERVATORY_KEY_FILE overrides the search entirely — one path, and only that
#: path. It exists because once a shell profile exports the key, "no credential"
#: becomes a state a test can no longer reach: clearing the environment variable
#: simply falls through to the secret store. A degradation that cannot be tested
#: is a degradation nobody has seen work.
#: THE PROJECT'S OWN KEY WINS. The secret store's key is the MACHINE's shared
#: secret — every agent on the host may reach OpenRouter through it — so if it
#: came first, a key placed in this repository for this project alone could
#: never be the one that got used, and the remedy an operator would reach for
#: when the shared key hits its limit (give the observatory its own key) would
#: be inert by construction. A specific credential beats a shared fallback; the
#: fallback is what keeps the machine working when the specific one is absent,
#: which is the whole point of having two.
KEY_FILES = ((pathlib.Path(os.environ["OBSERVATORY_KEY_FILE"]),)
             if os.environ.get("OBSERVATORY_KEY_FILE") else
             (paths.STATE / ".openrouter-key",
              paths.source_path("secret_store", paths.SECRETS) / 'openrouter'))
UA = "project-observatory/0.1 (+https://github.com/passioncode-ai/project-observatory-dashboard)"

# OpenRouter credits a request to the app named by HTTP-Referer, titled by X-OpenRouter-Title,
# filed under at most two X-OpenRouter-Categories (https://openrouter.ai/docs/app-attribution).
# The app is the product page, so every Observatory install adds to one public listing. Only the
# requests Observatory itself makes carry it, and only to OpenRouter: another provider gets
# nothing it would not otherwise see. None of these values is a secret. (Roadmap RM-16.)
ATTRIBUTION_URL = "https://passioncode.ai/observatory/"
ATTRIBUTION_TITLE = "Project Observatory"
ATTRIBUTION_CATEGORIES = "programming-app,cli-agent"


def attribution_headers(base_url: str) -> dict[str, str]:
    """The app-attribution headers for a request to `base_url`, or none off OpenRouter."""
    host = (urllib.parse.urlsplit(base_url).hostname or "").lower()
    if host != "openrouter.ai" and not host.endswith(".openrouter.ai"):
        return {}
    return {"HTTP-Referer": ATTRIBUTION_URL,
            "X-OpenRouter-Title": ATTRIBUTION_TITLE,
            "X-Title": ATTRIBUTION_TITLE,  # the legacy name, still read
            "X-OpenRouter-Categories": ATTRIBUTION_CATEGORIES}


class ProviderError(Exception):
    """Base. Subclasses split retryable from fatal at the type level, not at the call site."""


class Retryable(ProviderError):
    """Overload, rate limit, 5xx, a socket that died. Worth another model."""


class Fatal(ProviderError):
    """A rejected schema, a model that does not exist. Another attempt at the same
    model spends money to fail identically; another MODEL may still work."""


class CredentialError(Fatal):
    """401/403. Not the model's fault, so nothing is marked unhealthy and the whole
    run stops at once — trying every model in turn against a dead key produces one
    identical error per model and poisons the health of all of them."""


class PolicyRefused(Exception):
    """embedding-policy/1 did not authorize this text to leave the machine.

    Deliberately NOT a ProviderError: the indexer catches those to keep rows queued
    for a retry, and a policy refusal is not transient. A caller that reaches this
    has skipped the door — it must be loud, never a quiet lexical fallback."""


class BudgetExceeded(ProviderError):
    """A guardrail said stop. Not a failure — a decision."""


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def config() -> dict:
    return json.loads(CONFIG.read_text(encoding="utf-8"))


# ─────────────────────────────── the catalogue ───────────────────────────────

def _fetch_catalogue(base_url: str) -> dict:
    req = urllib.request.Request(f"{base_url}/models", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.loads(r.read().decode("utf-8"))


def catalogue(refresh: bool = False) -> tuple[dict, str]:
    """Returns (models_by_id, provenance). Never raises: a stale catalogue with a
    stated age beats a run that cannot start."""
    cfg = config()
    ttl = timedelta(hours=cfg.get("catalogue_ttl_hours", 24))
    cached, age = None, None
    if CATALOGUE.exists():
        try:
            cached = json.loads(CATALOGUE.read_text(encoding="utf-8"))
            age = now() - datetime.strptime(cached["fetched_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc)
        except Exception:
            cached = None
    if cached and not refresh and age is not None and age < ttl:
        return cached["models"], f"cached {int(age.total_seconds() // 60)}m ago"
    try:
        raw = _fetch_catalogue(cfg["base_url"])
    except (urllib.error.URLError, OSError, ValueError) as exc:
        if cached:
            return cached["models"], f"STALE ({exc.__class__.__name__}); cached {age}"
        raise Retryable(f"the catalogue is unreachable and nothing is cached: {exc}") from exc
    models = {}
    for m in raw.get("data", []):
        pricing = m.get("pricing") or {}
        def num(k: str) -> float:
            try:
                return float(pricing.get(k) or 0.0)
            except (TypeError, ValueError):
                return 0.0
        models[m["id"]] = {
            "id": m["id"],
            "name": m.get("name", ""),
            "context_length": m.get("context_length") or 0,
            "price_in_per_1m": num("prompt") * 1e6,
            "price_out_per_1m": num("completion") * 1e6,
            "supported_parameters": m.get("supported_parameters") or [],
        }
    # THROUGH `atomic`, like every other writer of state this project cannot
    # rebuild. `Path.write_text` truncates the destination before it writes, and
    # a crash in between loses the file — for the wallet that means lost spend
    # events. The catalogue is the cheapest of these — a refetch restores it —
    # and it goes the same way so that no reader of this file has to remember
    # which of them was the important one.
    atomic.write_json(CATALOGUE, {"fetched_at": iso(), "models": models})
    return models, "fetched now"


# ──────────────────────────────── health ─────────────────────────────────────

def _health() -> dict:
    if HEALTH.exists():
        try:
            return json.loads(HEALTH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def mark_unhealthy(model_id: str, reason: str) -> None:
    h = _health()
    h[model_id] = {"since": iso(), "reason": reason[:200]}
    HEALTH.parent.mkdir(parents=True, exist_ok=True)
    atomic.write_json(HEALTH, h)


def mark_healthy(model_id: str) -> None:
    h = _health()
    if h.pop(model_id, None) is not None:
        atomic.write_json(HEALTH, h)


def unhealthy(model_id: str) -> str | None:
    """None when usable. A model is re-probed after the configured window: a
    health check that only runs on failure never recovers."""
    entry = _health().get(model_id)
    if not entry:
        return None
    minutes = config().get("health", {}).get("probe_after_minutes", 60)
    try:
        since = datetime.strptime(entry["since"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        return None
    if now() - since > timedelta(minutes=minutes):
        return None                                                           
    return entry.get("reason", "unhealthy")


# ──────────────────────────────── selection ──────────────────────────────────

def resolve_chain(requested: str | None = None) -> tuple[list[dict], str, str]:
    """Returns (chain, selection_level, catalogue_provenance).

    Levels, in precedence order: the caller's explicit choice, the configured
    chain, the built-in default. Which one won is returned so it can be logged.
    """
    cfg = config()
    models, provenance = catalogue()
    if requested:
        entry = models.get(requested)
        if entry is None:
            raise Fatal(f"{requested} is not in the catalogue ({len(models)} models known). "
                        f"Check the id against {cfg['base_url']}/models")
        return [dict(entry, why="named by the caller")], "caller", provenance
    chain, retired = [], []
    for e in cfg.get("chain", []):
        entry = models.get(e["id"])
        if entry is None:
            # SKIPPED AND NAMED. A retired id is not fatal — the point of a chain
            # is that it survives one model leaving — but it was skipped
            # SILENTLY, so a three-model chain could become one and the only
            # visible sign would be the bill. `chain_retired` travels with the
            # answer, the CLI prints it, and `agent/observe.py` puts it in the
            # run report.
            #
            # That report is NOT where a reader looks, and this comment used to
            # claim it was. Measured 2026-09-07: the only readers of the field
            # were this repository's own tests, so the sentence sent the next
            # person to a consumer that did not exist. The reader is
            # `provider.chain_retired` in `tools/build_findings.py`, which reads
            # the run report and raises a warning naming the id and the file to
            # edit — a retirement never expires, unlike a health mark.
            retired.append(e["id"])
            continue
        chain.append(dict(entry, why=e.get("why", "")))
    if chain:
        if retired:
            chain[0] = dict(chain[0], chain_retired=retired)
        return chain, "config", provenance
    raise Fatal("no model in the configured chain exists in the catalogue; "
                "check agent/models.json against the provider")


def needs_structured(model: dict) -> bool:
    return "structured_outputs" in model.get("supported_parameters", [])


def estimate(model: dict, in_tokens: int, out_tokens: int) -> float:
    return (in_tokens * model["price_in_per_1m"] + out_tokens * model["price_out_per_1m"]) / 1e6


# ───────────────────────────────── wallet ────────────────────────────────────

_provider_usage_cache: dict | None = None


def provider_usage(force: bool = False) -> dict | None:
    """What the PROVIDER says this key has spent. None when unreachable.

    Preferred over the local journal for the daily and monthly caps, for one
    reason: it cannot drift. A local counter is wrong the first time a run dies
    between the call and the write, and wrong forever after. The provider's
    number is the same number the provider enforces against.

    The local journal is still authoritative for spend VELOCITY, because the API
    reports day, week and month — not a rolling half hour.
    """
    global _provider_usage_cache
    if _provider_usage_cache is not None and not force:
        return _provider_usage_cache
    key, _ = read_key()
    if not key:
        return None
    req = urllib.request.Request(f"{config()['base_url']}/key",
                                 headers={"Authorization": f"Bearer {key}", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read().decode("utf-8")).get("data") or {}
    except (urllib.error.URLError, OSError, ValueError):
        return None
    _provider_usage_cache = {
        "daily": float(d.get("usage_daily") or 0.0),
        "weekly": float(d.get("usage_weekly") or 0.0),
        "monthly": float(d.get("usage_monthly") or 0.0),
        "total": float(d.get("usage") or 0.0),
        "limit": d.get("limit"),
        "limit_remaining": d.get("limit_remaining"),
        "limit_reset": d.get("limit_reset"),
    }
    record_key_usage(_provider_usage_cache)
    return _provider_usage_cache


def retention_config() -> dict:
    """The horizons, from the one file that holds every other one.

    A missing or unreadable file falls back to the numbers that were literals
    before this existed — the wallet must still be prunable when a config is
    being edited, and refusing to charge because a JSON file is mid-save would
    make the spend journal the fragile part of a spend guardrail.
    """
    try:
        return json.loads(paths.config_file("retention.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _wallet() -> dict:
    if WALLET.exists():
        try:
            return json.loads(WALLET.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"denomination": "credits", "events": [], "days": {}, "months": {}}


def _save_wallet(w: dict) -> None:
    WALLET.parent.mkdir(parents=True, exist_ok=True)
    # THE SPEND JOURNAL, and the one whose truncation is not hypothetical: 500
    # events were lost from this file on 2026-09-07. The day and
    # month totals were restorable from a measurement taken minutes earlier;
    # the event list was not.
    atomic.write_json(WALLET, w)


def record_key_usage(pu: dict) -> None:
    """Persist what the provider just said about the KEY, with its stamp.

    `tools/build_findings.py` must not call the network — a findings build that
    depends on an unrelated service being reachable produces a different set of
    findings depending on the weather. So the one place that DOES call it writes
    down what it learned, and the findings read that offline with its age
    visible.

    A failure here can never stop spending: the caller has already been told
    what it asked for, and this is a record of it.
    """
    try:
        atomic.write_json(KEY_USAGE, {
            "checked_at": iso(),
            "daily": pu.get("daily"), "weekly": pu.get("weekly"),
            "monthly": pu.get("monthly"), "total": pu.get("total"),
            "limit": pu.get("limit"), "limit_remaining": pu.get("limit_remaining"),
            "limit_reset": pu.get("limit_reset"),
            "note": ("What the PROVIDER says this KEY has spent — the whole account, "
                     "shared with every other tool on this machine that uses it. "
                     "This project's own spend is in store/wallet.json and is what "
                     "its ceilings are measured against."),
        })
    except Exception:                                                             
        pass


def wallet_state() -> dict:
    w, cfg = _wallet(), config()["wallet"]
    day, month = now().strftime("%Y-%m-%d"), now().strftime("%Y-%m")
    window = timedelta(minutes=cfg["velocity_window_minutes"])
    recent = 0.0
    for e in w.get("events", []):
        try:
            t = datetime.strptime(e["at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if now() - t <= window:
            recent += e.get("cost", 0.0)
    local_day = round(w.get("days", {}).get(day, 0.0), 6)
    local_month = round(w.get("months", {}).get(month, 0.0), 6)
    pu = provider_usage()
    state = {
        "denomination": w.get("denomination", "credits"),
        # `source` describes where the KEY figures come from. The caps are
        # decided from this project's own journal, which is always
        # available, so the guard no longer depends on an unrelated service
        # being reachable.
        "source": "provider" if pu else "local journal (the provider was unreachable)",
        # KEPT for older readers, and it is the key's number when the provider
        # answers. `local_today` beside it is this project's.
        "today": round(pu["daily"], 6) if pu else local_day,
        "key_today": round(pu["daily"], 6) if pu else None,
        "key_month": round(pu["monthly"], 6) if pu else None,
        "daily_ceiling": cfg["daily_ceiling"],
        "month": round(pu["monthly"], 6) if pu else local_month,
        "monthly_ceiling": cfg["monthly_ceiling"],
        # Velocity is always local: the API reports day, week and month, never a
        # rolling window, and a rolling window is what catches a loop today.
        "window_spend": round(recent, 6),
        "velocity_ceiling": cfg["velocity_ceiling"],
        "window_minutes": cfg["velocity_window_minutes"],
        "calls": len(w.get("events", [])),
        "local_today": local_day,
        "local_month": local_month,
    }
    if pu:
        state["key_limit"] = pu["limit"]
        # `limit_remaining: null` is a key with NO limit, not one with nothing left:
        # reading it as 0.0 refused every call made with an unlimited key.
        state["key_remaining"] = (None if pu["limit_remaining"] is None
                                  else round(float(pu["limit_remaining"]), 6))
        state["key_limit_reset"] = pu["limit_reset"]
        state["key_total"] = round(pu["total"], 6)
    return state


def check_budget(provider: str = "openrouter") -> str | None:
    """None when spending is permitted, else the guardrail that says stop.

    Three independent mechanisms, one shared answer — a daily cap, a monthly cap,
    and spend velocity over a rolling window. The third exists because the first
    two catch a runaway tomorrow.

    **`provider` is not decoration.** The provider's own counters belong to the
    OpenRouter key. Embeddings are bought from OpenAI on a different key
    (OpenRouter serves none), so gating them on OpenRouter's figures asks the
    wrong meter two ways at once — another consumer of a shared OpenRouter key
    could stop this estate's indexing, and whether it did would depend on
    whether OpenRouter's usage endpoint answered: the same call could be refused
    interactively and permitted inside the gate, where the provider was
    unreachable and the state fell back to the local journal. A guard whose
    verdict depends on the reachability of an unrelated service is not a guard.

    For any other provider the caps are evaluated against THIS project's own
    journal, which `charge()` records for every provider alike, and the
    OpenRouter-specific key limit is not consulted at all. The velocity ceiling
    applies to everything: it is computed from local events and its question —
    "is something looping right now" — is provider-independent.
    """
    s = wallet_state()
    # THIS PROJECT'S OWN JOURNAL, for every provider alike. Reading the daily
    # and monthly caps from the PROVIDER's counters measures THE KEY — which may
    # be shared with whatever else on this machine uses the same provider. The
    # consequence is total: a neighbour's spending trips both ceilings, and the
    # agent, the indexer and the semantic half of `observatory_search` are all
    # disabled while the key still has credit and this system has spent almost
    # nothing. A ceiling must govern what the system CONTROLS; the docstring
    # above makes this argument for embeddings and it applies unchanged to the
    # agent.
    #
    # The drift a local journal could suffer is answered where it belongs: the
    # key's own remaining limit is checked first below, is enforced by the
    # provider, and cannot drift — so a journal that under-counts by one dead
    # call cannot overspend past it.
    today, month = s["local_today"], s["local_month"]
    source = "this project's journal"
    own_meter = provider == "openrouter"
    # The key's own limit is the outer backstop. It is checked first because it
    # is the one that returns a 402 instead of degrading, and knowing that is
    # about to happen is worth more than discovering it. It is OpenRouter's key,
    # so it is skipped for anything bought elsewhere.
    if own_meter and s.get("key_remaining") is not None and s["key_remaining"] <= 0:
        # ALL THREE FACTS, like the ceiling messages below. Saying only the key's
        # number reads as though this system spent the whole limit, when its own
        # journal may hold a tiny fraction of it. The share spent by other
        # consumers of the same key is named so that misreading cannot happen.
        others = max(float(s["key_total"]) - float(month), 0.0)
        return (f"the limit on this KEY is spent: {s['key_total']:.4f} of "
                f"{s['key_limit']} {s['denomination']}, resets "
                f"{s.get('key_limit_reset') or 'never'}. "
                f"this project's own journal holds {month:.4f} {s['denomination']} "
                f"for the month, so {others:.2f} was another consumer of the same key")
    if today >= s["daily_ceiling"]:
        # BOTH figures, because they can differ by orders of magnitude and the
        # difference is the whole story. The provider's own counters measure THE
        # KEY — and a key can be shared with whatever else on the machine uses
        # the same provider. Reporting only the key's figure against the ceiling
        # reads as "the observatory overspent", which can be false.
        # The figure that DECIDED is this project's own; the key's is context.
        extra = (f"; the key itself shows {s['key_today']:.4f} today, so "
                 f"{s['key_today'] - today:.4f} of that is another consumer"
                 if s.get("key_today") is not None else "")
        return (f"daily ceiling: {today:.4f} of {s['daily_ceiling']:.2f} "
                f"{s['denomination']} spent today by THIS project ({provider}, per "
                f"{source}){extra}")
    if month >= s["monthly_ceiling"]:
        extra = (f"; the key itself shows {s['key_month']:.4f} this month"
                 if s.get("key_month") is not None else "")
        return (f"monthly ceiling: {month:.4f} of {s['monthly_ceiling']:.2f} "
                f"{s['denomination']} spent this month by THIS project ({provider}, "
                f"per {source}){extra}")
    if s["window_spend"] >= s["velocity_ceiling"]:
        return (f"spend velocity: {s['window_spend']:.4f} {s['denomination']} in the last "
                f"{s['window_minutes']}min, ceiling {s['velocity_ceiling']:.2f} — "
                f"something is looping")
    return None


def charge(model_id: str, cost: float, tokens_in: int, tokens_out: int,
           upstream: float | None = None, provider: str = "openrouter",
           estimated: bool = False) -> str | None:
    """Record, prune, save — and return the guardrail that now says stop, if any.

    Recording and enforcing happen in the same breath on purpose.
    """
    w = _wallet()
    day, month = now().strftime("%Y-%m-%d"), now().strftime("%Y-%m")
    w.setdefault("days", {})[day] = w.get("days", {}).get(day, 0.0) + cost
    w.setdefault("months", {})[month] = w.get("months", {}).get(month, 0.0) + cost
    w.setdefault("events", []).append({
        "at": iso(), "model": model_id, "provider": provider, "cost": round(cost, 8),
        "estimated": estimated,
        "upstream": round(upstream, 8) if upstream is not None else None,
        "in": tokens_in, "out": tokens_out})
    # THE HORIZONS COME FROM the retention config, where every other horizon in
    # this project lives. As literals here, while also declared there and read by
    # nothing, the numbers would agree only by coincidence: changing the config
    # would do nothing and changing the code would make the config a lie.
    keep = retention_config()
    w["events"] = w["events"][-int(keep.get("wallet_events", 500)):]
    for d in sorted(w["days"])[:-int(keep.get("wallet_days", 60))]:
        del w["days"][d]
    for m in sorted(w["months"])[:-int(keep.get("wallet_months", 12))]:
        del w["months"][m]
    _save_wallet(w)
    global _provider_usage_cache
    _provider_usage_cache = None                                                
    return check_budget()


# ────────────────────────────── the call ─────────────────────────────────────

def _post(base_url: str, key: str, body: dict, timeout: int = 120) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/chat/completions", data=data, method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "User-Agent": UA, **attribution_headers(base_url)})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        if exc.code in (408, 409, 429) or exc.code >= 500:
            raise Retryable(f"HTTP {exc.code}: {detail}") from exc
        if exc.code in (401, 403):
            raise CredentialError(f"HTTP {exc.code}: {detail}") from exc
        raise Fatal(f"HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise Retryable(f"{type(exc).__name__}: {exc}") from exc


EMBED_KEY_FILES = ((pathlib.Path(os.environ["OBSERVATORY_EMBED_KEY_FILE"]),)
                   if os.environ.get("OBSERVATORY_EMBED_KEY_FILE") else
                   (paths.source_path("secret_store", paths.SECRETS) / 'openai',
                    paths.STATE / ".openai-key"))


#: What a key for each provider must look like. Checked because the environment
#: is not trustworthy: a variable meant for one provider can hold another's key,
#: which is then sent to the wrong host and answered 401 — while the correct key
#: sits unused in the secret store, shadowed by it. A prefix check turns that
#: into a refusal with a name.
KEY_SHAPES = {
    "OPENROUTER_API_KEY": ("sk-or-v1-", "OpenRouter"),
    "OPENAI_API_KEY": ("sk-", "OpenAI"),
}
#: An OpenRouter key also starts with OpenAI's prefix, so OpenAI's check needs the negative too.
KEY_ANTI_SHAPES = {"OPENAI_API_KEY": ("sk-or-",)}


#: Which variables have already been reported as holding the wrong shape. The
#: fact is about the environment, so it is worth saying once and no more.
_SHAPE_WARNED: set[str] = set()


def _shape_ok(env_name: str, value: str) -> str | None:
    """None when the value looks right for this provider, else what is wrong."""
    want = KEY_SHAPES.get(env_name)
    if not want:
        return None
    prefix, who = want
    if not value.startswith(prefix):
        return (f"it does not start with {prefix!r}, so it is not a {who} key. "
                f"A key in the wrong variable is sent to the wrong host and "
                f"answered with a 401 that names nothing.")
    for bad in KEY_ANTI_SHAPES.get(env_name, ()):
        if value.startswith(bad):
            return (f"it starts with {bad!r} — that is an OpenRouter key sitting in "
                    f"{env_name}. Sent to {who}'s host it answers 401, and meanwhile it "
                    f"shadows the correct key in the secret store.")
    return None


#: Where the shape verdicts are kept for a reader that is NOT this process.
#: `paths.STATE`, beside `key-usage.json`, because a literal under `paths.STORE`
#: cannot be redirected, and a test writing into the live store can truncate
#: the operator's wallet.
def _shapes_file():
    return paths.STATE / "key-shapes.json"


def shape_report() -> list[dict]:
    """One row per key variable THIS PROCESS CAN SEE, and never its value.

    A variable absent from the environment gets no row. That is the point: a row
    saying "absent" reads as "checked and fine", and the process best placed to
    check is not always the one that can — the scheduled tick inherits neither of
    these variables, so a misplaced key in the operator's shell never shows up in
    the tick's own log.

    Only the identifying PREFIX of a wrong value is carried. The verdict is
    unreadable without it and it is not key material; the rest, including any
    masked tail, never leaves this function.
    """
    rows: list[dict] = []
    for env_name, (prefix, who) in sorted(KEY_SHAPES.items()):
        value = os.environ.get(env_name)
        if not value:
            continue
        wrong = _shape_ok(env_name, value)
        row = {"env": env_name, "provider": who, "expected_prefix": prefix,
               "verdict": "wrong" if wrong else "ok"}
        if wrong:
            row["reason"] = wrong.split(". ")[0]
            # The prefix that DECIDES the verdict, taken from the anti-shapes
            # this module already declares rather than sliced off the value.
            row["looks_like"] = next(
                (bad for bad in KEY_ANTI_SHAPES.get(env_name, ())
                 if value.startswith(bad)), "")
        rows.append(row)
    return rows


def write_shape_report(rows: list[dict] | None = None) -> pathlib.Path:
    """Merge this process's verdicts into the receipt and return its path.

    MERGED, not replaced: two processes see two different environments, and a
    run that cannot see a variable must not erase another run's true observation
    of it. A run that CAN see one overwrites its row with a fresh stamp, so
    fixing the shell clears the finding on the next `project-observatory full key`.
    """
    rows = shape_report() if rows is None else rows
    f = _shapes_file()
    doc = {"observations": {}}
    if f.is_file():
        try:
            prior = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(prior.get("observations"), dict):
                doc["observations"] = prior["observations"]
        except (ValueError, OSError):
            # An unreadable receipt is replaced rather than allowed to block the
            # write: it holds no canon, only the last look at an environment.
            pass
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for r in rows:
        doc["observations"][r["env"]] = {**r, "seen_at": stamp}
    doc["written_at"] = stamp
    f.parent.mkdir(parents=True, exist_ok=True)
    atomic.write_json(f, doc)
    return f


def _read_from(env_name: str, files: tuple) -> tuple[str | None, str]:
    """Shared discipline for every key: environment first, then one file per
    secret at mode 600, and a loose file is refused rather than used.

    A value of the wrong SHAPE is skipped with a warning rather than used or
    silently ignored — the environment wins over a file, so a wrong value there
    hides a right one here, and that is the failure worth naming."""
    env = os.environ.get(env_name)
    if env and env.strip():
        wrong = _shape_ok(env_name, env.strip())
        if wrong:
            # ONCE PER PROCESS. The key path is consulted on every embedding
            # batch and every model call, so this printed 22 times in a single
            # `./observatory.py check` — measured 2026-09-07 — in the one log an
            # operator reads when something else has broken. The fact is about
            # the ENVIRONMENT and does not change during a run; repeating it
            # teaches a reader to skim exactly the stream that carries the real
            # message.
            if env_name not in _SHAPE_WARNED:
                _SHAPE_WARNED.add(env_name)
                print(f"  ignoring ${env_name}: {wrong}", file=sys.stderr)
        else:
            return env.strip(), f"${env_name}"
    for f in files:
        if not f.is_file():
            continue
        mode = f.stat().st_mode & 0o777
        if mode & 0o077:
            raise Fatal(f"{f} is mode {mode:o} — group or world readable. "
                        f"A key file must be 600.\n  chmod 600 {f}")
        value = f.read_text(encoding="utf-8").strip()
        if not value:
            continue
        wrong = _shape_ok(env_name, value)
        if wrong:
            raise Fatal(f"{f} holds something that is not a key for this provider: {wrong}")
        return value, str(f)
    return None, "nowhere"


def _with_local(files: tuple, name: str) -> tuple:
    """`files` with the local key at `paths.STATE / name` resolved (PB-091):
    the legacy store stands in when only it holds the key; both refuse."""
    import configuration
    try:
        where, note = paths.local_key(name)
    except configuration.ConfigurationError as exc:
        raise Fatal(str(exc)) from None
    if note and note not in _SHAPE_WARNED:
        _SHAPE_WARNED.add(note)
        print(f"  {note}", file=sys.stderr)
    return tuple(where if f == paths.STATE / name else f for f in files)


def read_embed_key() -> tuple[str | None, str]:
    files = ((pathlib.Path(os.environ["OBSERVATORY_EMBED_KEY_FILE"]),)
             if os.environ.get("OBSERVATORY_EMBED_KEY_FILE") else _with_local(EMBED_KEY_FILES, ".openai-key"))
    return _read_from(EMBED_KEY_ENV, files)


def embed(texts: list[str], log=print, *, authorization=None) -> dict:
    """Vectors for the pinned contract, or a refusal. Never a silent mismatch.

    The dimension the provider returns is checked against the declared contract
    on every call. A model that quietly returns a different width does not fail —
    it degrades cosine search, and nothing downstream can tell.

    **It checks its own ceiling, like `complete()` does.** It did not, and the
    asymmetry is why the class recurred three times: this module has exactly two
    functions that call `charge()`, one guarded itself and the other required
    every caller to remember. `agent/observe.py` remembered, `survey.py`
    remembered only after the wire audit found it spending on any MCP client's
    behalf, and `store/indexer.py:104` never did — it embeds a batch per tick,
    which is the largest spender of the three. A guard every caller must
    re-implement is a guard, singular, that is missing.

    `BudgetExceeded` derives from `ProviderError`, which the indexer already
    catches to fall back to the lexical index alone — so the ceiling arriving
    here turns an unmetered spend into the honest degradation that file already
    announces, through the handler it already has.
    """
    import configuration
    if not configuration.enabled("embeddings", "features"):
        raise Fatal("Feature embeddings is not enabled for this workspace")
    cfg = config()["embedding"]
    # THE DOOR (PB-137 N-003). Before the budget, the key and the request: text
    # leaves only under a remote verdict from embedding_policy, made for THIS
    # configured provider and model. A verdict for another model is a configuration
    # that changed between the decision and the call, and is refused the same way.
    _require_authorization(authorization, cfg)
    if not texts:
        return {"vectors": [], "tokens": 0, "cost": 0.0, "model": cfg["model"]}
    # For THIS provider's meter. `cfg["provider"]` is openai; the OpenRouter
    # counters and key limit describe a different key entirely.
    stop = check_budget(cfg["provider"])
    if stop:
        raise BudgetExceeded(stop)
    key, source = read_embed_key()
    if not key:
        raise Fatal(
            f"no embedding key. Looked in ${EMBED_KEY_ENV} and "
            + ", ".join(str(f) for f in EMBED_KEY_FILES)
            + f"\n  The estate already holds one; copy it in with:\n"
              f"    umask 077 && printf %s '<key>' > {EMBED_KEY_FILES[0]}")
    body = json.dumps({"model": cfg["model"], "input": texts}).encode("utf-8")
    req = urllib.request.Request(
        f"{cfg['base_url']}/embeddings", data=body, method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "User-Agent": UA, **attribution_headers(cfg["base_url"])})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        if exc.code in (401, 403):
            raise CredentialError(f"HTTP {exc.code}: {detail}") from exc
        if exc.code in (408, 409, 429) or exc.code >= 500:
            raise Retryable(f"HTTP {exc.code}: {detail}") from exc
        raise Fatal(f"HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise Retryable(f"{type(exc).__name__}: {exc}") from exc

    vectors = [d["embedding"] for d in sorted(raw.get("data", []),
                                              key=lambda d: d.get("index", 0))]
    if len(vectors) != len(texts):
        raise Fatal(f"asked for {len(texts)} vectors, got {len(vectors)}")
    for v in vectors:
        if len(v) != cfg["dims"]:
            raise Fatal(
                f"the contract declares {cfg['dims']} dimensions and the provider returned "
                f"{len(v)}. Two writers into one vector column with different widths do not "
                f"fail — they degrade cosine search silently, so this refuses instead.")
    tokens = int((raw.get("usage") or {}).get("total_tokens") or 0)
    # Estimated, not reported: OpenAI publishes no catalogue endpoint, so this
    # comes from a dated line in agent/models.json.
    cost = tokens * float(cfg["price_per_1m_tokens"]) / 1e6
    charge(f"{cfg['provider']}/{cfg['model']}", cost, tokens, 0, provider=cfg["provider"],
           estimated=True)
    return {"vectors": vectors, "tokens": tokens, "cost": cost, "model": cfg["model"],
            "key_source": source, "cost_is_estimate": True}


def _require_authorization(authorization, cfg: dict) -> None:
    if authorization is None or not getattr(authorization, "remote", False):
        reason = getattr(authorization, "reason", "no authorization")
        raise PolicyRefused(f"embedding-policy/1 did not authorize export ({reason})")
    if (getattr(authorization, "provider", None), getattr(authorization, "model", None)) != \
            (cfg.get("provider"), cfg.get("model")):
        raise PolicyRefused("embedding-policy/1 authorized another provider or model than "
                            "the one configured now; decide again")


def read_key() -> tuple[str | None, str]:
    """The chat provider's key. Same discipline as every other."""
    search = ((pathlib.Path(os.environ["OBSERVATORY_KEY_FILE"]),)
              if os.environ.get("OBSERVATORY_KEY_FILE") else _with_local(KEY_FILES, ".openrouter-key"))
    return _read_from(KEY_ENV, search)


def have_key() -> bool:
    """Can this run USE a credential? Not: does a file exist.

    It used to return True for a refused key — "a key exists; it is the
    permissions that are wrong" — and the distinction was real but no caller
    ever acted on it. Its only caller, `agent/observe.py`, took True to mean
    "proceed" and walked straight into `check_budget()`, which resolves the key
    again and does NOT catch the refusal: a key file at mode 644 produced an
    uncaught traceback in the scheduled tick instead of the degradation written
    for it.

    The distinction is kept where it is useful and lost where it was harmful.
    `key_status()` still answers `REFUSED — … chmod 600 …` for a loose file and
    `no key. Looked in: …` for an absent one, and the caller prints it in either
    case. A boolean that says yes to something unusable is not a distinction, it
    is a wrong answer."""
    try:
        key, _ = read_key()
    except Fatal:
        return False
    return bool(key)


def key_report() -> dict:
    """Whether the chat key is usable and WHERE it comes from — never a character
    of it, and no absolute path (this lands in app windows and doctor output).

    `key_source` exists because the environment wins: an `OPENROUTER_API_KEY`
    exported in the shell that starts the engine is the assistant's key, ahead
    of every file, so a person's own key could be spent with nothing on screen
    saying so. Now doctor and `assistant status` name the source."""
    try:
        key, where = read_key()
    except Fatal as exc:
        return {"key_status": "refused", "key_source": None,
                "key_note": str(exc).split("\n")[0][:200]}
    if not key:
        return {"key_status": "absent", "key_source": None}
    if where.startswith("$"):
        source = (f"environment: ${KEY_ENV}, inherited from the process that started the engine; "
                  f"it is used ahead of every key file")
    elif os.environ.get("OBSERVATORY_KEY_FILE"):
        source = "file: OBSERVATORY_KEY_FILE"
    elif pathlib.Path(where).name == ".openrouter-key":
        source = "file: the workspace's .openrouter-key (install_key.py --for observatory)"
    else:
        source = "file: the secret store's openrouter key"
    return {"key_status": "present", "key_source": source}


def key_status() -> str:
    """One line, for a human. Never prints the key, only its shape."""
    try:
        key, where = read_key()
    except Fatal as exc:
        return f"REFUSED — {exc}"
    if not key:
        places = "\n    ".join(f"${KEY_ENV}" if i == 0 else str(f)
                                for i, f in enumerate([None, *KEY_FILES]) if i or True)
        return (f"no key. Looked in:\n    ${KEY_ENV}\n    "
                + "\n    ".join(str(f) for f in KEY_FILES)
                + f"\n  Create one at https://openrouter.ai/keys, then:\n"
                  f"    umask 077 && printf %s '<key>' > {KEY_FILES[0]}")
    # PRESENCE AND LENGTH, plus the provider's own prefix when the key carries
    # it — never a character of the key itself: a masked tail still lands in a
    # transcript and narrows a search.
    prefix = KEY_SHAPES["OPENROUTER_API_KEY"][0]
    shape = (prefix + "…, " if key.startswith(prefix) else "unrecognised prefix, ") + f"length {len(key)}"
    return f"present ({shape}) from {where}"


def scheduled_key_status() -> str:
    """What the TICK will resolve, which is the one that spends.

    `key_status()` answers for the shell it is run in, and the shell exports
    `OPENROUTER_API_KEY` on this machine — so an operator who had just given the
    project its own key file ran this command, saw the environment's key, and
    reasonably concluded the file had not taken (2026-09-10). It had: launchd
    passes only HOME and PATH, so the scheduled run never sees that variable and
    reads the file. Two callers, two answers, and until now the command showed
                

    Computed by asking the same resolver with the key variables removed, rather
    than by reasoning about them — a claim about what another process will do is
    worth having only if something derives it.
    """
    import os as _os
    saved = {k: _os.environ.pop(k) for k in (KEY_ENV, "OPENAI_API_KEY")
             if k in _os.environ}
    try:
        return key_status()
    finally:
        _os.environ.update(saved)


def complete(messages: list[dict], *, schema_name: str, schema: dict,
             requested: str | None = None, log=print) -> dict:
    """One structured completion, over the chain, capped in total attempts.

    Returns {parsed, model, cost, tokens_in, tokens_out, attempts, stop}.
    Raises BudgetExceeded before spending, Fatal when no attempt can help, and
    Retryable only when every attempt was retryable.
    """
    import configuration
    if not configuration.enabled("agent", "features"):
        raise Fatal("Feature agent is not enabled for this workspace")
    key, source = read_key()
    if not key:
        raise Fatal(f"no OpenRouter key. {key_status()}")
    log(f"  key from {source}")
    stop = check_budget()
    if stop:
        raise BudgetExceeded(stop)

    cfg = config()
    chain, level, provenance = resolve_chain(requested)
    log(f"  chain from {level} ({provenance}): "
        + ", ".join(f"{m['id']} ${m['price_in_per_1m']:.3f}/${m['price_out_per_1m']:.3f}"
                    for m in chain))

    budget_attempts = cfg.get("attempts", {}).get("total", 4)
    attempts, last_retryable = 0, None
    for model in chain:
        why_not = unhealthy(model["id"])
        if why_not:
            log(f"  skipping {model['id']}: marked unhealthy — {why_not}")
            continue
        if not needs_structured(model):
            log(f"  skipping {model['id']}: the catalogue says it has no structured_outputs")
            continue
        while attempts < budget_attempts:
            attempts += 1
            body = {
                "model": model["id"],
                "messages": messages,
                "response_format": {"type": "json_schema",
                                    "json_schema": {"name": schema_name, "strict": True,
                                                    "schema": schema}},
                # Routes only to endpoints that actually support the response
                # format, instead of discovering they do not by a 400.
                "provider": {"require_parameters": True},
                "max_tokens": 2048,
            }
            try:
                raw = _post(cfg["base_url"], key, body)
            except Retryable as exc:
                last_retryable = exc
                log(f"  {model['id']} attempt {attempts}/{budget_attempts} retryable: {exc}")
                mark_unhealthy(model["id"], str(exc))
                break                                                                   
            except CredentialError:
                # The key is the problem, not this model. Marking three models
                # unhealthy for one dead credential is how a chain comes back
                # empty after the key is fixed.
                raise
            except Fatal as exc:
                mark_unhealthy(model["id"], str(exc))
                log(f"  {model['id']} fatal: {exc}")
                break

            usage = raw.get("usage") or {}
            cost = float(usage.get("cost") or 0.0)
            upstream = (usage.get("cost_details") or {}).get("upstream_inference_cost")
            tin = int(usage.get("prompt_tokens") or 0)
            tout = int(usage.get("completion_tokens") or 0)
            if cost == 0.0:
                # The provider did not report a cost. Estimate from the catalogue
                # rather than record zero: an unpriced call still spent money.
                cost = estimate(model, tin, tout)
                log(f"  {model['id']}: no cost reported; estimated {cost:.6f} from the catalogue")
            stop = charge(model["id"], cost, tin, tout,
                          float(upstream) if upstream is not None else None)

            choices = raw.get("choices") or []
            if not choices:
                mark_unhealthy(model["id"], "no choices in the response")
                log(f"  {model['id']}: response carried no choices")
                break
            content = (choices[0].get("message") or {}).get("content") or ""
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError as exc:
                # Paid for, and unusable. Not retryable at the same model: a
                # schema it cannot honour will not start honouring it.
                mark_unhealthy(model["id"], f"unparseable structured output: {exc}")
                log(f"  {model['id']}: structured output did not parse — {exc}")
                break
            mark_healthy(model["id"])
            return {"parsed": parsed, "model": model["id"], "cost": cost,
                    "tokens_in": tin, "tokens_out": tout, "attempts": attempts,
                    "finish_reason": choices[0].get("finish_reason"), "stop": stop,
                    "selection_level": level}
        if attempts >= budget_attempts:
            break
    if last_retryable:
        raise Retryable(f"every model in the chain failed; last: {last_retryable} "
                        f"({attempts} attempt(s) of {budget_attempts})")
    raise Fatal(f"no model in the chain could serve the request "
                f"({attempts} attempt(s) of {budget_attempts})")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Inspect the provider boundary.")
    ap.add_argument("what", choices=["chain", "wallet", "health", "refresh", "key"])
    a = ap.parse_args()
    if a.what == "refresh":
        models, prov = catalogue(refresh=True)
        print(f"catalogue {prov}: {len(models)} models")
    elif a.what == "chain":
        chain, level, prov = resolve_chain()
        print(f"selection level: {level} · catalogue {prov}\n")
        # A CHAIN THAT LOST A MODEL SAYS SO. Printed before the survivors, not
        # after: a reader who sees three lines where the config names four has
        # no way to know unless the loss is stated where they are looking.
        gone = (chain[0] if chain else {}).get("chain_retired") or []
        if gone:
            print(f"\033[33m{len(gone)} configured model(s) are NOT in the "
                  f"catalogue and were skipped: {', '.join(gone)}\033[0m")
            print("  The chain still works with what is left; check "
                  "agent/models.json against the provider.\n")
        for m in chain:
            flag = unhealthy(m["id"]) or ("ok" if needs_structured(m) else "NO structured_outputs")
            print(f"  {m['id']:<34} ${m['price_in_per_1m']:>7.3f}/${m['price_out_per_1m']:>7.3f} "
                  f"ctx {m['context_length']:>9,}  [{flag}]")
            if m.get("why"):
                print(f"      {m['why']}")
    elif a.what == "wallet":
        s = wallet_state()
        print(json.dumps(s, indent=1))
        print("guardrail:", check_budget() or "none — spending is permitted")
    elif a.what == "key":
        here = key_status()
        print(here)
        # AND WHAT THE SCHEDULE WILL USE. The tick is what spends, and it does
        # not inherit this shell — so the answer above is about the operator's
        # terminal and not about the system. Printed always, and only called out
        # as a difference when the two disagree.
        there = scheduled_key_status()
        if there != here:
            print(f"  the scheduled tick resolves a DIFFERENT key: {there}")
            print("  (launchd passes only HOME and PATH, so the shell's "
                  "variables are invisible to it — the tick is what spends)")
        else:
            print("  the scheduled tick resolves the same key")
        # EVERY key variable, not only the active provider's. `key_status()`
        # reports only the key this chain resolves, so the EMBEDDING key
        # (`OPENAI_API_KEY`) is never mentioned by it even when it holds the
        # wrong kind of key. The receipt is what lets `tools/build_findings.py`
        # say so on a surface: this step is deliberately outside the tick,
        # because the tick does not inherit the shell where these variables are
        # set.
        rows = shape_report()
        for r in rows:
            if r["verdict"] == "wrong":
                print(f"  ${r['env']}: WRONG SHAPE — {r.get('reason', '')}")
            else:
                article = "an" if r["provider"][0] in "AEIOU" else "a"
                print(f"  ${r['env']}: looks like {article} {r['provider']} key")
        if not rows:
            print("  no key variable is set in this environment — nothing to check")
        print(f"  recorded in {write_shape_report(rows)}")
    else:
        print(json.dumps(_health(), indent=1) or "no model marked unhealthy")
