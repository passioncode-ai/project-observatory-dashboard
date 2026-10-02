# Contributing

Start with the [onboarding](docs/ONBOARDING.md), [compatibility policy](docs/COMPATIBILITY.md)
and [security boundary](SECURITY.md). Code, tests and defaults must be usable
without the author's accounts, projects or paths. Agents working here also read
[AGENTS.md](AGENTS.md).

## License and contributor agreement

Project Observatory is open source under the [GNU AGPL-3.0](LICENSE) (`AGPL-3.0-only`), and
PassionCode.ai also offers it under a [commercial license](COMMERCIAL-LICENSE.md) for use that does
not meet the AGPL's terms (<contact@passioncode.ai>). SPDX:
`AGPL-3.0-only OR LicenseRef-PassionCode-Commercial`. Versions up to and including v0.9.1 were
released under PolyForm Noncommercial or Internal Use (v0.8.2–v0.9.1) and the MIT License (v0.8.1
and earlier); those releases keep their licence.

Contributions are accepted under the [Contributor License Agreement](CLA.md) ([CLA.md](CLA.md)).
It lets PassionCode.ai offer every contribution under the AGPL and under the commercial license; you keep the copyright in your contribution. Opening a pull
request is how you agree to it; there is no checkbox to tick.

## Checks

The complete checks need:

- **Python 3.11 or newer** with loadable SQLite extensions (`requires-python` in
  `pyproject.toml`; choosing the interpreter is in the onboarding guide);
- **Node.js 22**: CI pins `node-version: '22'` (`.github/workflows/check.yml`) for the
  dashboard's page checks; other majors are not tested;
- **Git 2.17 or newer**: `full cleanup` calls `git worktree remove`
  (`observatory/engine/tools/cleanup.py`), which Git added in 2.17. CI runs the hosted
  runners' current Git, so older releases are not exercised.

```sh
python -m pip install -c requirements-full.lock '.[full]'
python -m unittest discover -s tests -v
project-observatory full check
python tools/check_public_release.py --history
python docs/site/check.py --self-test
```

A maintainer who holds the private predecessor also passes
`--private-denylist FILE`: a local JSON array of private names, never committed. Names this
repository publishes on purpose, such as the author and the author's public projects, are listed
with a reason in `tools/public-identifiers.json` and subtracted from that list. An entry there is a
review decision. It is not a way to silence a finding, and an entry without a reason fails the check.

Use synthetic fixtures and isolated temporary workspaces. Never run the full
historical test directory indiscriminately against an operational home. The
portable runner lists its supported suites and labels live provider checks
separately. Tests must not depend on personal credentials or project inventory.

Do not alter a released database migration in place: add a migration, preserve
existing IDs/checksums, and test supported prefixes, rollback, WAL data and
future-version refusal. Preserve unknown optional configuration fields. Changes
to MCP input/output schemas and metric plugin requirements need explicit
compatibility tests. Keep the old portable command namespace working.

A change to the complete engine updates its source inventory in the same review:
run `python tools/update_inventory.py` (CI runs it with `--check`).
The initial inventory also records the sanitized extraction from its private
predecessor; never copy that predecessor's Git history or operational documents.
Dependency constraint updates require the Python/OS CI matrix, not just a local
import check. Build a wheel and run `tools/check_package.py` against it.

Update scenarios, onboarding and documentation with behavior. Keep unsupported
features and unexecuted tests clearly labelled. Public copy must cite reviewed
facts and never turn occurrence counts into unique-secret or breach counts.
New public assets require an explicit path and content review before entering
the release allowlist.

## Interface strings

The dashboard's text is English in the code and translated from one catalog per
language (`observatory/engine/dashboard/locales/`), read by both the page builder and
the page script. To add or change a string:

- write it in English as the message id: `T("{n} projects", {n})` in the page script,
  `t("…")`, `t.mark("…")` or `t.attr(name, "…")` in Python, or `data-t` on a static
  element of the template;
- add the Russian to `locales/ru.json`, with the same `{placeholders}`; a count takes
  a plural object (`one`, `few`, `many`, `other` in Russian, `one`, `other` in `en.json`);
- when one English text needs two Russian forms, give it a context: `domain@@not measured`;
- run `project-observatory full check --suite i18n` from any directory. It runs
  `observatory/engine/tests/test_i18n.py` in a sandbox with its own synthetic workspace, and
  fails on a missing or unused translation, mismatched placeholders, incomplete plural forms or
  Russian text left in the sources. (Running the file directly with `python -m unittest` also
  needs an initialized `OBSERVATORY_HOME`: its page-build cases emit a registry first.)

A finding's title is a message id with arguments (`finding_types.titled(msgid, **args)`
gives `title`, `title_id`, `title_args`), so its Russian lives in `locales/ru.json` like any
interface string. A finding's details and action, written by the rules in
`tools/*_findings.py`, are still data in English. The PassionCode design tokens in `dashboard/brand/` are vendored
bytes: change them at the canonical source, then copy and repin (`brand/README.md`).

## Comments and design notes

This repository is the engine's upstream, so its comments and docstrings are the design record:
explain *why* a function exists, which failure it guards against, and which document governs it.
They must not name private projects, people, hosts, paths or credential values, and they must not
cite decision records that live outside this repository. The first public export removed every
comment; on 2026-09-23 3,049 lines were restored from the original sources, each one filtered for
private identifiers and each file verified to have an unchanged syntax tree.

