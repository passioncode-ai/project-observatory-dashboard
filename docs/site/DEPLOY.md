# Public website deployment

The deployment input is the fifteen reviewed files in `site/`, checked by [`check.py`](check.py) (its `ALLOWED` set is the list). This directory is separate from the application's private generated dashboard. Never deploy an Observatory home, repository root, database or generated dashboard.

## Validate and preview

```sh
python3 docs/site/check.py --self-test
python3 -m http.server 8766 --bind 127.0.0.1 --directory site
```

The self-test proves that an unexpected file, a private-path marker, mismatched case-study arithmetic, changed cover bytes, broken article anchors and escaped local links are rejected. It supplements the complete source/history privacy gate in [`../../tools/check_public_release.py`](../../tools/check_public_release.py).

## Cloudflare Pages

The public project is `project-observatory`, with production branch `main` and custom domain <https://observatory.sshlg.me/>. The fallback host is <https://project-observatory.pages.dev/>.

**This host is retired as a product page.** The product page is <https://passioncode.ai/observatory/>. `site/_redirects` answers `/`, `/index.html` and `/index` with a permanent `301` to it; Cloudflare Pages applies a `_redirects` rule even where a static file matches, so `index.html` stays in the tree only as the source `tools/check_site_interactions.cjs` reads, and its canonical URL names the product page. The field notes (`/field-notes/`), their assets and `404.html` stay served here, and `sitemap.xml` lists the field notes alone. `check.py` fails when a rule changes its target or status, when the home rule is dropped, when any rule would match a served page, or when the sitemap lists a redirected address.

Use the maintainer's already-authorized Cloudflare login or a scoped credential held outside the repository. A fork must select its own account, project and domain and update the canonical URL, sitemap and robots file.

```sh
python3 docs/site/check.py --self-test
npx --yes wrangler@4.135.0 pages deploy site --project-name project-observatory --branch main
```

Select the account through Wrangler's supported login/account configuration. No token belongs in this command, a source file, chat, or Git history. The custom domain must be attached in Pages and its CNAME point to the project's Pages host. Domain setup is separate from deployment and may take time to validate TLS.

After deployment, confirm the redirect and the served article from outside:

```sh
curl -sI https://observatory.sshlg.me/ | grep -iE '^(HTTP|location)'             # 301, location https://passioncode.ai/observatory/
curl -sI https://observatory.sshlg.me/index.html | grep -iE '^(HTTP|location)'   # 301, same location
curl -s -o /dev/null -w '%{http_code}\n' https://observatory.sshlg.me/field-notes/   # 200
curl -s -o /dev/null -w '%{http_code}\n' https://observatory.sshlg.me/no-such-page  # 404
```

Compare the served article with `site/field-notes/index.html`, inspect response headers and confirm the production/custom host in a browser. Record the source revision and deployment identifier in the release handoff. The site makes no request to a running local Observatory and includes no analytics or external assets.

## Browser review on 2026-09-21

The same content was rendered in dawn and paper-led variants at 1440×900. Dawn was selected for separation of the introduction from the paper evidence sections. At 390×844, the installation command wrapped without document overflow (document and viewport both 390px). The synthetic toggle displayed the redacted finding, and the copy control produced its success state. Missing-JavaScript content remains present in the original HTML, including both example states. No claim of a complete WCAG conformance audit is made.

Wrangler may create a local `.wrangler/` cache. It is ignored by Git and is not a release input. Run source publication checks from a clean checkout; the release gate deliberately rejects runtime cache files that remain in the prospective public tree.

## Full-engine site review

Both landing and article checked at 320px and 1440px: document width equals viewport. Cover loads at its native 1672×941 ratio. Synthetic toggle, prompt copy success and keyboard skip link worked; the browser reported no console warnings/errors. These checks cover the reviewed flows, not a complete accessibility certification. The generated illustration contains no runtime screenshot.

## Illustrated article and 404 contract

Run `python3 tools/build_article.py --check` before publishing. The Markdown source, figures and article HTML must agree. The fifteen-file deployment includes both reviewed illustrations and `404.html`. An unknown path must return HTTP 404 with the recovery page, never HTTP 200 with the home page. Validate actual status after deployment; a local Python static server does not reproduce Pages fallback routing.
