"""Policy provenance is explicit; no legacy identity is guessed from a folder."""
import copy
import hashlib
import json
import random
import unittest
from unittest.mock import patch
import adv_j1_property as A


class PolicyAdmission(unittest.TestCase):
    def setUp(self):
        net=patch('socket.socket.connect',side_effect=AssertionError('synthetic only'))
        net.start();self.addCleanup(net.stop)
        self.m,self.p,self.door,_=A.make_door(random.Random(7))
        self.p.echo=lambda x:x;self.p.drift=0;self.p.page=self.m.PAGE=100;self.m.PAGES_MAX=100
        self.pol=A.T._pol(1,limit=10)
        self.name='project-a-prod-k0'

    def direct(self):
        return self.m.issue_key(self.name,10,'example','vault:project-a/prod/K0','project-a')

    def policy(self): return self.m.issue_for_policy(self.pol,'example')

    def test_new_policy_row_binds_exact_digest_before_mint_and_refuses_rotation_before_reads(self):
        original=self.m._request;seen=[]
        def request(path,*a,**kw):
            if path=='/keys':seen.append(copy.deepcopy(self.m.ledger()['issued'][self.name]))
            return original(path,*a,**kw)
        self.m._request=request;self.policy()
        binding=self.m.ledger()['issued'][self.name]['policy_binding']
        digest=hashlib.sha256(json.dumps(self.pol,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
        self.assertEqual(binding['policy_sha256'],digest)
        self.assertEqual(binding['schema_version'],1)
        self.assertEqual(binding['origin'],'issue-for-policy')
        self.assertEqual(seen[0]['policy_binding'],binding)
        before=copy.deepcopy(self.m.ledger());calls=self.p.calls
        with self.assertRaisesRegex(RuntimeError,'policy'):self.m.rotate_one(self.name)
        self.assertEqual(self.p.calls,calls);self.assertEqual(self.m.ledger(),before)

    def test_unmarked_standalone_and_identical_project_folder_rotation_remain_allowed(self):
        self.direct();self.assertNotIn('policy_binding',self.m.ledger()['issued'][self.name])
        self.assertEqual(self.m.rotate_one(self.name)['rotations'],1)

    def test_explicit_policy_apply_attests_existing_row_but_dry_run_does_not(self):
        self.direct();before=copy.deepcopy(self.m.ledger());writes=self.p.writes
        self.m.issue_for_policy(self.pol,'example',dry_run=True)
        self.assertEqual(before,self.m.ledger())
        self.policy();row=self.m.ledger()['issued'][self.name]
        self.assertIn('policy_binding',row);self.assertEqual(writes,self.p.writes)
        first=copy.deepcopy(row['policy_binding']);self.policy()
        self.assertEqual(first,self.m.ledger()['issued'][self.name]['policy_binding'])

    def test_changed_policy_updates_snapshot_but_never_authorizes_rotation(self):
        self.policy();old=self.m.ledger()['issued'][self.name]['policy_binding']['policy_sha256']
        self.pol['limitUsd']=20;self.policy()
        binding=self.m.ledger()['issued'][self.name]['policy_binding']
        self.assertNotEqual(old,binding['policy_sha256'])
        self.assertEqual(binding['headroom'],'not-verified')
        self.assertEqual(binding['limit_usd'],20)
        with self.assertRaisesRegex(RuntimeError,'policy'):self.m.rotate_one(self.name)

    def test_failed_or_unknown_mint_retains_provenance(self):
        self.p.fail['mint']=[1,'502-after']
        with self.assertRaises(self.m.PolicyPartial):self.policy()
        row=self.m.ledger()['issued'][self.name]
        self.assertTrue(row['pending']);self.assertIn('policy_binding',row)

    def test_binding_write_failure_prevents_provider_mutation(self):
        self.direct();writes=self.p.writes
        with patch.object(self.m,'save_ledger',side_effect=OSError('synthetic disk failure')):
            with self.assertRaises(OSError):self.policy()
        self.assertEqual(writes,self.p.writes)

    def test_malformed_stale_or_unknown_binding_never_bypasses_refusal(self):
        self.direct()
        for binding in (None,False,{}, {'schema_version':99},{'recorded_at':'2020-01-01T00:00:00Z'}):
            doc=self.m.ledger();doc['issued'][self.name]['policy_binding']=binding;self.m.save_ledger(doc)
            calls=self.p.calls
            with self.assertRaisesRegex(RuntimeError,'policy'):self.m.rotate_one(self.name)
            self.assertEqual(calls,self.p.calls)

    def test_standalone_reissue_cannot_erase_existing_provenance(self):
        self.policy();row=self.m.ledger()['issued'][self.name];binding=copy.deepcopy(row['policy_binding'])
        del self.p.keys[row['hash']];self.p.order.remove(row['hash'])
        self.direct();self.assertEqual(binding,self.m.ledger()['issued'][self.name]['policy_binding'])

    def test_origin_is_sanitized_and_new_row_dry_run_does_not_write(self):
        self.pol['source']='/synthetic-private-marker/policy.json'
        self.m.issue_for_policy(self.pol,'example',dry_run=True)
        self.assertFalse(self.m.ledger()['issued'])
        self.policy();binding=self.m.ledger()['issued'][self.name]['policy_binding']
        self.assertNotIn('synthetic-private-marker',json.dumps(binding))
        self.assertNotIn('source',binding)

    def test_legacy_alias_of_policy_identity_cannot_rotate_through_an_unmarked_row(self):
        self.policy();doc=self.m.ledger();row=copy.deepcopy(doc['issued'][self.name]);row.pop('policy_binding')
        doc['issued']['legacy-alias']=row;self.m.save_ledger(doc);calls=self.p.calls
        with self.assertRaisesRegex(RuntimeError,'policy'):self.m.rotate_one('legacy-alias')
        self.assertEqual(calls,self.p.calls)
        self.assertEqual(doc,self.m.ledger())

    def test_reserved_identity_alias_is_guarded_but_distinct_hash_is_not(self):
        self.direct();doc=self.m.ledger();h=doc['issued'][self.name]['hash']
        doc['issued']['marked']={'account':'example','hash':'different','successor_hash':h,'policy_binding':None}
        self.m.save_ledger(doc);calls=self.p.calls
        with self.assertRaisesRegex(RuntimeError,'policy'):self.m.rotate_one(self.name)
        self.assertEqual(calls,self.p.calls)
        doc['issued']['marked']['successor_hash']='unrelated';self.m.save_ledger(doc)
        self.assertEqual(self.m.rotate_one(self.name)['rotations'],1)

    def test_only_explicitly_named_policy_source_is_attested(self):
        self.direct()
        self.m.issue_key('other',1,'example','vault:project-a/prod/OTHER','project-a')
        self.policy();self.assertNotIn('policy_binding',self.m.ledger()['issued']['other'])


if __name__=='__main__':unittest.main()
