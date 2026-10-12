# Russian copy pass — one vocabulary, plain syntax

Date: 2026-10-12. Branch `agent/ru-copy-pass`. Not a release: no version bump, no CHANGELOG
entry. The operator asked for every Russian string in the product to be reviewed and rewritten,
because it read badly. Copy only: no message id, placeholder, command, path, environment or
slot name changed, and no behaviour changed.

## What shipped

| Surface | File | Strings changed |
|---|---|---|
| Dashboard (every page, the session-start line) | `observatory/engine/dashboard/locales/ru.json` | 386 of 1,337 |
| Mac app | `macos/Sources/ObservatoryCore/Resources/ru.lproj/Localizable.strings` | 35 of 138 |
| Desktop app | `desktop/ui/i18n/ru.json` | 17 of 72 |

The Mac app's `Localizable.stringsdict` plurals were already correct and are unchanged. Finding
texts the rules write stay English (docs/brand/locales/ru.md); the CLI's output is English, apart
from the lines it takes from the same catalog (the session-start line, the auto-update switch label).

### What was wrong, and the rule now applied

- **English left in a sentence.** `remote` → «удалённый репозиторий»; `не запушено` →
  «не отправлено»; `чекаут` → «рабочая копия» (the word the Checkouts column already used).
- **Developer jargon on a person's screen.** «резолвится» → «не находится в DNS»; «рендерится» →
  «отображается»; «скан» → «сканирование» or «поиск утечек»; «деплой» → «развёртывание»;
  «квитанция» → «отчёт о запуске».
- **Calques.** «Движение» → «Активность»; «Двери» → «Шлюзы»; «леджер» → «учёт шлюза» (keys) or
  «записи памяти» (memory), by sense; «Страж диска» → «Контроль диска»; «Вживую» → «В реальном
  времени»; «простаивающие ветки» → «давно не менявшиеся»; «Объявлен активным, измерен спящим» →
  «Заявлен активным, по замерам неактивен».
- **Two words for one thing.** «потолок»/«лимит» → «лимит»; «скил»/«скилл» → «скилл»;
  «кеш»/«кэш» → «кэш»; «исход»/«результат» → «результат»; «раскрытие»/«показать» → «показ»;
  «Рабочая папка»/«Папка данных» → «папка данных»; «разговор»/«диалог» → «диалог»;
  «Построить»/«Собрать дашборд» → «Собрать дашборд»; «Как в системе»/«Системный» → «Системный».
- **The product's name.** "the observatory" in a sentence was «обсерватория»; it is Observatory.
- **Address.** The locale pack sets a neutral address with no «вы»; «Нужно вам», «ваших проектов»,
  «Запускаю сервер…», «Работаю…» are now «Требует решения», «проектов», «Сервер запускается…»,
  «Выполняется…».
- **The organisation's glossary.** restore → «восстановить» (was «вернуть»).
- **Punctuation.** A rhetorical dash before the next step became a colon or a semicolon (79 spaced
  dashes in the dashboard catalog → 16, the rest copula or a command reference); «ёлочки»
  throughout.
- **Plurals.** Every changed count keeps all four Russian categories; tile strings keep the number
  first because the page splits it off (`<b>{n}</b><span>…</span>`).

### Ten before → after pairs

| English id | Before | After |
|---|---|---|
| Commits on no remote | Коммиты не на remote | Коммиты есть только локально |
| Domain does not resolve | Домен не резолвится | Домен не находится в DNS |
| Dashboard renders blank | Дашборд рендерится пустым | Дашборд открывается пустым |
| Ledger follows a renamed project | Леджер ссылается на переименованный проект | Записи памяти ссылаются на переименованный проект |
| Rotate {name}? The door mints a new key, … | Ротировать {name}? Дверь создаст новый ключ, … | Ротировать {name}? Шлюз выпустит новый ключ, … |
| Disable {name}? It stops spending until "enable". | Отключить {name}? Он перестанет тратить до «включить». | Отключить {name}? Расходы прекратятся до повторного включения. |
| Live — refreshed {at} | Вживую — обновлено в {at} | В реальном времени: обновлено {at} |
| Needs you | Нужно вам | Требует решения |
| Every project↔repository link carries the rule that made it. | Каждая связь проект↔репозиторий несёт правило, которым проведена. | У каждой связи проекта с репозиторием указано правило, по которому она установлена. |
| Starting the server… (desktop) | Запускаю сервер… | Сервер запускается… |

## Checks actually run (macOS, Python 3.14.7)

See the pull request for the exit codes of the final run; the narrow checks run during the work:

| Check | Result |
|---|---|
| `project-observatory full check --suite i18n` (and action_outcomes, agents_page, disk_guard, rollup_sessions, workspace_redesign, dashboard_render, dashboard_shell, machine, organizations, freshness, serverd) | PASS after the pinned strings were updated |
| `python -m unittest tests.test_desktop_app` | OK — 11 tests |
| `swift test --package-path macos --filter 'LocalizationTests\|ModelTests'` | 41 tests, 0 failures |
| `python tools/update_inventory.py --check` | passed, no differences |
| `python3 docs/brand/lint.py docs/brand` | exit 0; 0 errors, 818 B022 warnings, identical before and after (the linter's `Sources:` cover the dashboard's Python and the site, not the catalogs) |

Tests that pin exact Russian text, updated in the same change: `test_i18n`, `test_action_outcomes`,
`test_agents_page`, `test_serverd`, `test_disk_guard`, `test_rollup_sessions`,
`test_workspace_redesign`, `test_dashboard_render`.

## Missing terms (for the brand pack's owner)

`terminology.md` and `locales/ru.md` have no Russian for these; the pass chose a wording and
used it everywhere, and the choice is the owner's to confirm or replace:

- workspace → «папка данных» (the test bans `workspace` and names this term in a comment; the
  glossary has no row)
- door (a provider's key-minting gateway) → «шлюз»; its ledger → «учёт шлюза»
- the memory ledger → «записи памяти»
- remote → «удалённый репозиторий»; checkout → «рабочая копия»; worktree kept as `worktree`
  (git's own term; git's Russian uses «рабочий каталог», which reads as a plain folder here)
- silence (a finding) → «заглушить», «заглушение»
- spending ceiling → «лимит»; reveal → «показ»; disk guard → «Контроль диска»
- Health (the page) stays «Здоровье»: unchanged here, a candidate for the owner's review

## Open

- `site/` headings end in a full stop (B063, English, outside this pass); the installed super-ux
  linter's `--fail-on B063` exits 2 on them, before and after this branch.
- `docs/brand/voice.md` names `Humanization pass: humanizer + editorial review`; this pass ran the
  copywriting skill's own pass and editorial review, not a separate humanizer tool.
- Next task: a native reader of Russian reviews the catalog on the running dashboard
  (`project-observatory full open --serve`, switch to Русский) and confirms or replaces the
  missing terms above in `docs/brand/terminology.md`.
