"""Revoke uses current provider identity and bounded, current scan evidence."""
import copy
import datetime
import json
import os
import random
import unittest
from unittest.mock import patch
import adv_j1_property as A


class RevokeConsumers(unittest.TestCase):
    def setUp(self):
        net = patch('socket.socket.connect', side_effect=AssertionError('synthetic only'))
        net.start(); self.addCleanup(net.stop)
        self.m, self.p, self.door, _ = A.make_door(random.Random(7))
        self.p.echo = lambda x: x
        self.p.drift = 0
        self.p.page = self.m.PAGE = 100
        self.m.PAGES_MAX = 100
        self.m.issue_for_policy(A.T._pol(1, limit=10), 'example')
        self.name = next(iter(self.m.ledger()['issued']))
        self.scan = self.m.paths.SCRATCH / 'openrouter.json'
        self.receipt = {'schema_version': 1, 'scanned_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        'keys': [], 'destinations': {}, 'destination_hashes': {}, 'degraded': []}
        self.write()

    def write(self):
        self.scan.write_text(json.dumps(self.receipt))

    def refused(self):
        before = (copy.deepcopy(self.m.ledger()), copy.deepcopy(self.p.keys), self.p.writes)
        with self.assertRaises((RuntimeError, ValueError)):
            self.m.revoke_key(self.name)
        self.assertEqual(before, (self.m.ledger(), self.p.keys, self.p.writes))

    def test_current_successor_label_only_holder_after_actual_rotation_is_refused(self):
        old_label = self.m.ledger()['issued'][self.name]['label']
        self.m.rotate_one(self.name)
        row = self.m.ledger()['issued'][self.name]
        current_label = self.p.keys[row['hash']]['label']
        self.assertNotEqual(old_label, current_label)
        self.receipt['destinations'] = {'worker': current_label}
        self.write(); self.refused()

    def test_current_complete_known_unused_scan_allows_exact_delete(self):
        h = self.m.ledger()['issued'][self.name]['hash']
        self.assertTrue(self.m.revoke_key(self.name)['revoked'])
        self.assertNotIn(h, self.p.keys)
        self.assertNotIn(self.name, self.m.ledger()['issued'])

    def test_missing_source_refuses(self):
        self.scan.unlink(); self.refused()

    def test_unread_source_refuses_without_exposing_exception_body(self):
        original = self.m.osprivacy.open
        def denied(path, *args, **kwargs):
            if path == self.scan: raise PermissionError('synthetic-private-marker')
            return original(path, *args, **kwargs)
        with patch.object(self.m.osprivacy, 'open', side_effect=denied):
            with self.assertRaises((RuntimeError, ValueError)) as exc:
                self.m.revoke_key(self.name)
        self.assertNotIn('synthetic-private-marker', str(exc.exception))

    def test_stale_future_and_unknown_timestamps_refuse(self):
        for stamp in ('2020-01-01T00:00:00Z','2099-01-01T00:00:00Z',None,'', '2026-10-13T00:00:00'):
            with self.subTest(stamp=stamp):
                self.receipt['scanned_at'] = stamp
                self.write(); self.refused()

    def test_malformed_or_degraded_sources_refuse(self):
        for key,value in (('schema_version',True),('keys',[None]),('keys',{}),('destinations',[]),
                          ('destination_hashes',{'worker':None}),('degraded',[{'reason':'synthetic-private-marker'}]),
                          ('degraded',None)):
            with self.subTest(key=key,value=value):
                original=copy.deepcopy(self.receipt); self.receipt[key]=value
                self.write(); self.refused(); self.receipt=original
        self.scan.write_text('not-json'); self.refused()

    def test_missing_required_collections_are_unknown(self):
        for field in ('keys','destinations','destination_hashes','degraded'):
            with self.subTest(field=field):
                original=copy.deepcopy(self.receipt); self.receipt.pop(field)
                self.write(); self.refused(); self.receipt=original

    def test_unbound_or_unknown_provider_identity_refuses(self):
        row=copy.deepcopy(self.p.keys[self.m.ledger()['issued'][self.name]['hash']])
        for changed in (dict(row,hash='different-identity'), dict(row,label=None)):
            with patch.object(self.m,'find_key',return_value=changed): self.refused()

    def test_changed_receipt_during_identity_lookup_refuses(self):
        original = self.m.find_key
        def change(*args):
            row=original(*args)
            self.receipt['destinations']={'worker':row['label']}; self.write()
            return row
        with patch.object(self.m,'find_key',side_effect=change): self.refused()

    def test_current_hash_and_renamed_provider_name_holders_refuse(self):
        row=self.p.keys[self.m.ledger()['issued'][self.name]['hash']]
        self.receipt['destination_hashes']={'worker':{'hash':row['hash'],'label':row['label']}}
        self.write(); self.refused()
        self.receipt['destination_hashes']={}; row['name']='renamed-current'
        self.receipt['keys']=[{'name':row['name'],'label':None,'serves':'worker'}]
        self.write(); self.refused()

    def test_symlink_and_oversized_source_refuse(self):
        self.scan.unlink(); target=self.scan.with_suffix('.target');target.write_text(json.dumps(self.receipt))
        try: self.scan.symlink_to(target)
        except OSError: self.skipTest('symlinks unavailable')
        self.refused();self.scan.unlink()
        self.scan.write_bytes(b' '*(4*1024*1024+1));self.refused()

    @unittest.skipUnless(hasattr(os,'mkfifo'),'FIFO unsupported')
    def test_fifo_refuses_without_blocking(self):
        self.scan.unlink();os.mkfifo(self.scan);self.refused()


if __name__=='__main__': unittest.main()
