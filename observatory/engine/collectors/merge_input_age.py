"""Input timestamps for the merge; a new model does not renew old measurements."""
from datetime import datetime, timezone
import json
import stat
from pathlib import Path

FRESHNESS_HOURS = {'local.json': 2, 'vault.json': 2, 'sessions.json': 2,
                   'remotes.json': 6, 'bitbucket.json': 6, 'gh/': 6,
                   'env.json': 48, 'heroku.json': 48, 'openrouter.json': 48,
                   'domains_live.json': 48, 'cloudflare_zones.json': 48}
STAMP_FIELDS = ('measured_at', 'scanned_at', 'generated_at', 'observed_at', 'at', 'scanned_on', 'observed_on')

def survey(root: Path, *, now=None):
    now = now or datetime.now(timezone.utc)
    inputs, degraded = {}, []
    candidates = [(root / name, hours) for name, hours in FRESHNESS_HOURS.items() if not name.endswith('/')]
    try:
        candidates += [(p, 6) for p in (root / 'gh').iterdir() if p.name.endswith('.json') and not p.name.startswith('_')]
    except FileNotFoundError:
        pass
    except OSError:
        degraded.append({'source': 'gh/', 'reason': 'input age could not be measured: listing unavailable'})
    for path, hours in candidates:
        name = path.relative_to(root).as_posix()
        try:
            info = path.stat()
            if not stat.S_ISREG(info.st_mode):
                raise ValueError('regular input required')
            doc = json.loads(path.read_text(encoding='utf-8'))
            stamp = next((doc[field] for field in STAMP_FIELDS if field in doc), None) if isinstance(doc, dict) else None
            # Legacy list outputs have no timestamp. Their file time is explicitly
            # the fallback; an invalid declared timestamp is never overwritten.
            declared = isinstance(doc, dict) and any(field in doc for field in STAMP_FIELDS)
            measured = datetime.fromisoformat(str(stamp).replace('Z', '+00:00')) if declared else datetime.fromtimestamp(info.st_mtime, timezone.utc)
            date_only = measured.tzinfo is None and isinstance(stamp, str) and len(stamp) == 10
            if date_only:
                # scan_sessions emits a UTC date, not an instant. Midnight is a
                # conservative lower bound; a copied file must not renew it.
                measured = measured.replace(tzinfo=timezone.utc)
            if measured.tzinfo is None:
                raise ValueError('timezone required')
            age = (now - measured).total_seconds() / 3600
            inputs[name] = measured.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
            if age < -1 / 60:
                degraded.append({'source': name, 'reason': 'input timestamp is in the future; freshness is unknown'})
            elif date_only and age <= 24 + hours:
                degraded.append({'source': name, 'reason': f'input has a date-only measurement time; freshness within {hours} h is unknown'})
            elif age > hours:
                if date_only:
                    age = max(0, age - 24)  # Even the last instant of that day is old.
                degraded.append({'source': name, 'reason': f'input is {age:.1f} h old; its collector has not run since (freshness bound {hours} h)'})
        except FileNotFoundError:
            continue  # The merge's source reader owns missing-source degradation.
        except (OSError, ValueError, TypeError, OverflowError):
            inputs[name] = None
            degraded.append({'source': name, 'reason': 'input age could not be measured: unreadable data or invalid timestamp'})
    return inputs, degraded
