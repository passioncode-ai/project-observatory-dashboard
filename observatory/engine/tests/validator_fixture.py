"""A complete synthetic workspace for the validator's rules; no copied inventory.

`tools/validate_registry.py` reads more than the registry: the canonical wiki
page a project names, the registrar export, the Cloudflare zone snapshot and two
curated files in the workspace's `config/`. A fixture narrower than that tests
a validator that cannot exist, so every input is planted here and each is
selected the way a real workspace selects it — the export and the wiki through
their environment overrides, the snapshot through `config/settings.json`, the
curation as files in the workspace's own `config/`.
"""
from datetime import date
import csv
import json
import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECT = 'project:fixture'
#: Registered at the registrar the export describes, so it is compared to it.
EXPORTED = 'alpha-web.example'
#: Registered elsewhere, so it can be excluded without disturbing the export.
ELSEWHERE = 'beta-api.example'


def export_path(root: Path) -> Path:
    return root / 'evidence' / 'Domain_List.csv'


def snapshot_path(root: Path) -> Path:
    return root / 'evidence' / 'cloudflare-zones.json'


def seed(root: Path) -> Path:
    """Plant the workspace under `root` and return its registry directory."""
    reg = root / 'registry'; reg.mkdir()
    wiki = root / 'wiki'; wiki.mkdir()
    config = root / 'config'; config.mkdir()
    (root / 'evidence').mkdir()
    # The tier rules are configuration the validator re-derives tiers from.
    shutil.copyfile(ROOT / 'defaults' / 'activity_tiers.json', config / 'activity_tiers.json')
    (config / 'settings.json').write_text(json.dumps({
        'schema_version': 1, 'sources': {'cloudflare_snapshot': str(snapshot_path(root))},
        'integrations': {}, 'features': {}}))

    def write(name, body):
        p = reg / name; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(json.dumps(body))
    refs = ['SRC-FIXTURE']
    write('sources.json', {'sources': [{'id': refs[0]}]})
    domain = lambda n, r: dict(id='domain:' + n, name=n, ownership='owned', registrar=r,
                               dns={'provider': 'cloudflare', 'zone_present': True}, source_refs=refs)
    nc = domain(EXPORTED, 'namecheap')
    nc['namecheap'] = dict(status='active', privacy=True, auto_renew=True, expires_on='2035-01-02')
    write('domains.json', {'domains': [nc, domain(ELSEWHERE, 'cloudflare')]})
    write('domain-exclusions.json', {'domains': []})
    snapshot_path(root).write_text(json.dumps({'zones': [
        {'domain_id': 'domain:' + n, 'status': ('active' if n == EXPORTED else 'invalid_nameservers'),
         'plan': 'free'} for n in (EXPORTED, ELSEWHERE)]}))
    with export_path(root).open('w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['Domain Name', 'Domain status at NC', 'Domain privacy protection status',
                         'Domain auto-renew status', 'Domain expiration date'])
        writer.writerow([EXPORTED, 'active', 'ON', 'ON', 'Jan 02 2035'])
    (wiki / 'fixture.md').write_text('Synthetic canonical page.\n')
    write('projects.json', {'projects': [dict(id=PROJECT, name='Fixture', source_refs=refs,
          canonical_page='vault:fixture.md', activity_tier='active',
          last_activity_on=date.today().isoformat())]})
    repos = [dict(id='repository:fixture/' + n, name_with_owner='fixture/' + n, source_refs=refs)
             for n in ['a', 'b']]
    write('repositories.json', {'repositories': repos})
    write('relations.json', {'relation_types': {'implemented_by': {}}, 'relations': [
        {'id': 'relation:fixture', 'from': PROJECT, 'to': repos[0]['id'], 'type': 'implemented_by',
         'rule': 'synthetic', 'source_refs': refs}]})
    write('domain-liveness.json', {'source_refs': refs})
    write('stale-remotes.json', {'source_refs': refs, 'clones': []})
    write('heroku-apps.json', {'apps': [dict(id='heroku:fixture', name='fixture', state='running',
                                             project=PROJECT, link_rule='verified')],
          'source_refs': refs, 'totals': {'apps': 1, 'linked_to_a_project': 1}})
    write('credentials.json', {'credentials': [dict(id='credential:fixture', kind='machine-secret',
                                                    used_by=[PROJECT])],
          'source_refs': refs, 'totals': {'credentials': 1, 'leaked_unrotated': 0}})
    write('products.json', {'products': [dict(id='product:fixture', kind='curated',
                                              why='Synthetic product claim',
                                              members=[{'project': PROJECT, 'role': 'site'}])],
          'roles': ['site'], 'totals': {'products': 1}})
    write('cloudflare-zones.json', {'zones': [
        dict(id='zone:fixture', standing='linked', registered_domain='domain:' + EXPORTED,
             project=PROJECT, status='active'),
        dict(id='zone:product', standing='product', product='product:fixture', status='active')],
        'totals': {'zones': 2}})
    write('mcp-servers.json', {'servers': [dict(id='mcp:fixture', liveness='not-probed',
                                                target='https://mcp.example.com/mcp')],
          'totals': {'declarations': 1}})
    (config / 'heroku_links.json').write_text(json.dumps({'links': []}))
    (config / 'credential_owners.json').write_text(json.dumps({'owners': []}))
    return reg


def environment(reg: Path) -> dict:
    """The validator's environment: this workspace and nothing inherited."""
    root = reg.parent
    env = {k: v for k, v in os.environ.items() if not k.startswith('OBSERVATORY_')}
    return {**env, 'OBSERVATORY_HOME': str(root), 'OBSERVATORY_REGISTRY': str(reg),
            'OBSERVATORY_VAULT': str(root / 'wiki'),
            'OBSERVATORY_DOMAIN_EXPORT': str(export_path(root))}
