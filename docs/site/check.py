#!/usr/bin/env python3
"""Validate the public static tree without reading any runtime state."""
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit, unquote, parse_qs
import hashlib
import sys
import json
import tempfile
import shutil

ROOT = Path(__file__).resolve().parents[2] / 'site'
ALLOWED = {'index.html', 'style.css', 'tokens.css', 'app.js', 'mark.svg', 'robots.txt', 'sitemap.xml', 'llms.txt', '_headers', '_redirects', 'field-notes/index.html', 'assets/observatory-cover.png', 'assets/credential-copies-cartoon.png', 'assets/dashboard-overview-en.png', '404.html'}
COVER_HASH = '70403cdb6ffc4029edcf2febbf63dec88a3118fa6cea9d7d6b17150d853d4833'
IMAGE_HASHES = {"assets/observatory-cover.png": COVER_HASH, "assets/credential-copies-cartoon.png": "8b69fe6ffcf44a4d5f8a32622d5c4d9d847d539c8c673c49fadf132c518be30c", "assets/dashboard-overview-en.png": "7ebedff4e0f9c0c1377edc722c9695cc01019dbab5649050b5176ed9cf93082c"}
# observatory.sshlg.me is retired as a product page. Its home page answers a permanent
# redirect to the product page on passioncode.ai; the field-notes article, its assets and
# the 404 page stay served here, so no rule may match them. Cloudflare Pages applies a
# `_redirects` rule even where a static file matches the path, which is why index.html
# can stay in the tree as the source the interaction checks read.
PRODUCT_PAGE = 'https://passioncode.ai/observatory/'
REDIRECTS = [('/', PRODUCT_PAGE, '301'), ('/index.html', PRODUCT_PAGE, '301'), ('/index', PRODUCT_PAGE, '301')]
SERVED = ['/field-notes/', '/field-notes/index.html', '/assets/', '/404.html', '/style.css', '/tokens.css', '/mark.svg', '/robots.txt', '/sitemap.xml', '/llms.txt']
SITEMAP = ['https://observatory.sshlg.me/field-notes/']

def redirect_failures(root):
    path=root/'_redirects'
    if not path.is_file(): return ['redirect rules missing']
    rules=[tuple(line.split()) for line in path.read_text().splitlines() if line.strip() and not line.lstrip().startswith('#')]
    failures=[]
    if rules!=REDIRECTS: failures.append('redirect rules differ from the retired-site contract')
    for rule in rules:
        if any(shadows(rule[0], kept) for kept in SERVED): failures.append('redirect rule shadows a served page: '+rule[0])
    return failures

def shadows(source, kept):
    # A splat matches every path under its prefix; a plain rule matches its own path only.
    if source.endswith('*'): return kept.startswith(source[:-1])
    return source==kept or (kept.endswith('/') and source.startswith(kept))

def sitemap_failures(root):
    import re
    locs=re.findall(r'<loc>([^<]+)</loc>',(root/'sitemap.xml').read_text()) if (root/'sitemap.xml').is_file() else []
    return [] if locs==SITEMAP else ['sitemap lists a redirected or unknown address']

class Page(HTMLParser):
    def __init__(self):
        super().__init__(); self.ids=[]; self.links=[]; self.assets=[]; self.headings=0
    def handle_starttag(self, tag, attrs):
        a=dict(attrs)
        if 'id' in a: self.ids.append(a['id'])
        if tag == 'h1': self.headings += 1
        if tag == 'a' and a.get('href'): self.links.append(a['href'])
        if tag in {'script','img','iframe','source'} and a.get('src'): self.assets.append(a['src'])
        if tag == 'link' and a.get('rel') != 'canonical': self.assets.append(a.get('href',''))

def check(root):
    root=root.resolve(); failures=[]
    files={p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()}
    if files != ALLOWED: failures.append('static file allowlist differs')
    if any(p.is_symlink() for p in root.rglob('*')): return failures+['symlink in static artifact']
    pages={}
    for rel in sorted(files):
        p=root/rel
        if rel in IMAGE_HASHES:
            data=p.read_bytes()
            if hashlib.sha256(data).hexdigest()!=IMAGE_HASHES[rel] or not data.startswith(b'\x89PNG\r\n\x1a\n'): failures.append('unreviewed cover image')
            continue
        try: body=p.read_text()
        except UnicodeError:
            failures.append('unreviewed binary'); continue
        if any(s in body for s in ['/Users/', 'BEGIN PRIVATE KEY', 'BEGIN RSA PRIVATE KEY', 'store.db', '.keyserver-token']): failures.append('private marker in artifact')
        if p.suffix=='.html':
            page=Page();page.feed(body);pages[p]=page
            if page.headings!=1: failures.append('expected one primary heading: '+rel)
            if len(page.ids)!=len(set(page.ids)): failures.append('duplicate HTML ids: '+rel)
            if any(urlsplit(ref).scheme or ref.startswith('//') for ref in page.assets): failures.append('nonlocal asset request')
    for source,page in pages.items():
        for ref in page.links+page.assets:
            url=urlsplit(ref)
            if url.scheme or url.netloc:
                if url.scheme not in {'http','https','mailto'}: failures.append('unexpected link scheme')
                continue
            path=unquote(url.path)
            target=((root/path.lstrip('/')) if path.startswith('/') else (source.parent/path) if path else source).resolve()
            if target.is_dir():target=target/'index.html'
            if not target.is_relative_to(root) or not target.is_file(): failures.append('unresolved local reference');continue
            if ref in page.assets and target.suffix in {'.css', '.js'}:
                digest=hashlib.sha256(target.read_bytes()).hexdigest()[:12]
                if parse_qs(url.query) != {'v': [digest]}: failures.append('missing or stale asset version')
            if url.fragment and (target not in pages or unquote(url.fragment) not in pages[target].ids): failures.append('missing anchor')
    text=(root/'index.html').read_text() if (root/'index.html').is_file() else ''
    for required in ['SYNTHETIC EXAMPLE','known secret values','COMPLETE LOCAL ENGINE','Never paste them into agent chat','docs/ONBOARDING.md','docs/MIGRATION.md']:
        if required not in text: failures.append('missing public scope disclosure: '+required)
    aggregate=json.loads((Path(__file__).parent/'case-study.json').read_text())
    if aggregate['total']!=sum(row['count'] for row in aggregate['surfaces']):failures.append('historical aggregate arithmetic')
    if str(aggregate['total'])+' recorded value replacements' not in text:failures.append('historical aggregate/site mismatch')
    failures+=redirect_failures(root)+sitemap_failures(root)
    return failures

if __name__=='__main__':
    errors=check(ROOT)
    if '--self-test' in sys.argv:
        cases=('unexpected-file','private-marker','aggregate-mismatch','image-tamper','cartoon-tamper','article-anchor','article-link','stale-asset','unversioned-asset',
               'redirect-missing','redirect-target','redirect-status','redirect-root-dropped','redirect-shadows-article','sitemap-redirected')
        for case in cases:
            with tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary)/'site';shutil.copytree(ROOT,root)
                before={p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file()}
                if case=='unexpected-file':(root/'extra.txt').write_text('synthetic')
                elif case=='private-marker':(root/'app.js').write_text('// /Users/synthetic')
                elif case in {'image-tamper','cartoon-tamper'}:
                    name='observatory-cover.png' if case=='image-tamper' else 'credential-copies-cartoon.png'
                    with (root/'assets'/name).open('ab') as f:f.write(b'changed')
                elif case=='stale-asset':
                    with (root/'style.css').open('a') as f:f.write('\n/* changed */\n')
                elif case=='unversioned-asset':
                    target=root/'index.html'
                    import re
                    target.write_text(re.sub(r'(style\.css)\?v=[a-f0-9]+', r'\1', target.read_text()))
                elif case in {'article-anchor','article-link'}:
                    target=root/'field-notes/index.html'
                    old,new=(('href="#what-monitoring-made-visible"','href="#absent"') if case=='article-anchor' else ('href="../mark.svg"','href="../../private.html"'))
                    target.write_text(target.read_text().replace(old,new,1))
                elif case=='redirect-missing':(root/'_redirects').unlink()
                elif case in {'redirect-target','redirect-status','redirect-root-dropped','redirect-shadows-article'}:
                    target=root/'_redirects';body=target.read_text()
                    if case=='redirect-target':body=body.replace(PRODUCT_PAGE,'https://example.com/observatory/')
                    elif case=='redirect-status':body=body.replace(' 301',' 302')
                    elif case=='redirect-root-dropped':body='\n'.join(l for l in body.splitlines() if not l.startswith('/ '))+'\n'
                    else:body+='/field-notes/* '+PRODUCT_PAGE+' 301\n'
                    target.write_text(body)
                elif case=='sitemap-redirected':
                    target=root/'sitemap.xml';target.write_text(target.read_text().replace('</urlset>','<url><loc>https://observatory.sshlg.me/</loc></url></urlset>'))
                else:
                    target=root/'index.html';target.write_text(target.read_text().replace('202 recorded value replacements','203 recorded value replacements'))
                after={p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file()}
                # A probe whose edit matched nothing proves nothing; it must change the tree.
                if after==before:errors.append('negative probe changed nothing: '+case)
                elif not check(root):errors.append('negative probe accepted: '+case)
        print(f'Negative probes: {len(cases)}')
    print('FAIL: '+'; '.join(errors) if errors else f'PASS: {len(ALLOWED)} reviewed static files; pages, local links, scope, cover digest, retired-home redirect and sitemap')
    sys.exit(bool(errors))
