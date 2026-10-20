# Observation and cleanup source composition

This candidate combines the scoped, independently reviewed observation and cleanup
followups on release source `12b7343277004aa408283fb4aef06ccbae935954`.
It does not integrate current main, settle financial acceptance, install a build,
or authorize cleanup. Individual PR branches remain unchanged.

## Components and preservation

The fix-only sequence is PR261, PR265, PR269, PR271, PR266, PR263, PR264,
PR267, PR272, then PR275. Initial rejected commits are always paired with their
accepted correction successors. Only each PR's own delta is applied: the tick
and runner branches' unrelated financial ancestry is excluded.

[Source map](source-map.json) records every original-to-delivery commit, the exact
base/checkpoint/tree and source hashes. The final changed path set equals the
union of component paths. Financial `openrouter.py` and
`test_credential_doors.py` remain byte-identical to the release base. All other
runtime/test/workflow files equal their last accepted component except the
portable runner, whose changes are only the union of suite registrations and
source-copy modules. Its functions/classes equal PR267's accepted source.

Documentation conflicts preserve the additive descriptions for people and agents;
engine copies are generated from root documents and the source inventory is
regenerated from the combined tree. The runner includes new unittest suites
without assertion-line exemptions. Production holder absence remains unknown;
local checkout observations remain offline, bounded, and non-actionable.

## Verification boundary

The runtime checkpoint is `69056ec1611efa6937383ab52e57a06c1a289212`.
Independent source-composition review and seven composed control groups passed.
Validation remains **PARTIAL**: the original13-suite portable run exited1, with
12 suites passing448 executed cases and two explicit skips; the full maintenance
suite timed out after481.87seconds without a terminal summary. Its completed
case count is unknown. No assertion failure was printed before timeout, but
that does not prove the uncompleted tests passed or identify the timeout cause.

A separate bounded unchanged-source retry passed the five changed/adjacent
retention cases in12.157seconds. The12-case Reinstall class also passed in
102.032seconds. These diagnose the relevant subset and restoration path; they
do not supersede the full-suite timeout or establish its cause. Eight root
suites passed60 cases, including runner failure evidence, packaging/CLI,
document copies and Windows workflow structure. Python3.11 parsed27 changed
Python files. [Checks](checks.json) preserves statuses and scope separately.

The runner's functions/classes are AST-identical to accepted PR267; its260 suites
are unique, with the three added suites using unittest evidence, and all added
source modules included once. Native Windows on the combined candidate, full
hosted checks, current-main integration, live providers, installed service
behavior and release acceptance are not established by this packet.

Historical source receipts remain in their component run packets. In particular,
the old native Windows setup failure and inherited financial hosted failures are
not rewritten as passes. The component PR275 at `c7ce68a8f71ef7702602812fd7a900cd99b6d414` subsequently
passed all five native `WindowsHolderUnknown` cases, with no skips, in
[job115228019588](https://github.com/passioncode-ai/project-observatory-dashboard/actions/runs/38377071511/job/115228019588).
Its historical informative root step separately reported175 cases, two failures
and ten skips; that is not passing acceptance. Native execution of this combined
candidate remains NOT_RUN. Pending cadence and holder-capture work are excluded.

Next: retain the unresolved full maintenance timeout, complete the exact
current/history privacy gate, and publish a draft handoff without merging. A
later acceptance run must establish full maintenance completion separately.
