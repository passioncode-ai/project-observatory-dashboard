# An unread ceiling cannot supply current headroom

After the fractional-counting correction, a separate synthetic counterexample
confirmed that `_counted` used a saved ledger ceiling when its provider lookup
failed. Starting with a recorded $5 key, an external change raised its actual
ceiling to $8. With provider reads unavailable, a two-slot $10 policy counted
only the saved $5 and issued another $5 key. The engine POST moved the total
from $8 to $13. [Original evidence](unread-counterexample.json) binds source
`f24b3ca`; this is not a live incident report.

`held-unread` now counts as unknown, which supplies no admission headroom under
a finite policy cap. A confirmed missing identity still counts zero; a pending
mint retains its separate reservation semantics. Restoring a current provider
read permits lowering the observed $8 key to $5 before issuing the second $5
key. No blind raise or retry is introduced.

The regression was added first and failed three assertions. After the bounded
correction, seven focused functions passed 47 assertions. [Receipt](unread-tests.json)
binds the production source. It checks unchanged provider state under the
refusal, distinct missing/pending semantics and the successful read/lower/issue
path. The existing unread-key regression now expects conservative unknown
exposure rather than a claimed current ledger bound.

The planner's existing `unbounded` diagnostic means no proven finite bound,
including an unread observation; it is not measured spend. Pending reservations,
external writes after a successful read and complete consumer reconciliation
remain separate limitations. Next: independently review this exact source,
compose both strict admission corrections with the accepted test adapter, and
remeasure the original 5200 cases. No live provider calls occurred.
