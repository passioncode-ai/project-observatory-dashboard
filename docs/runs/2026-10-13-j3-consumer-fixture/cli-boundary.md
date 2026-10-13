# Revoke replay uses the real CLI exception boundary

The two ERRORs in the original full measurement escaped from the model's direct
`cmd_revoke` call when the synthetic save hook raised OSError. The actual
`tools/openrouter.py` entry point catches exactly OSError, ValueError and
RuntimeError, with a fixed refusal message and exit code 2.

The model's `revoke` adapter now returns 2 for that same tuple. It does not clear
hooks, undo effects, alter the ledger, erase consumers or bypass invariant
checks. Unexpected exception classes still escape as unjudged errors. Rotation
and other direct-call wrappers are unchanged; future evidence must justify any
separate adapter correction rather than treating every exception as a pass.

Two tests were added first and both errored before the correction. The complete
focused model suite then passed 21 tests; [receipt](raw/cli-boundary-tests.json).
The new post-DELETE failure test proves the provider deletion remains effective,
the failed ledger write retains the old row, the injection is consumed, and a
fresh-evidence retry reconciles the row. It also verifies representation after
the failed action. A negative test refuses to swallow an unexpected LookupError.

[Bounded seed replays](raw/cli-boundary-replay.json) retain source hashes:
7001556 and 7003454 now complete all original assertions and PASS; 7001212 and
7004446 still FAIL `(L)`, 7000828 still FAILS `(a)` on this branch's unchanged
production source, and invalid-budget seed 7000104 retains its rejection check.
The original full-5200 ERROR receipt is unchanged. These results are not a full
remeasurement or a production fix. Next: independently review this exact adapter,
then run all original seeds on the composed strict-admission correction and this
model, keeping remaining failures visible.
