"""Portable credential/config boundaries, using an empty synthetic home only."""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

class PrivateSources(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.home = self.base / 'runtime'
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(('OBSERVATORY_', 'CLAUDE_MEM_', 'OPENAI_', 'OPENROUTER_'))}
        self.env.update(HOME=str(self.base / 'user'), OBSERVATORY_HOME=str(self.home),
                        PYTHONDONTWRITEBYTECODE='1')

    def run_python(self, source):
        result = subprocess.run([sys.executable, '-c', source], cwd=ROOT,
                                env=self.env, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_default_credentials_and_curation_stay_private(self):
        self.run_python('''
import importlib.util, pathlib, sys
import paths
root=paths.ROOT
checks={
 'agent/providers.py':['CONFIG','KEY_FILES','EMBED_KEY_FILES'],
 'collectors/credentials_registry.py':['OWNERS','ANNOTATIONS','MACHINE_STORE'],
 'collectors/heroku_registry.py':['LINKS'],
 'collectors/google_registry.py':['CONSOLES_PATH'],
 'collectors/estate_surfaces.py':['CURATED_PRODUCTS','BOUNDARY'],
 'collectors/scan_bitbucket.py':['SECRET'],
 'collectors/scan_google.py':['SECRET_STORE'],
 'collectors/scan_openrouter.py':['DESTINATIONS'],
 'plugins/ga4_analytics.py':['KEY_FILE','MAP_FILE'],
 'plugins/search_console.py':['KEY_FILE'],
 'plugins/cloudflare_analytics.py':['TOKEN_DIR'],
 'tools/vault.py':['STORE','LEAKS','MOVES'],
 'tools/openrouter.py':['ADMIN_STORE','LEGACY','LEDGER'],
 'tools/cloudflare.py':['ADMIN_STORE'],
 'tools/digitalocean.py':['ADMIN_STORE','READ_STORE','RECORDS'],
 'tools/revoke_key.py':['SECRET'],
 'tools/install_key.py':['DESTINATIONS','PROVISIONING'],
 'tools/scan_leaks.py':['VAULT','SESSIONS','DESTINATIONS'],
 'tools/scrub_companion.py':['HOME','STORES','BACKUP_DIR'],
}
def walk(v):
 if isinstance(v,pathlib.Path): yield v
 elif isinstance(v,dict):
  for x in v.values(): yield from walk(x)
 elif isinstance(v,(list,tuple)):
  for x in v: yield from walk(x)
for filename, fields in checks.items():
 name='isolated_'+filename.replace('/','_').replace('.','_')
 spec=importlib.util.spec_from_file_location(name,root/filename)
 mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
 for field in fields:
  for p in walk(getattr(mod,field)):
   assert p.is_relative_to(paths.HOME), (filename,field)
assert not paths.HOME.exists(), 'imports created runtime state'
''')

    def test_optional_collectors_do_nothing_without_opt_in(self):
        for name in ('github','bitbucket','cloudflare','google','heroku','mcp',
                     'openrouter','remote_env','domains','sessions','vault'):
            with self.subTest(name=name):
                proc=subprocess.run([sys.executable, f'collectors/scan_{name}.py'],
                                    cwd=ROOT, env=self.env, text=True,
                                    capture_output=True, timeout=15)
                self.assertEqual(proc.returncode,0,proc.stderr)
                self.assertIn('integration disabled',proc.stdout)
        self.assertFalse(self.home.exists())

    def test_explicit_legacy_secret_reference_is_preserved(self):
        target=self.base/'legacy-secrets'
        (self.home/'config').mkdir(parents=True)
        # The configuration schema is owned centrally; use its current envelope.
        self.run_python('''
import json, configuration
p=configuration.home()/'config/settings.json'
d=configuration.load();d.setdefault('sources',{})['secret_store']='''+repr(str(target))+'''
p.write_text(json.dumps(d))
''')
        # Use the actual canonical config path exposed by configuration.
        self.run_python('''
from pathlib import Path
import configuration, paths
from tools import openrouter
assert openrouter.ADMIN_STORE == Path('''+repr(str(target))+''')/'openrouter-admin'
''')

    def test_remediation_requires_explicit_opt_in(self):
        proc=subprocess.run([sys.executable,'tools/scrub_companion.py'],cwd=ROOT,
                            env=self.env,text=True,capture_output=True,timeout=15)
        self.assertEqual(proc.returncode,0,proc.stderr)
        self.assertIn('remediation is disabled',proc.stdout)
        self.assertFalse(self.home.exists())

    def test_both_remediation_stores_get_backups(self):
        self.run_python('''
import sqlite3, pathlib
from tools import scrub_companion as s
s.BACKUP_DIR=pathlib.Path('''+repr(str(self.base/'backups'))+''')
for filename in ('claude-mem.db','chroma.sqlite3'):
 p=pathlib.Path('''+repr(str(self.base))+''')/filename
 c=sqlite3.connect(p);c.execute('CREATE TABLE facts(value TEXT)');c.execute("INSERT INTO facts VALUES ('synthetic')");c.commit();c.close()
 backup=s.backup(p)
 assert backup is not None and backup.stat().st_mode & 0o777 == 0o600
 c=sqlite3.connect(backup);assert c.execute('SELECT value FROM facts').fetchone()[0]=='synthetic';c.close()
''')

if __name__ == '__main__':
    unittest.main()
