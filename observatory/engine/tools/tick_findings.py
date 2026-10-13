"""Interpret tick receipts without calling a skipped measurement fresh."""
from __future__ import annotations
import math
import pathlib
import sys
import time
sys.path.append(str(pathlib.Path(__file__).resolve().parents[1] / 'dashboard'))
from finding_types import titled

CORE_TAIL = {'merge', 'emit', 'validate'}

def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def findings(tick, rotation, *, now=None, config=None, severe=None):
    now = time.time() if now is None else now
    config = config if isinstance(config, dict) else {}
    severe = severe if isinstance(severe, dict) else {}
    out = []
    tick = tick if isinstance(tick, dict) else {}
    for row in tick.get('failed_steps', []) if isinstance(tick.get('failed_steps', []), list) else []:
        if not isinstance(row, dict):
            continue
        name, outcome = str(row.get('step', 'unknown')), row.get('outcome')
        grave = severe.get(name)
        out.append({'type': 'tick.step_failed', 'subject': 'step:' + name,
                    'severity': 'critical' if grave else 'info' if outcome == 'not_started' and name not in CORE_TAIL else 'warning',
                    **(titled('the scheduled tick did not start `{step}`: its time ran out', step=name)
                 if outcome == 'not_started' else
                 titled('the scheduled tick stopped `{step}` at its time limit', step=name)
                 if outcome == 'stopped' else
                 titled("the scheduled tick's `{step}` step exited {code}", step=name, code=row.get('exit'))),
                    'detail': (grave or ('This core step did not complete; the tick stopped before replacing further outputs.' if name in CORE_TAIL else 'The tick does not abort on a degradation; this step did not complete and its previous output may still be in use.'))
                              + ' Last tick finished ' + str(tick.get('finished_at', 'unknown')) + '.',
                    'action': 'read store/logs/tick.log; resolve the reported limit or step failure before rerunning the affected step',
                    'evidence': ['store:raw/tick.json', 'store:logs/tick.log']})
    if rotation is None and not tick.get("collector_order"):
        return out

    def degraded(reason, action=None):
        out.append({'type': 'tick.rotation_degraded', 'subject': 'tick:rotation', 'severity': 'warning',
                    **titled('collector scheduling coverage is unknown'), 'detail': reason,
                    'action': action or 'check the installed engine version and tick-rotation.json schema; run a compatible tick before judging collector freshness',
                    'evidence': ['store:tick-rotation.json', 'store:raw/tick.json']})

    if not isinstance(rotation, dict) or rotation.get('schema') != 2 or not isinstance(rotation.get('collectors'), dict):
        degraded('The rotation receipt has an unsupported or unreadable schema; missing starvation findings are not proof of healthy scheduling.')
        return out
    planned = tick.get('collector_order')
    if not isinstance(planned, list):
        degraded('The tick receipt has no collector plan; planned and unplanned collectors cannot be distinguished.')
        return out
    threshold = number(config.get('starve_hours'))
    threshold = threshold if threshold is not None and threshold > 0 else 6
    invalid = False
    completion_unknown = False
    for name in dict.fromkeys(n for n in planned if isinstance(n, str)):
        row = rotation['collectors'].get(name)
        if not isinstance(row, dict):
            invalid = True
            continue
        outcome = row.get('outcome')
        if any(value is not None and (number(value) is None or number(value) < 0)
               for value in (row.get('started_at'), row.get('finished_at'), row.get('last_ran_at'), row.get('not_started_streak'))):
            invalid = True
            continue
        if row.get('not_started_streak') is not None and number(row['not_started_streak']) % 1:
            invalid = True
            continue
        if outcome is None or (outcome == 'ran' and row.get('last_ran_at') is None):
            completion_unknown = True
        if outcome == 'disabled':
            continue
        if outcome not in ('ran', 'failed', 'stopped', 'not_started', None):
            invalid = True
            continue
        last, streak = number(row.get('last_ran_at')), number(row.get('not_started_streak'))
        hours = max(0, now - last) / 3600 if last is not None else None
        if (streak or 0) < 3 and (hours is None or hours <= threshold):
            continue
        status = 'interrupted or completion unknown' if outcome is None else outcome
        out.append({'type': 'tick.collector_starved', 'subject': 'collector:' + name,
                    'severity': 'critical' if name in ('leaks', 'integrity') else 'warning',
                    **(titled('the `{step}` collector has not run for {hours} hours', step=name, hours=f'{hours:.1f}')
                       if hours is not None else titled('the `{step}` collector has no completed measurement after repeated skipped attempts', step=name)),
                    'detail': f'Planned in the last tick. Last start: {row.get("started_at")}; last outcome: {status}; not-started streak: {streak or 0:g}.',
                    'action': 'check the collector limit and earlier steps in store/logs/tick.log; run the affected collector with sufficient time, then verify its measurement timestamp',
                    'evidence': ['store:tick-rotation.json', 'store:raw/tick.json']})
    reasons, remedies = [], []
    if invalid:
        reasons.append('A planned collector has a missing or invalid rotation row; its scheduling coverage is unmeasured.')
        remedies.append('inspect the collector receipt and tick log, then rerun the affected collector to establish a valid completion record')
    if completion_unknown:
        reasons.append('A planned collector has unknown completion; an interrupted first run is not a healthy completed measurement.')
        remedies.append('inspect the tick log for the interrupted collector and rerun it before relying on its coverage')
    # One receipt owns one stable acknowledgement ID, even when several causes
    # coexist. Duplicate IDs would make the entire findings rebuild refuse.
    if reasons:
        degraded(' '.join(reasons), '; '.join(remedies))
    return out
