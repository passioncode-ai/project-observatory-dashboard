# Home at its two viewports (CP-01)

CP-01 asks that Home answer "is everything fine, and what needs me first" in one screen at 1440x900, and read at
390x844. These are viewport screenshots of `index.html` over the demo estate (`tools/demo_estate.py`, a fictional
estate), WebKit, in both languages, taken on 2026-11-18 (OBS-608). Each is reviewed and pinned by its
digest in `tools/check_public_release.py` (`PUBLIC_IMAGES`): a new render is a new review.

| File | Language | Viewport |
|---|---|---|
| `home-en-1440.png` | English | 1440x900 |
| `home-ru-1440.png` | Russian | 1440x900 |
| `home-en-390.png` | English | 390x844 |
| `home-ru-390.png` | Russian | 390x844 |

`tests/test_docs_drift.py` (`ControlPlaneTruth`) fails when a file is missing or its size is not its viewport. The
values on the page are checked by `observatory/engine/tests/test_home_page.py` (`ControlPlaneHome`): no link and no
shown value twice.

## The command

Playwright is not a dependency of this repository. With Node, and Playwright with its WebKit installed in a folder of
your own (`npm i playwright && npx playwright install webkit`, then `NODE_PATH=<that folder>/node_modules`), from the
repository root:

```sh
for l in en ru; do
  python3 tools/demo_estate.py "$TMPDIR/demo-$l" --locale "$l"
  node -e '
    const {webkit} = require("playwright");
    const [dir, out, l] = process.argv.slice(1);
    (async () => {
      const browser = await webkit.launch();
      for (const [w, h] of [[1440, 900], [390, 844]]) {
        const page = await (await browser.newContext({viewport: {width: w, height: h}, locale: l})).newPage();
        await page.goto("file://" + dir + "/pages/index.html");
        await page.waitForTimeout(500);
        await page.screenshot({path: `${out}/home-${l}-${w}.png`});
      }
      await browser.close();
    })();' "$TMPDIR/demo-$l" docs/design/control-plane/shots "$l"
done
```

## What they showed that CP-01 did not hold

- The first render (commit `6d99bd12`) showed the Disk and memory tile of a machine never surveyed printing the
  survey's English reason with the command `project-observatory full machine`. Home now says "This machine has not
  been surveyed yet." in the page's language, and a reason of any other tile that names a command is replaced by
  "Not read yet; its page says why." (`home_page._reason_html`). The reason with its command stays in the machine
  survey's JSON and MCP answer (`machine_view.py`), where agents read it. Tests: `test_home_page.py` `StateBand`,
  `test_a_machine_never_surveyed_says_so_in_the_readers_language_with_no_command` and
  `test_a_reason_that_holds_a_command_is_not_shown_on_home`.
