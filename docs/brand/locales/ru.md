Contract: brand-contract v1
Locale: ru
Primary: no
Address form: нейтрально, без обращения
Length coefficient: 1.0
Humor: -1 from base
Never translated: Project Observatory, PassionCode.ai, Switchboard, Fabric, Skills, Harness
Keywords: unresearched; no search-volume claims
Reviewed by: unreviewed

# Locale delta

Use natural technical language. Keep proper names and code identifiers intact.
The dashboard follows the organisation's localization standard (`knowledge/localization.md`
in the Fabric workspace, L10N-01..06): it opens in Russian when the reader's first system
language is Russian and in English otherwise, and the rail's switch (Системный / English /
Русский) overrides it per reader; `interface.locale` is the language of a build no browser
reads. The public documentation and website are English. Interface strings are English message ids; the Russian catalog is
`observatory/engine/dashboard/locales/ru.json`. Finding texts written by the rules stay
English. The product name, PassionCode.ai, Switchboard and Fabric are never translated.

Russian interface voice: short noun phrases for labels («Находки», «Ключи»), neutral
imperative for actions («скопировать команду»), no address to the reader, correct
plural forms for every count, «—» for an unknown value and never a zero in its place.

## Glossary

The organisation's glossary (L10N-06) decides these terms in every PassionCode product; a
term this product needs first joins that glossary. `tests/test_i18n.py` `Glossary` fails on
a variant it replaced.

| English | Русский | Not |
|---|---|---|
| workflow | задача | процесс (an OS process stays «процесс»); a background job is «задание» |
| handoff | передача | |
| backup / restore | резервная копия / восстановить | бэкап |
| vault | хранилище | vault, сейф |
| Keychain | Связка ключей | |
| account | аккаунт | учётная запись |
| Terminal (the macOS app) | Терминал | a terminal in general stays «терминал» |
| Restart to update | Перезапустить для обновления | |
| Install updates automatically | Устанавливать обновления автоматически | |
| agent | агент | |
| Project Observatory, PassionCode.ai, Fabric, Switchboard | — | never translated |

Dates in Russian read `02.01.2026 03:04 UTC`; numbers group with a no-break space and take a
decimal comma (`1 234,5`).
