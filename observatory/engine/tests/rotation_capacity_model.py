"""Offline conditional model, NOT a provider adapter or production recovery planner.

All flags describe synthetic assumptions, never grants or measured provider proof.
No output grants mutation authority. The original full adversary is separate and
unchanged; these bounded traces explain safety/liveness prerequisites only.
"""
from decimal import Decimal, InvalidOperation, ROUND_FLOOR

STAGES = ('lower', 'mint', 'deliver', 'delete', 'restore')
BOUNDARIES = ('before-intent', 'after-intent', 'before-effect', 'after-effect', 'after-confirm')
CENT = Decimal('0.01')


def amount(value):
    if type(value) not in (str, int, float, Decimal) or len(str(value)) > 64:
        raise ValueError('invalid-amount')
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ValueError('invalid-amount') from None
    if not result.is_finite() or result < 0 or result > Decimal('1000000000000'):
        raise ValueError('invalid-amount')
    # Bounded precision is explicit, not rounding into a different policy.
    if result.as_tuple().exponent < -6:
        raise ValueError('unsupported-precision')
    return result


def text(value):
    return format(value.normalize(), 'f')


def valid_policy(limit, slots):
    try:
        cap = amount(limit)
        return type(slots) is int and slots > 0 and cap % CENT == 0 and cap >= slots * CENT
    except ValueError:
        return False


def propose(*, policy, other, predecessor, successor, availability_admitted=False,
            bounded_writes=False, current_reads=False, closed_world=False):
    """Pure numerical proposal. Synthetic assumption flags are never authorization."""
    out = {'schema': 'synthetic-capacity-transfer/1', 'actionable': False,
           'status': 'blocked', 'original_bound': None, 'missing': []}
    try:
        cap, rest, old, new = map(amount, (policy, other, predecessor, successor))
    except ValueError:
        out['missing'] = ['invalid-or-unknown-amount']; return out
    original = rest + old
    out['original_bound'] = text(max(cap, original))
    if cap <= 0 or cap % CENT or new <= 0 or new % CENT:
        out['missing'].append('whole-cent-positive-policy-and-successor-required')
    if original > cap: out['missing'].append('already-over-policy')
    lower = min(old, cap - rest - new).quantize(CENT, rounding=ROUND_FLOOR)
    if lower < CENT: out['missing'].append('no-positive-cent-predecessor-capacity')
    if lower < old and availability_admitted is not True:
        out['missing'].append('availability-contract-missing')
    for flag, reason in ((bounded_writes,'provider-cap-write-bound-unproved'),
                         (current_reads,'authoritative-current-reads-unproved'),
                         (closed_world,'external-writer-exclusion-unproved')):
        if flag is not True: out['missing'].append(reason)
    if out['missing']: return out
    out.update(status='conditional-proposal', policy=text(cap), other=text(rest),
               predecessor=text(old), successor=text(new), lower_to=text(lower),
               maximum_overlap=text(rest + lower + new), next='offline-model-only')
    return out


def simulate(plan, *, fault=None, holders_complete=True, old_unused=True,
             value_preserved=True, external_added='0', mint_overshoot='0', predecessor_usage='0'):
    """Explore before/after each intent, effect and confirmation under exact caps.

    Faults after an intent retain maximum possible exposure even if the fake
    effect did not occur. A missing one-time value cannot be recovered by this
    model. External drift and provider overshoot deliberately falsify assumptions.
    This does not implement durable I/O, provider consistency or live holder reads.
    """
    out = {'schema': 'synthetic-capacity-trace/1', 'actionable': False, 'trace': [],
           'status': 'blocked', 'missing': list(plan['missing']), 'next': 'read-current-evidence'}
    if plan['status'] != 'conditional-proposal': return out
    if fault is not None and (fault[0] not in STAGES or fault[1] not in BOUNDARIES):
        raise ValueError('unknown-fault-boundary')
    old, new = amount(plan['predecessor']), Decimal(0)
    old_upper, new_upper = old, Decimal(0)
    other, bound = amount(plan['other']), amount(plan['original_bound'])
    lower, target = amount(plan['lower_to']), amount(plan['successor'])
    identified = available = delivered = False
    consumer, pending = 'predecessor', None

    def snapshot(stage, boundary):
        row = {'stage': stage, 'boundary': boundary, 'pending': pending,
               'actual_total': text(other + old + new), 'upper_total': text(other + old_upper + new_upper),
               'predecessor_actual': text(old), 'successor_actual': text(new),
               'predecessor_upper': text(old_upper), 'successor_upper': text(new_upper),
               'original_bound': text(bound), 'successor_identified': identified,
               'value_available': available, 'delivery_confirmed': delivered, 'consumer': consumer,
               'predecessor_unserviceable': consumer == 'predecessor' and old < amount(predecessor_usage),
               'within_bound': other + old + new <= bound,
               'represented': other + old + new <= other + old_upper + new_upper <= bound}
        out['trace'].append(row); out['final'] = row
        if not row['within_bound'] or not row['represented']:
            out['status'] = 'assumption-violation'; return True
        if fault == (stage, boundary):
            out['status'] = 'interrupted'; out['fault'] = [stage, boundary]
            if pending == 'mint' and not identified:
                out['missing'].append('recover-identity-and-one-time-value')
            return True
        return False

    for stage in STAGES:
        if snapshot(stage, 'before-intent'): return out
        if stage == 'deliver' and value_preserved is not True:
            available = False
            snapshot(stage, 'value-unavailable')
            out['missing'].append('one-time-value-unavailable'); return out
        if stage == 'delete' and (holders_complete is not True or old_unused is not True or not delivered):
            out['missing'].append('fresh-complete-holder-proof-required'); return out
        pending = stage
        if stage == 'mint': new_upper = target  # record reservation BEFORE effect
        if stage == 'restore': new_upper = amount(plan['predecessor'])  # old already confirmed absent
        if snapshot(stage, 'after-intent'): return out
        if snapshot(stage, 'before-effect'): return out
        if stage == 'lower': old = lower
        elif stage == 'mint':
            new = target + amount(mint_overshoot)
            other += amount(external_added)
        elif stage == 'deliver': consumer = 'successor'
        elif stage == 'delete': old = Decimal(0)
        elif stage == 'restore': new = amount(plan['predecessor'])
        if snapshot(stage, 'after-effect'): return out
        # This is an authoritative observation + durable receipt ASSUMPTION.
        # A crash before it retains the prior upper bound, never actual-as-known.
        if stage == 'lower': old_upper = lower
        elif stage == 'mint': identified = available = True
        elif stage == 'deliver': delivered = True
        elif stage == 'delete': old_upper = Decimal(0)
        elif stage == 'restore': new_upper = new
        pending = None
        if snapshot(stage, 'after-confirm'): return out
    out['status'] = 'conditional-completion'
    out['next'] = 'independent-contract-review'
    return out

