"""OBS-80: a partial refresh cannot erase a different application's evidence."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(ENGINE / 'collectors'))
import scan_heroku as scan


def row(name, cost=7):
    return {'name': name, 'id': name + '-id', 'monthly_cost': cost, 'scaled': 1,
            'unpriced': [], 'deploy_beyond_window': False, 'local_folders': [],
            'formation_error': None, 'dynos_error': None, 'addons_error': None}


class ScopedSurvey(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.out = self.base / 'heroku.json'
        self.old = {'schema_version': 1, 'scanned_at': '2026-01-01T00:00:00Z',
                    'account': 'owner@example.invalid', 'teams': ['example-org'],
                    'apps': [row('alpha-web'), {**row('beta-api', 19), 'extra': {'keep': [1, 2]}}],
                    'degraded': [{'source': 'heroku:beta-api', 'reason': 'old unknown'}],
                    'remotes_to_unknown_apps': ['gamma-worker'], 'extra': {'keep': True}}
        self.out.write_text(json.dumps(self.old))
        self.env = patch.dict(os.environ, {'HOME': str(self.base), 'OBSERVATORY_HOME': str(self.base/'workspace')})
        self.env.start(); self.addCleanup(self.env.stop)
        # These fail before an accidental live network or credential lookup can occur.
        self.net = patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden'))
        self.net.start(); self.addCleanup(self.net.stop)
        import configuration
        self.enabled = patch.object(configuration, 'enabled', return_value=True)
        self.enabled.start(); self.addCleanup(self.enabled.stop)

    def run_scan(self, names=('alpha-web',), token=('synthetic', None), apps=None, result=None):
        if apps is None: apps = [{'name': n, 'id': n+'-id'} for n in ('alpha-web','beta-api')]
        if result is None: result = lambda a, _: row(a['name'], 9)
        import configuration
        with patch.object(configuration, 'enabled', return_value=True), patch.object(scan,'token',return_value=token), patch.object(scan,'get',return_value={}), patch.object(scan,'paged',return_value=apps), patch.object(scan,'scan_app',side_effect=result), patch.object(scan,'local_checkouts',return_value=({}, [])):
            return scan.main(['scan_heroku', str(self.out), '--only', *names])

    def read(self): return json.loads(self.out.read_text())

    def test_subset_preserves_unrequested_rows_and_global_metadata(self):
        self.assertEqual(self.run_scan(), 0)
        doc=self.read()
        self.assertEqual(doc['apps'][1], self.old['apps'][1])
        self.assertEqual(doc['scanned_at'], self.old['scanned_at'])
        for key in ('account','teams','remotes_to_unknown_apps','extra'):
            self.assertEqual(doc[key], self.old[key])
        self.assertEqual(doc['apps'][0]['monthly_cost'],9)
        self.assertIn(self.old['degraded'][0],doc['degraded'])
        self.assertEqual(doc['scoped_refresh']['refreshed'],['alpha-web'])

    def test_auth_failure_retains_rows_and_records_unknown(self):
        self.assertNotEqual(self.run_scan(token=(None,'unavailable')),0)
        self.assertEqual(self.read()['apps'],self.old['apps'])
        self.assertTrue(self.read()['degraded'])

    def test_list_failure_retains_rows(self):
        self.assertNotEqual(self.run_scan(apps={'__error__':'403'}),0)
        self.assertEqual(self.read()['apps'],self.old['apps'])

    def test_missing_target_is_not_authoritative_deletion(self):
        self.assertNotEqual(self.run_scan(apps=[{'name':'beta-api','id':'beta-api-id'}]),0)
        self.assertEqual(self.read()['apps'],self.old['apps'])

    def test_unreadable_target_keeps_previous_measurement(self):
        for field in ('formation_error','dynos_error','addons_error','releases_error','github_error','domains_error','pipeline_error'):
            with self.subTest(field=field):
                self.assertNotEqual(self.run_scan(result=lambda a,t: {**row(a['name'],0),field:'403'}),0)
                self.assertEqual(self.read()['apps'],self.old['apps'])

    def test_malformed_prior_file_is_not_overwritten(self):
        for value in ('{broken', '[]', '{"schema_version":99,"apps":[]}', '{"schema_version":1,"apps":[{}]}'):
            self.out.write_text(value)
            self.assertNotEqual(self.run_scan(),0)
            self.assertEqual(self.out.read_text(),value)

    def test_empty_scope_refuses_without_scan(self):
        before=self.out.read_bytes()
        with patch.object(scan,'token',side_effect=AssertionError('token forbidden')):
            self.assertNotEqual(scan.main(['scan_heroku',str(self.out),'--only']),0)
        self.assertEqual(self.out.read_bytes(),before)

    def test_missing_baseline_does_not_claim_complete_survey(self):
        self.out.unlink(); self.assertEqual(self.run_scan(),0)
        doc=self.read();self.assertIsNone(doc['scanned_at'])
        self.assertTrue(any(x['source']=='heroku:scope' for x in doc['degraded']))

    def test_success_preserves_unknown_fields_inside_requested_row(self):
        self.old['apps'][0]['future']={'nested':123};self.out.write_text(json.dumps(self.old))
        self.assertEqual(self.run_scan(),0)
        self.assertEqual(self.read()['apps'][0]['future'],{'nested':123})

    def test_exception_for_one_target_keeps_peer_success(self):
        def result(a,t):
            if a['name']=='alpha-web':raise TimeoutError('synthetic')
            return row(a['name'],10)
        self.assertNotEqual(self.run_scan(names=('alpha-web','beta-api'),result=result),0)
        self.assertEqual(self.read()['apps'][0],self.old['apps'][0])
        self.assertEqual(self.read()['apps'][1]['monthly_cost'],10)

    def test_shared_writer_lock_refuses_before_credentials(self):
        import oslocks
        with self.out.with_name(self.out.name+'.lock').open('a+b') as lock:
            oslocks.flock(lock.fileno(),oslocks.LOCK_EX|oslocks.LOCK_NB)
            with patch.object(scan,'token',side_effect=AssertionError('token forbidden')):
                self.assertNotEqual(scan.main(['scan_heroku',str(self.out),'--only','alpha-web']),0)

    def test_identity_change_is_not_silent_replacement(self):
        self.assertNotEqual(self.run_scan(apps=[{'name':'alpha-web','id':'replacement-id'}],
                            result=lambda a,t: {**row(a['name']), 'id':a['id']}),0)
        self.assertEqual(self.read()['apps'],self.old['apps'])

    def test_success_clears_only_target_degradation(self):
        self.old['degraded'].append({'source':'heroku:alpha-web','reason':'old unknown'})
        self.out.write_text(json.dumps(self.old));self.assertEqual(self.run_scan(),0)
        self.assertEqual(self.read()['degraded'],self.old['degraded'][:1])

    def test_failed_atomic_write_leaves_original_bytes(self):
        before=self.out.read_bytes()
        with patch.object(scan.atomic, 'write_json', side_effect=OSError('synthetic disk full')):
            self.assertNotEqual(self.run_scan(),0)
        self.assertEqual(self.out.read_bytes(),before)

    def test_new_target_appends_without_changing_existing_rows(self):
        self.assertEqual(self.run_scan(names=('gamma-worker',),apps=[{'name':'gamma-worker','id':'gamma-worker-id'}]),0)
        self.assertEqual(self.read()['apps'][:2],self.old['apps'])
        self.assertEqual(self.read()['apps'][2]['name'],'gamma-worker')

    def test_invalid_inventory_does_not_erase_prior_data(self):
        for apps in ([{}],[{'name':'alpha-web','id':'x'},{'name':'alpha-web','id':'y'}]):
            self.assertNotEqual(self.run_scan(apps=apps),0)
            self.assertEqual(self.read()['apps'],self.old['apps'])

    def test_scan_app_reports_release_and_github_unreadability(self):
        def get(url,*args,**kwargs):
            if url.endswith('/releases') or url.endswith('/github'):
                return {'__error__':'transport'}
            if url.endswith('/pipeline-couplings'):return {'__error__':'404'}
            return []
        with patch.object(scan,'get',side_effect=get):
            result=scan.scan_app({'id':'alpha-id','name':'alpha-web'},'synthetic')
        self.assertEqual(result['releases_error'],'unreadable')
        self.assertEqual(result['github_error'],'unreadable')

    def test_actual_app_reader_rejects_unknown_collection_shapes(self):
        for malformed in ({}, {'unexpected': []}):
            def get(url,*args,**kwargs):
                if url.endswith('/releases'): return []
                if url.endswith('/github'): return {'repo': None}
                if url.endswith('/pipeline-couplings'): return {'__error__':'404'}
                return malformed
            import configuration
            with patch.object(configuration,'enabled',return_value=True), patch.object(scan,'token',return_value=('synthetic',None)), patch.object(scan,'paged',return_value=[{'name':'alpha-web','id':'alpha-web-id'}]), patch.object(scan,'get',side_effect=get), patch.object(scan,'local_checkouts',return_value=({},[])):
                self.assertNotEqual(scan.main(['scan_heroku',str(self.out),'--only','alpha-web']),0)
            self.assertEqual(self.read()['apps'], self.old['apps'])

    def test_actual_empty_collections_are_valid_measurements(self):
        def get(url,*args,**kwargs):
            if url.endswith('/github'): return {'repo': None}
            if url.endswith('/pipeline-couplings'): return {'__error__':'404'}
            return []
        with patch.object(scan,'token',return_value=('synthetic',None)), patch.object(scan,'paged',return_value=[{'name':'alpha-web','id':'alpha-web-id'}]), patch.object(scan,'get',side_effect=get), patch.object(scan,'local_checkouts',return_value=({},[])):
            self.assertEqual(scan.main(['scan_heroku',str(self.out),'--only','alpha-web']),0)
        self.assertEqual(self.read()['apps'][0]['monthly_cost'],0)
        self.assertEqual(self.read()['apps'][0]['formation'],[])

    def test_scoped_refresh_preserves_local_inventory_without_reading_it(self):
        self.old['apps'][0]['local_folders']=['known-folder']
        self.out.write_text(json.dumps(self.old))
        def get(url,*args,**kwargs):
            if url.endswith('/github'):return {'repo':None}
            if url.endswith('/pipeline-couplings'):return {'__error__':'404'}
            return []
        with patch.object(scan,'token',return_value=('synthetic',None)), patch.object(scan,'paged',return_value=[{'name':'alpha-web','id':'alpha-web-id'}]), patch.object(scan,'get',side_effect=get), patch.object(scan,'local_checkouts',side_effect=PermissionError('synthetic unreadable local inventory')) as local:
            self.assertEqual(scan.main(['scan_heroku',str(self.out),'--only','alpha-web']),0)
            local.assert_not_called()
        self.assertEqual(self.read()['apps'][0]['local_folders'],['known-folder'])
        self.assertEqual(self.read()['scoped_refresh']['local_checkouts'],'not_refreshed')

    def test_unknown_github_pipeline_and_malformed_rows_are_not_empty(self):
        for endpoint,bad in (('/github',{}),('/pipeline-couplings',{}),('/formation',[{}]),('/dynos',[{}]),('/addons',[{}]),('/domains',[{}]),('/releases',[{}])):
            with self.subTest(endpoint=endpoint):
                def get(url,*args,**kwargs):
                    if url.endswith(endpoint):return bad
                    if url.endswith('/github'):return {'repo':None}
                    if url.endswith('/pipeline-couplings'):return {'__error__':'404'}
                    return []
                with patch.object(scan,'token',return_value=('synthetic',None)), patch.object(scan,'paged',return_value=[{'name':'alpha-web','id':'alpha-web-id'}]), patch.object(scan,'get',side_effect=get):
                    self.assertNotEqual(scan.main(['scan_heroku',str(self.out),'--only','alpha-web']),0)
                self.assertEqual(self.read()['apps'],self.old['apps'])

    def test_negative_and_nonfinite_provider_quantities_are_unreadable(self):
        cases=[('/formation',[{'type':'web','quantity':-1,'size':'Standard-1X'}])]
        for amount in (-1,float('nan'),float('inf'),True):
            cases.append(('/addons',[{'name':'example-db','plan':{'name':'example'},
                'app':{'name':'alpha-web'},'state':'provisioned','billed_price':{'cents':amount}}]))
        for endpoint,bad in cases:
            with self.subTest(endpoint=endpoint,bad=bad):
                self.out.write_text(json.dumps(self.old))
                def get(url,*args,**kwargs):
                    if url.endswith(endpoint):return bad
                    if url.endswith('/github'):return {'repo':None}
                    if url.endswith('/pipeline-couplings'):return {'__error__':'404'}
                    return []
                with patch.object(scan,'token',return_value=('synthetic',None)), patch.object(scan,'paged',return_value=[{'name':'alpha-web','id':'alpha-web-id'}]), patch.object(scan,'get',side_effect=get):
                    self.assertNotEqual(scan.main(['scan_heroku',str(self.out),'--only','alpha-web']),0)
                self.assertEqual(self.read()['apps'],self.old['apps'])

    def test_zero_provider_quantity_and_price_remain_valid(self):
        def get(url,*args,**kwargs):
            if url.endswith('/formation'):return [{'type':'web','quantity':0,'size':'Basic'}]
            if url.endswith('/addons'):return [{'name':'example-db','plan':{'name':'example'},'app':{'name':'alpha-web'},'state':'provisioned','billed_price':{'cents':0}}]
            if url.endswith('/github'):return {'repo':None}
            if url.endswith('/pipeline-couplings'):return {'__error__':'404'}
            return []
        with patch.object(scan,'token',return_value=('synthetic',None)), patch.object(scan,'paged',return_value=[{'name':'alpha-web','id':'alpha-web-id'}]), patch.object(scan,'get',side_effect=get):
            self.assertEqual(scan.main(['scan_heroku',str(self.out),'--only','alpha-web']),0)
        self.assertEqual(self.read()['apps'][0]['monthly_cost'],0)
        self.assertEqual(self.read()['scoped_refresh']['status'],'complete_for_scope')

    def test_repeated_new_app_refresh_keeps_unknown_local_inventory(self):
        for _ in range(2):
            self.assertEqual(self.run_scan(names=('gamma-worker',),apps=[{'name':'gamma-worker','id':'gamma-worker-id'}]),0)
            self.assertTrue(any(d['source']=='heroku:gamma-worker' and 'local checkout' in d['reason'] for d in self.read()['degraded']))

    def test_full_scan_takes_the_same_lock(self):
        import oslocks
        with self.out.with_name(self.out.name+'.lock').open('a+b') as lock:
            oslocks.flock(lock.fileno(),oslocks.LOCK_EX|oslocks.LOCK_NB)
            with patch.object(scan,'token',side_effect=AssertionError('token forbidden')):
                self.assertNotEqual(scan.main(['scan_heroku',str(self.out)]),0)

if __name__=='__main__':unittest.main()
