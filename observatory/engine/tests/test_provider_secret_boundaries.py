#!/usr/bin/env python3
"""Provider credential boundaries, exercised with synthetic values and mocked HTTP only."""
from __future__ import annotations
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tools')]
with tempfile.TemporaryDirectory(prefix='provider-import-') as temporary:
    with patch.dict(os.environ, {'OBSERVATORY_HOME': str(Path(temporary).resolve())}):
        import private_io
        import install_key
        import revoke_key
        import openrouter
        import cloudflare


class ProviderBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='provider-boundary-')
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / 'home'
        self.store = self.home / 'secrets'
        self.env = {k:v for k,v in os.environ.items() if not k.startswith('OBSERVATORY_')}
        self.env.update(OBSERVATORY_HOME=str(self.home), OBSERVATORY_VAULT_DIR=str(self.store/'projects'))
        self.patches = [
            patch.multiple(openrouter, ADMIN_STORE=self.store/'openrouter-admin',
                           LEGACY=self.store/'openrouter-provisioning', LEDGER=self.store/'issued.json'),
            patch.multiple(install_key, PROVISIONING=self.store/'openrouter-provisioning',
                           DESTINATIONS={'observatory':(self.store/'working-key','local consumer')}),
            patch.object(revoke_key,'SECRET',self.store/'openrouter-provisioning'),
            patch.object(cloudflare,'ADMIN_STORE',self.store/'cloudflare-admin'),
            patch('urllib.request.urlopen',side_effect=AssertionError('Live HTTP is forbidden')),
            patch.object(openrouter,'_journal'), patch.object(cloudflare,'_journal'),
        ]
        for item in self.patches:item.start()

    def tearDown(self):
        for item in reversed(self.patches):item.stop()
        self.temp.cleanup()

    def writers(self):
        return [lambda p,v:install_key.write(p,v,'observatory'),
                openrouter.write_secret,cloudflare.write_secret,private_io.write]

    def test_writers_replace_complete_files_with_owner_only_permissions(self):
        for index,write in enumerate(self.writers()):
            target=self.store/('key'+str(index))
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_text('old');target.chmod(0o644)
            write(target,'new-synthetic')
            self.assertEqual(target.read_text().strip(),'new-synthetic')
            self.assertEqual(target.stat().st_mode&0o777,0o600)
            self.assertEqual(target.parent.stat().st_mode&0o777,0o700)
        self.assertEqual(list(self.store.glob('.credential-write-*')),[])

    def test_symlink_files_parents_and_metadata_refuse_without_touching_target(self):
        victim=self.root/'victim';victim.write_text('keep')
        self.store.mkdir(parents=True)
        link=self.store/'key';link.symlink_to(victim)
        for write in self.writers():
            with self.assertRaises(RuntimeError):write(link,'new-synthetic')
        link.unlink();meta=link.with_name('key.meta.json');meta.symlink_to(victim)
        for write in (openrouter.write_secret,cloudflare.write_secret):
            with self.assertRaises(RuntimeError):write(link,'new-synthetic')
        self.assertFalse(link.exists())
        parent=self.store/'linked';parent.symlink_to(self.root,target_is_directory=True)
        for write in self.writers():
            with self.assertRaises(RuntimeError):write(parent/'victim','new-synthetic')
        self.assertEqual(victim.read_text(),'keep')

    def test_admin_reads_refuse_symlinks_and_public_permissions(self):
        for module in (openrouter,cloudflare):
            directory=module.ADMIN_STORE;directory.mkdir(parents=True)
            key=directory/'example';key.write_text('synthetic-admin');key.chmod(0o644)
            with self.assertRaises(RuntimeError):module.read_admin('example')
            key.unlink();key.symlink_to(self.root/'absent')
            with self.assertRaises(RuntimeError):module.read_admin('example')

    def test_legacy_alias_only_accepts_canonical_sibling_slot(self):
        target=self.store/'openrouter-admin/example'
        private_io.write(target,'old-synthetic')
        alias=install_key.PROVISIONING;alias.symlink_to(Path('openrouter-admin/example'))
        install_key.write(alias,'replacement-synthetic','provisioning')
        self.assertTrue(alias.is_symlink())
        self.assertEqual(private_io.read(private_io.legacy_path(alias)),'replacement-synthetic')
        alias.unlink();alias.symlink_to(self.root/'victim')
        with self.assertRaises(RuntimeError):install_key.write(alias,'blocked','provisioning')

    def test_install_and_revoke_stash_never_echo_value_or_tail(self):
        key='sk-or-v1-'+'x'*35+'Q9Z7'
        output=io.StringIO()
        with patch.object(install_key,'kind_of',return_value=('provisioning',{})), patch('sys.stdin',io.StringIO(key)), contextlib.redirect_stdout(output):
            self.assertEqual(install_key.main(['install_key.py']),0)
        with patch('sys.stdin',io.StringIO(key)),contextlib.redirect_stdout(output):
            self.assertEqual(revoke_key.load_or_store(),key)
        self.assertNotIn(key,output.getvalue());self.assertNotIn('Q9Z7',output.getvalue())

    def test_provider_errors_do_not_echo_response_secrets(self):
        value='synthetic-sensitive-value'
        for module in (openrouter,cloudflare):
            body=json.dumps({'error':{'message':value},'errors':[{'message':value}]}).encode()
            error=urllib.error.HTTPError('https://example.invalid',401,'Unauthorized',{},io.BytesIO(body))
            with patch('urllib.request.urlopen',side_effect=error):
                with self.assertRaises(RuntimeError) as got:module._request('/example','synthetic-admin')
            self.assertNotIn(value,str(got.exception));self.assertIn('401',str(got.exception))

    def record(self):
        return {'issued':{'example':{'account':'example','destination':'vault:example/local/EXAMPLE_KEY',
                                   'hash':'old-id','limit_usd':10,'limit_reset':'monthly','rotations':0}}}

    def mocked_rotation(self,cleanup_fails=False):
        calls=[]
        def request(path,key,payload=None,method=None):
            calls.append((path,method))
            if path=='/keys':return {'key':'synthetic-successor','data':{'hash':'new-id'}}
            if cleanup_fails and method=='DELETE':raise RuntimeError('synthetic-successor')
            return {}
        return calls,request

    def test_failed_delivery_deletes_successor_and_keeps_predecessor(self):
        calls,request=self.mocked_rotation()
        with patch.object(openrouter,'read_admin',return_value=('example','synthetic-admin')), patch.object(openrouter,'find_key',return_value={'hash':'old-id'}), patch.object(openrouter,'_request',side_effect=request), patch.object(openrouter,'deliver',side_effect=RuntimeError('synthetic-successor')):
            with self.assertRaises(RuntimeError) as got:openrouter.rotate_one('example',self.record())
        self.assertIn(('/keys/new-id','DELETE'),calls)
        self.assertNotIn(('/keys/old-id','DELETE'),calls)
        self.assertIn('successor deleted',str(got.exception))
        self.assertNotIn('synthetic-successor',str(got.exception))

    def test_cleanup_failure_reports_partial_without_claiming_success(self):
        calls,request=self.mocked_rotation(cleanup_fails=True)
        with patch.object(openrouter,'read_admin',return_value=('example','synthetic-admin')), patch.object(openrouter,'find_key',return_value={'hash':'old-id'}), patch.object(openrouter,'_request',side_effect=request), patch.object(openrouter,'deliver',side_effect=OSError('synthetic-successor')):
            with self.assertRaises(RuntimeError) as got:openrouter.rotate_one('example',self.record())
        self.assertIn('Partial failure',str(got.exception));self.assertIn('cleanup failed',str(got.exception))
        self.assertNotIn(('/keys/old-id','DELETE'),calls)
        self.assertNotIn('synthetic-successor',str(got.exception))

    def test_successful_rotation_delivers_before_revocation_and_records_completion(self):
        calls,request=self.mocked_rotation()
        def deliver(value,to,*,rotate=False):
            self.assertTrue(rotate);calls.append(('delivered',None));return 'vault example/local/EXAMPLE_KEY'
        doc=self.record()
        with patch.object(openrouter,'read_admin',return_value=('example','synthetic-admin')), patch.object(openrouter,'find_key',return_value={'hash':'old-id'}), patch.object(openrouter,'_request',side_effect=request), patch.object(openrouter,'deliver',side_effect=deliver):
            result=openrouter.rotate_one('example',doc)
        self.assertLess(calls.index(('delivered',None)),calls.index(('/keys/old-id','DELETE')))
        self.assertEqual(openrouter.ledger()['issued']['example']['rotation_status'],'complete')
        self.assertNotIn('synthetic-successor',json.dumps(result))

    def test_finalization_failure_keeps_recovery_ledger_without_secret_values(self):
        calls,request=self.mocked_rotation()
        def fail_old_delete(path,key,payload=None,method=None):
            if path=='/keys/old-id' and method=='DELETE':
                raise RuntimeError('synthetic-successor')
            return request(path,key,payload,method)
        with patch.object(openrouter,'read_admin',return_value=('example','synthetic-admin')), patch.object(openrouter,'find_key',return_value={'hash':'old-id'}), patch.object(openrouter,'_request',side_effect=fail_old_delete), patch.object(openrouter,'deliver',return_value='vault example/local/EXAMPLE_KEY'):
            with self.assertRaises(RuntimeError) as got:openrouter.rotate_one('example',self.record())
        row=openrouter.ledger()['issued']['example']
        self.assertEqual(row['hash'],'new-id')
        self.assertEqual(row['predecessor_hash'],'old-id')
        self.assertEqual(row['rotation_status'],'successor-delivered-predecessor-pending')
        self.assertIn('Partial failure',str(got.exception))
        self.assertNotIn('synthetic-successor',str(got.exception)+json.dumps(row))

    def test_vault_destination_uses_rotation_for_existing_slot(self):
        first=subprocess.run([sys.executable,str(ROOT/'tools/vault.py'),'put','example','local','EXAMPLE_KEY'],input='old-synthetic',text=True,capture_output=True,env=self.env)
        self.assertEqual(first.returncode,0,first.stderr)
        with patch.dict(os.environ,self.env,clear=True):
            openrouter.deliver('successor-synthetic','vault:example/local/EXAMPLE_KEY',rotate=True)
        slot=self.store/'projects/example/local/EXAMPLE_KEY'
        self.assertEqual(slot.read_text(),'successor-synthetic')
        self.assertEqual([p.read_text() for p in slot.parent.glob('*.retired-*')],['old-synthetic'])

    def test_invalid_ledger_and_nonfinite_budget_refuse_before_provider(self):
        private_io.write(openrouter.LEDGER,'not-json')
        with patch.object(openrouter,'_request') as request:
            with self.assertRaises(RuntimeError):openrouter.issue_key('example',10,None,'observatory',None)
            for limit in (float('nan'),float('inf'),0,-1):
                with self.assertRaises(ValueError):openrouter.issue_key('example',limit,None,'observatory',None)
            request.assert_not_called()


if __name__=='__main__':unittest.main()
