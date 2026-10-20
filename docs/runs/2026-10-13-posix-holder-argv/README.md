# POSIX rendered argv safety correction

Status: independent scoped source/synthetic review passed at `22ea6f4`; draft delivery.
Base: accepted Windows source `ff13f861390dd5b575a1b7ec62c0565ff4131fdb` (PR272).

## Why this changed

A closed-script process remains alive while lsof reports no target handle. The
old rendered-ps parser missed absolute paths containing spaces, relative paths
with and without spaces, and an external symlink alias. An ordinary absolute
path remained a positive control. Five disposable macOS child processes and
own-PID ps/lsof observations reproduced this without another process's argv or
any cleanup action. Raw command lines are not persisted.

Apple's [pinned ps implementation](https://github.com/apple-oss-distributions/adv_cmds/blob/6bed8737a34dbb54782a18f47dccf933a9967a12/ps/print.c#L179-L240)
replaces argv terminators with spaces. Shell parsing cannot reconstruct those
boundaries. Linux [cmdline](https://man7.org/linux/man-pages/man5/proc_pid_cmdline.5.html)
preserves NUL delimiters for ordinary processes, but arguments are process-mutable;
[current cwd](https://man7.org/linux/man-pages/man5/proc_pid_cwd.5.html) does not
recover launch cwd after chdir. Native argv collection is outside this correction.

## Contract

Production holder inventories retain observed paths but remain incomplete. A
fresh target probe retains positive file/command holds and otherwise returns
unknown; current production path-removal tiers therefore refuse. A nonempty
cache busy-tool request likewise cannot be cleared by unmatched rendered text.
Empty requested tool sets are vacuously false, and generic commands without
holder preconditions retain their separate configured policy. Manually created
complete Holders snapshots remain an explicit legacy test/consumer API, not a
claim about production observation completeness. No owner lease, native collector,
installed setting or live cleanup is introduced.

## Validation scope

Tests-first: seven new test methods produced ten failing outcomes on the old
production contract. An earlier attempt also had two Git-fixture routing errors;
those were corrected before the ten-failure reproduction. Controls include four
path forms, direct/lexical positives, incomplete global snapshots, actual disk
and clean/archived dirty worktree admission with a stale free snapshot, and
cache-command child suppression. The focused corrected seven methods pass.
Final portable and independent source results are recorded in `checks.json`.

## Windows CI correction carried forward

PR272's native FILE_SHARE_DELETE rename control passed, but its strict class had
three passes and one setup error because the synthetic home omitted USERPROFILE.
This successor defines both owned HOME and USERPROFILE; a Windows path-resolution
assertion preserves the fixture boundary without weakening the native control.
Native successor execution remains pending.

The Windows job also still had job-level continue-on-error, so a strict step
alone did not enforce workflow failure. A tests-first structural gate now checks
both job and holder-step levels and preserves historical informative steps. The
job-level override is removed. This supersedes any claim that ff13's strict
holder step alone guaranteed workflow enforcement. It does not turn historical
informative Windows failures/skips into passing acceptance.

Next: scoped independent review, current/history privacy and a draft successor
stacked on PR272; hosted/native evidence remains separate. No release or install.

## Final local check

The final disk-guard suite ran 148 cases: 146 passed and two explicitly skipped. Git hardening
passes 61 cases; six documentation-copy checks and four workflow checks pass.
Python3.11 compiles the three changed Python files. Source inventory and copied
docs match. Brand lint reports zero errors and 2043 warnings (non-strict exit 0);
there is no repository UX-lint script, so no such execution is claimed.

The earlier disk-guard run retained one failure: a pre-existing two-second
archive fixture expired before reaching its intended final-holder assertion.
That fixture now allows ten seconds for setup/archive while still asserting a
five-second maximum for the final probe. The separate expired-deadline refusal
remains unchanged. This is synthetic fixture stabilization, not a production
budget increase. Native Windows and hosted successor checks remain pending.

## Independent corrected-source verdict

Exact `22ea6f44278b9a94cdb6a8749c1c0d9808530616` passed scoped source/synthetic
review: five actual owned child-process controls now yield unknown for the four
original missing path forms and true for the ordinary absolute path. Seven
additional controls cover destructive admission, cache child suppression, lsof/proc
completeness, malformed/timeout/decode evidence and the owned Windows home fixture.
The reviewer compared complete workflow YAML semantics: only the job-level
Windows exception was removed; strict holder step and job are both unsuppressed.
This documentation successor changes no runtime, tests or workflow bytes. Native
Windows successor and installed/live cleanup acceptance remain unmeasured.
