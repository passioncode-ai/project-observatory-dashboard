#!/usr/bin/env python3
"""`peer_owner.py` and `host_accounts.py`: who owns each end of a loopback connection, and how
many people's accounts the host has (OBS-355 part 1, docs/design/ACCESS.md rule 10).

The parsers run over captured-format fixtures (`peer_owner_fixtures.py`), the selection over a
faked table, and one live case per platform over a real loopback pair, so the macOS record
layout and the Linux table are checked on every CI row that runs this suite.
"""
from __future__ import annotations

import ast
import http.server
import os
import pathlib
import socket
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import host_accounts  # noqa: E402
import peer_owner as P  # noqa: E402
import peer_owner_fixtures as F  # noqa: E402

SERVER = ("127.0.0.1", F.SERVER_PORT)


class FakeSocket:
    def __init__(self, name, peer):
        self.name, self.peer = name, peer

    def getsockname(self):
        return self.name

    def getpeername(self):
        return self.peer


def ps_from_fixture(argv):
    return F.PS_UID[int(argv[-1])]


class Parsers(unittest.TestCase):
    def test_netstat_rows_with_spaced_process_names(self):
        rows = P.parse_netstat(F.NETSTAT_MULTI_ACCOUNT)
        self.assertEqual(len(rows), 9)
        client = [r for r in rows if r.local == F.OWN_CLIENT][0]
        self.assertEqual((client.pid, client.uid, client.listening), (F.OWN_CLIENT_PID, None, False))
        listeners = {r.local: r.pid for r in rows if r.listening}
        self.assertEqual(listeners, {SERVER: F.SERVER_PID, ("::1", 47070): 4400, ("127.0.0.1", 8021): 1,
                                     ("*", 47555): 300})

    def test_netstat_without_the_process_column_is_unreadable(self):
        with self.assertRaises(P.TableUnreadable):
            P.parse_netstat(F.NETSTAT_NO_PROCESS)

    def test_ps_uid(self):
        self.assertEqual(P.parse_ps_uid(F.PS_UID[F.FOREIGN_CLIENT_PID]), F.OTHER_UID)
        self.assertIsNone(P.parse_ps_uid(""))

    def test_proc_net_tcp_carries_each_rows_uid(self):
        rows = P.parse_proc_net_tcp(F.PROC_NET_TCP_MULTI, family=socket.AF_INET, byteorder="little")
        by = {(r.local, r.remote): r for r in rows}
        self.assertEqual(by[(F.FOREIGN_CLIENT, SERVER)].uid, F.OTHER_UID)
        self.assertEqual(by[(F.OWN_CLIENT, SERVER)].uid, F.OWN_UID)
        self.assertTrue(by[(SERVER, ("*", 0))].listening)
        self.assertEqual(by[(("127.0.0.1", 8021), ("*", 0))].uid, 0)

    def test_proc_net_tcp6(self):
        rows = P.parse_proc_net_tcp(F.PROC_NET_TCP6_MULTI, family=socket.AF_INET6, byteorder="little")
        self.assertIn((("::1", 50002), ("::1", F.SERVER_PORT), F.OTHER_UID),
                      [(r.local, r.remote, r.uid) for r in rows])

    def test_pcblist_n_pairs_each_socket_with_its_owner(self):
        rows = P.parse_pcblist_n(F.pcblist_n(F.PCBLIST_MULTI_ROWS))
        got = sorted((r.local, r.remote, r.uid, r.pid, r.listening, r.time_wait) for r in rows)
        want = sorted((tuple(l), ("*", 0) if r == ("*", 0) else tuple(r), u, p, r == ("*", 0), False)
                      for l, r, u, p in F.PCBLIST_MULTI_ROWS)
        self.assertEqual(got, want)

    def test_pcblist_n_reads_the_tcp_state(self):
        rows = P.parse_pcblist_n(F.pcblist_n([(F.OWN_CLIENT, SERVER, 0, 0, F.TCPS_TIME_WAIT),
                                              (("127.0.0.1", 47999), ("*", 0), 501, 9, F.TCPS_ESTABLISHED)]))
        self.assertEqual([(r.time_wait, r.listening) for r in rows], [(True, False), (False, False)])

    def test_pcblist_n_refuses_an_unknown_header(self):
        with self.assertRaises(P.TableUnreadable):
            P.parse_pcblist_n(b"\xff" * 64)

    def test_pcblist_n_refuses_a_record_past_the_end(self):
        blob = F.pcblist_n(F.PCBLIST_MULTI_ROWS[:1])
        with self.assertRaises(P.TableUnreadable):
            P.parse_pcblist_n(blob[:24 + 60])                          # the first xinpcb_n cut short

    def test_calibration_needs_the_listener_and_both_ends_exactly(self):
        rows = P.parse_pcblist_n(F.pcblist_n([(SERVER, ("*", 0), 501, 7), (SERVER, F.OWN_CLIENT, 501, 7),
                                              (F.OWN_CLIENT, SERVER, 501, 7)]))
        self.assertTrue(P.calibration_holds(rows, SERVER, F.OWN_CLIENT, uid=501, pid=7))
        self.assertFalse(P.calibration_holds(rows, SERVER, F.OWN_CLIENT, uid=501, pid=8))
        self.assertFalse(P.calibration_holds(rows[:2], SERVER, F.OWN_CLIENT, uid=501, pid=7))
        self.assertFalse(P.calibration_holds(rows + rows[2:], SERVER, F.OWN_CLIENT, uid=501, pid=7))

    def test_windows_tables(self):
        rows = P.parse_windows_tcp_table(F.windows_table(F.WINDOWS_MULTI_ROWS, socket.AF_INET), socket.AF_INET)
        self.assertEqual([(r.local, r.remote, r.listening, r.pid) for r in rows],
                         [(SERVER, ("*", 0), True, F.SERVER_PID), (SERVER, F.FOREIGN_CLIENT, False, F.SERVER_PID),
                          (F.FOREIGN_CLIENT, SERVER, False, F.FOREIGN_CLIENT_PID)])
        v6 = [(("::1", 47311), ("::", 0), True, 7), (("::1", 50003), ("::1", 47311), False, 8)]
        rows6 = P.parse_windows_tcp_table(F.windows_table(v6, socket.AF_INET6), socket.AF_INET6)
        self.assertEqual([(r.local, r.remote, r.listening, r.pid) for r in rows6],
                         [(("::1", 47311), ("*", 0), True, 7), (("::1", 50003), ("::1", 47311), False, 8)])

    def test_addresses_have_one_spelling(self):
        self.assertEqual(P.norm_host("::ffff:127.0.0.1"), "127.0.0.1")
        self.assertEqual(P.norm_host("0.0.0.0"), "*")
        self.assertEqual(P.norm_host("::"), "*")
        self.assertEqual(P.norm_host("localhost"), "127.0.0.1")


class Classification(unittest.TestCase):
    def test_pid_one_and_uid_zero_are_system(self):
        self.assertEqual(P.classify(0, 300, method="x", own_uid=501).kind, P.SYSTEM)
        self.assertEqual(P.classify(501, 1, method="x", own_uid=501).kind, P.SYSTEM)
        self.assertEqual(P.classify(0, 1, method="x", own_uid=0).kind, P.SYSTEM)

    def test_root_owns_its_own_uid_zero(self):
        # This process's uid is compared before the uid 0 rule: a root client owns its root listener.
        self.assertEqual(P.classify(0, 300, method="x", own_uid=0).kind, P.THIS_ACCOUNT)
        self.assertEqual(P.classify(0, None, method="x", own_uid=0).kind, P.THIS_ACCOUNT)
        self.assertEqual(P.classify(501, 9, method="x", own_uid=0).kind, P.ANOTHER_ACCOUNT)
        self.assertEqual(P.classify(501, 9, method="x", own_uid=501).kind, P.THIS_ACCOUNT)
        self.assertEqual(P.classify(502, 9, method="x", own_uid=501).kind, P.ANOTHER_ACCOUNT)

    def test_windows_sids(self):
        own = "S-1-5-21-1-2-3-1001"
        self.assertEqual(P.classify_sid(own, 50, own_sid=own).kind, P.THIS_ACCOUNT)
        self.assertEqual(P.classify_sid("S-1-5-21-1-2-3-1002", 50, own_sid=own).kind, P.ANOTHER_ACCOUNT)
        self.assertEqual(P.classify_sid("S-1-5-18", 50, own_sid=own).kind, P.SYSTEM)
        self.assertEqual(P.classify_sid(None, 4, own_sid=own).kind, P.SYSTEM)
        unknown = P.classify_sid(None, 50, own_sid=own)
        self.assertEqual((unknown.kind, unknown.reason), (P.UNKNOWN, "process-not-readable"))


class Selection(unittest.TestCase):
    """The question picks the right row of a table that holds both ends of every connection."""

    def table(self, text=F.NETSTAT_MULTI_ACCOUNT):
        return mock.patch.object(P, "read_table", return_value=(P.parse_netstat(text), "netstat"))

    def setUp(self):
        self.own_uid = mock.patch.object(P.os, "getuid", return_value=F.OWN_UID, create=True)
        self.own_uid.start()
        self.ps = mock.patch.object(P, "_run", side_effect=ps_from_fixture)
        self.ps.start()
        P._LISTENERS.clear()

    def tearDown(self):
        self.ps.stop()
        self.own_uid.stop()
        P._LISTENERS.clear()

    def test_peer_picks_the_client_socket(self):
        with self.table():
            foreign = P.peer(FakeSocket(SERVER, F.FOREIGN_CLIENT))
            own = P.peer(FakeSocket(SERVER, F.OWN_CLIENT))
        self.assertEqual((foreign.kind, foreign.uid, foreign.pid),
                         (P.ANOTHER_ACCOUNT, F.OTHER_UID, F.FOREIGN_CLIENT_PID))
        self.assertEqual((own.kind, own.pid), (P.THIS_ACCOUNT, F.OWN_CLIENT_PID))

    def test_server_end_picks_the_accepted_socket_not_the_listener(self):
        with self.table():
            owner = P.server_end(FakeSocket(F.FOREIGN_CLIENT, SERVER))
        self.assertEqual((owner.kind, owner.pid), (P.THIS_ACCOUNT, F.SERVER_PID))

    def test_peer_at_takes_ipv4_mapped_addresses(self):
        with self.table():
            owner = P.peer_at(("::ffff:127.0.0.1", F.SERVER_PORT, 0, 0), ("::ffff:127.0.0.1", 50002, 0, 0))
        self.assertEqual(owner.kind, P.ANOTHER_ACCOUNT)

    def test_a_time_wait_row_is_never_the_answer(self):
        with self.table(F.NETSTAT_TIME_WAIT):
            owner = P.peer(FakeSocket(SERVER, F.OWN_CLIENT))
        self.assertEqual((owner.kind, owner.pid), (P.THIS_ACCOUNT, F.OWN_CLIENT_PID))

    def test_two_live_rows_are_unknown(self):
        rows = [P.Row(F.FOREIGN_CLIENT, SERVER, False, 501, 1), P.Row(F.FOREIGN_CLIENT, SERVER, False, 502, 2)]
        with mock.patch.object(P, "read_table", return_value=(rows, "sysctl")):
            owner = P.peer(FakeSocket(SERVER, F.FOREIGN_CLIENT))
        self.assertEqual((owner.kind, owner.reason), (P.UNKNOWN, "several-rows"))

    def test_two_listeners_are_unknown(self):
        rows = [P.Row(SERVER, ("*", 0), True, 501, 1), P.Row(SERVER, ("*", 0), True, 502, 2)]
        with mock.patch.object(P, "read_table", return_value=(rows, "sysctl")):
            owner = P.listener(F.SERVER_PORT)
        self.assertEqual((owner.kind, owner.reason), (P.UNKNOWN, "several-rows"))

    def test_no_row_is_unknown(self):
        with self.table():
            owner = P.peer(FakeSocket(SERVER, ("127.0.0.1", 59999)))
        self.assertEqual((owner.kind, owner.reason), (P.UNKNOWN, "no-row"))

    def test_unreadable_table_is_unknown_with_a_reason(self):
        with mock.patch.object(P, "read_table", side_effect=P.TableUnreadable("netstat: exit 1")):
            owner = P.peer(FakeSocket(SERVER, F.FOREIGN_CLIENT))
        self.assertEqual((owner.kind, owner.reason), (P.UNKNOWN, "netstat: exit 1"))

    def test_a_gone_process_is_unknown(self):
        rows = [P.Row(F.FOREIGN_CLIENT, SERVER, False, None, 9999)]
        with mock.patch.object(P, "read_table", return_value=(rows, "netstat")):
            owner = P.peer(FakeSocket(SERVER, F.FOREIGN_CLIENT))
        self.assertEqual((owner.kind, owner.reason), (P.UNKNOWN, "process-gone"))

    def test_launchd_socket_is_system_without_a_ps(self):
        with self.table():
            owner = P.listener(8021)
        self.assertEqual(owner.kind, P.SYSTEM)

    def test_listener_prefers_the_exact_bind_then_the_wildcard_then_none(self):
        with self.table():
            self.assertEqual(P.listener(F.SERVER_PORT).pid, F.SERVER_PID)
            self.assertEqual(P.listener(47555).kind, P.SYSTEM)          # root's 0.0.0.0 listener
            self.assertEqual(P.listener(47070).kind, P.THIS_ACCOUNT)    # ::1
            self.assertEqual(P.listener(1).kind, P.NONE)

    def test_listener_is_cached_briefly(self):
        with self.table() as read:
            P.listener(F.SERVER_PORT)
            P.listener(F.SERVER_PORT)
        self.assertEqual(read.call_count, 1)


class LinuxListener(unittest.TestCase):
    """Linux prints a connection the server has not accept()ed yet with uid 0 and inode 0. Read as
    it stands, a client refused its own server and a root client read another account's listener as
    its own uid. So on Linux the server end is always its listener's (state 0A), never the
    connection row's (peer_owner 1.2.0, the contract's DEC-0154 HA-2 as amended)."""

    def setUp(self):
        self.uid = F.OWN_UID
        self.own_uid = mock.patch.object(P.os, "getuid", side_effect=lambda: self.uid, create=True)
        self.own_uid.start()

    def tearDown(self):
        self.own_uid.stop()

    def proc(self, rows):
        return mock.patch.object(P, "read_table", return_value=(rows, "proc"))

    def table(self, *listeners, server_row=(0, 0), local=SERVER, client=F.UNACCEPTED_CLIENT):
        """The server end (uid, inode), the client end, and these listeners as (address, uid)."""
        rows = [P.Row(local, client, False, server_row[0], None, False, server_row[1]),
                P.Row(client, local, False, F.OWN_UID, None, False, 1007)]
        return rows + [P.Row(at, ("*", 0), True, uid, None, False, 900 + i) for i, (at, uid) in enumerate(listeners)]

    def end(self, client=F.UNACCEPTED_CLIENT, server=SERVER):
        return P.server_end(FakeSocket(client, server))

    def test_the_parser_reads_the_inode(self):
        rows = P.parse_proc_net_tcp(F.PROC_NET_TCP_UNACCEPTED, family=socket.AF_INET, byteorder="little")
        by = {(r.local, r.remote): r for r in rows}
        self.assertEqual((by[(SERVER, F.UNACCEPTED_CLIENT)].uid, by[(SERVER, F.UNACCEPTED_CLIENT)].inode), (0, 0))
        self.assertEqual((by[(F.UNACCEPTED_CLIENT, SERVER)].uid, by[(F.UNACCEPTED_CLIENT, SERVER)].inode),
                         (F.OWN_UID, 1007))
        self.assertEqual(by[(SERVER, ("*", 0))].inode, 1001)
        # The other tables carry no inode.
        self.assertTrue(all(r.inode is None for r in P.parse_netstat(F.NETSTAT_MULTI_ACCOUNT)))
        self.assertTrue(all(r.inode is None for r in P.parse_pcblist_n(F.pcblist_n(F.PCBLIST_MULTI_ROWS))))

    def test_an_unaccepted_row_whose_listener_is_ours_reads_as_ours(self):
        rows = P.parse_proc_net_tcp(F.PROC_NET_TCP_UNACCEPTED, family=socket.AF_INET, byteorder="little")
        with self.proc(rows) as read:
            owner = self.end()
        self.assertEqual((owner.kind, owner.uid, owner.method), (P.THIS_ACCOUNT, F.OWN_UID, "proc"))
        self.assertIsNone(P.refusal(owner, F.SERVER_PORT))
        self.assertEqual(read.call_count, 1)                            # one read, no waiting for accept()

    def test_the_connection_rows_own_uid_is_ignored_entirely(self):
        # Unaccepted (uid 0, inode 0), or accepted with any uid and an inode: the listener answers.
        for server_row in ((0, 0), (0, 77), (F.OTHER_UID, 78), (F.OWN_UID, 79)):
            with self.subTest(server_row=server_row):
                with self.proc(self.table((SERVER, F.OTHER_UID), server_row=server_row)):
                    self.assertEqual((self.end().kind, self.end().uid), (P.ANOTHER_ACCOUNT, F.OTHER_UID))
                with self.proc(self.table((SERVER, F.OWN_UID), server_row=server_row)):
                    self.assertEqual(self.end().kind, P.THIS_ACCOUNT)
        # Without its connection row at all, the listener still answers.
        with self.proc([P.Row(SERVER, ("*", 0), True, F.OWN_UID, None, False, 5)]):
            self.assertEqual(self.end().kind, P.THIS_ACCOUNT)

    def test_a_root_client_refuses_another_accounts_slow_listener(self):
        self.uid = 0
        with self.proc(self.table((SERVER, F.OTHER_UID))):
            owner = self.end()
        self.assertEqual((owner.kind, owner.uid), (P.ANOTHER_ACCOUNT, F.OTHER_UID))
        self.assertEqual(P.refusal(owner, F.SERVER_PORT)[0], P.SERVER_NOT_OWN)

    def test_a_root_client_owns_its_own_root_listener(self):
        self.uid = 0
        with self.proc(self.table((SERVER, 0))):
            owner = self.end()
        self.assertEqual((owner.kind, owner.uid), (P.THIS_ACCOUNT, 0))
        self.assertIsNone(P.refusal(owner, F.SERVER_PORT))
        self.uid = F.OWN_UID                                            # an ordinary client: root's is the system's
        with self.proc(self.table((SERVER, 0))):
            self.assertEqual(P.refusal(self.end(), F.SERVER_PORT)[0], P.SERVER_NOT_OWN)

    def test_a_wildcard_listener(self):
        rows = P.parse_proc_net_tcp(F.PROC_NET_TCP6_WILDCARD_UNACCEPTED, family=socket.AF_INET6, byteorder="little")
        with self.proc(rows):
            owner = self.end(("::ffff:127.0.0.1", F.WILDCARD_CLIENT[1], 0, 0),
                             ("::ffff:127.0.0.1", F.WILDCARD_PORT, 0, 0))
        self.assertEqual((owner.kind, owner.uid), (P.ANOTHER_ACCOUNT, F.OTHER_UID))
        with self.proc(self.table((("*", F.SERVER_PORT), F.OWN_UID))):
            self.assertEqual(self.end().kind, P.THIS_ACCOUNT)
        # The exact bind takes the connection before the wildcard, as the kernel delivers it.
        with self.proc(self.table((("*", F.SERVER_PORT), F.OTHER_UID), (SERVER, F.OWN_UID))):
            self.assertEqual(self.end().kind, P.THIS_ACCOUNT)

    def test_listener_rows_of_one_uid_count_as_that_uid(self):
        with self.proc(self.table((SERVER, F.OWN_UID), (SERVER, F.OWN_UID))):          # SO_REUSEPORT
            self.assertEqual((self.end().kind, self.end().uid), (P.THIS_ACCOUNT, F.OWN_UID))
        rows = P.parse_proc_net_tcp(F.PROC_NET_TCP6_WILDCARD_UNACCEPTED, family=socket.AF_INET6, byteorder="little")
        rows.append(P.Row(("*", F.WILDCARD_PORT), ("*", 0), True, F.OTHER_UID, None, False, 3003))  # its 0.0.0.0 twin
        with self.proc(rows):
            owner = self.end(F.WILDCARD_CLIENT, ("127.0.0.1", F.WILDCARD_PORT))
        self.assertEqual((owner.kind, owner.uid), (P.ANOTHER_ACCOUNT, F.OTHER_UID))

    def test_two_listeners_with_different_uids_are_unknown(self):
        with self.proc(self.table((SERVER, F.OWN_UID), (SERVER, F.OTHER_UID))):
            owner = self.end()
        self.assertEqual((owner.kind, owner.reason), (P.UNKNOWN, "listeners-disagree"))
        self.assertEqual(P.refusal(owner, F.SERVER_PORT)[0], P.SERVER_UNVERIFIED)

    def test_no_listener_is_unknown(self):
        with self.proc(self.table((("127.0.0.1", 1), F.OWN_UID), server_row=(F.OWN_UID, 80))):
            owner = self.end()
        self.assertEqual((owner.kind, owner.reason), (P.UNKNOWN, "no-listener"))
        self.assertEqual(P.refusal(owner, F.SERVER_PORT)[0], P.SERVER_UNVERIFIED)

    def test_a_listener_row_without_its_own_inode_is_not_trusted(self):
        rows = self.table((SERVER, F.OWN_UID))
        rows.append(P.Row(SERVER, ("*", 0), True, F.OWN_UID, None, False, 0))
        with self.proc(rows):
            owner = self.end()
        self.assertEqual((owner.kind, owner.reason), (P.UNKNOWN, "listener-no-socket"))

    def test_other_tables_still_read_the_accepted_socket(self):
        # macOS and Windows carry the accepting process's own row: unchanged.
        with mock.patch.object(P, "read_table", return_value=(P.parse_netstat(F.NETSTAT_MULTI_ACCOUNT), "netstat")), \
                mock.patch.object(P, "_run", side_effect=ps_from_fixture):
            owner = self.end(F.FOREIGN_CLIENT)
        self.assertEqual((owner.kind, owner.pid), (P.THIS_ACCOUNT, F.SERVER_PID))

    def test_the_server_reading_the_client_row_is_unchanged(self):
        rows = P.parse_proc_net_tcp(F.PROC_NET_TCP_MULTI, family=socket.AF_INET, byteorder="little")
        with self.proc(rows):
            foreign, own = P.peer(FakeSocket(SERVER, F.FOREIGN_CLIENT)), P.peer(FakeSocket(SERVER, F.OWN_CLIENT))
        self.assertEqual((foreign.kind, foreign.uid), (P.ANOTHER_ACCOUNT, F.OTHER_UID))
        self.assertEqual((own.kind, own.uid), (P.THIS_ACCOUNT, F.OWN_UID))

    def test_the_server_reading_a_client_row_without_a_socket_fails_closed(self):
        """A client end without its inode is a socket its process already closed: no listener owns
        it, so the server refuses, and never borrows the owner of a listener on that port."""
        rows = [P.Row(F.UNACCEPTED_CLIENT, SERVER, False, 0, None, False, 0),
                P.Row(SERVER, F.UNACCEPTED_CLIENT, False, F.OWN_UID, None, False, 1011),
                P.Row(F.UNACCEPTED_CLIENT, ("*", 0), True, F.OWN_UID, None, False, 1012)]
        self.uid = 0                                                    # a root server: uid 0 is still no owner
        with self.proc(rows):
            owner = P.peer(FakeSocket(SERVER, F.UNACCEPTED_CLIENT))
        self.assertEqual((owner.kind, owner.reason), (P.UNKNOWN, "no-socket"))


class OwnConnection(unittest.TestCase):
    """The client check: refused before a byte of the request is sent."""

    def setUp(self):
        self.received = []
        outer = self

        class Recorder(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):  # noqa: N802
                outer.received.append(self.headers.get("Authorization"))
                self.send_response(204)
                self.send_header("Connection", "close")                 # http.client reconnects by itself
                self.end_headers()
                self.close_connection = True

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def send(self, owner, expect_pid=None):
        with mock.patch.object(P, "server_end", return_value=owner):
            conn = P.own_connection(self.port, expect_pid=expect_pid, timeout=3)
        conn.request("GET", "/", headers={"Authorization": "Bearer synthetic"})
        status = conn.getresponse().status
        conn.close()
        return status

    def test_refusals_send_nothing(self):
        cases = [(P.Owner(P.ANOTHER_ACCOUNT, 502, 9, "x"), None, P.SERVER_NOT_OWN),
                 (P.Owner(P.SYSTEM, 0, 1, "x"), None, P.SERVER_NOT_OWN),
                 (P.Owner(P.UNKNOWN, None, None, "x", "no-row"), None, P.SERVER_UNVERIFIED),
                 (P.Owner(P.THIS_ACCOUNT, 501, 9, "x"), 10, P.SERVER_NOT_THIS_WORKSPACE)]
        for owner, pid, code in cases:
            with self.subTest(code=code, kind=owner.kind):
                with self.assertRaises(P.ListenerRefused) as caught:
                    self.send(owner, pid)
                self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.received, [])

    def test_this_accounts_server_gets_the_request(self):
        self.assertEqual(self.send(P.Owner(P.THIS_ACCOUNT, 501, 9, "x"), 9), 204)
        self.assertEqual(self.received, ["Bearer synthetic"])

    def test_on_linux_the_listener_decides_before_anything_is_sent(self):
        listener = lambda uid: [P.Row(("127.0.0.1", self.port), ("*", 0), True, uid, None, False, 42)]
        with mock.patch.object(P.os, "getuid", return_value=F.OWN_UID, create=True):
            with mock.patch.object(P, "read_table", return_value=(listener(F.OWN_UID), "proc")):
                conn = P.own_connection(self.port, timeout=3)
                conn.request("GET", "/", headers={"Authorization": "Bearer synthetic"})
                self.assertEqual(conn.getresponse().status, 204)
                conn.close()
            with mock.patch.object(P, "read_table", return_value=(listener(F.OTHER_UID), "proc")):
                with self.assertRaises(P.ListenerRefused) as caught:
                    P.own_connection(self.port, timeout=3)
        self.assertEqual(caught.exception.code, P.SERVER_NOT_OWN)
        self.assertEqual(self.received, ["Bearer synthetic"])

    def test_the_reconnect_after_connection_close_is_checked_again(self):
        """The server answers `Connection: close`; http.client's next request opens a new connection
        by itself (auto_open), and that one reaches another account's listener: refused, unsent."""
        ends = [P.Owner(P.THIS_ACCOUNT, 501, 9, "x"), P.Owner(P.ANOTHER_ACCOUNT, 502, 10, "x")]
        with mock.patch.object(P, "server_end", side_effect=ends) as checked:
            conn = P.own_connection(self.port, timeout=3)
            conn.request("GET", "/", headers={"Authorization": "Bearer synthetic"})
            self.assertEqual(conn.getresponse().status, 204)
            with self.assertRaises(P.ListenerRefused) as caught:
                conn.request("GET", "/", headers={"Authorization": "Bearer synthetic"})
            conn.close()
        self.assertEqual(caught.exception.code, P.SERVER_NOT_OWN)
        self.assertEqual(checked.call_count, 2)
        self.assertEqual(self.received, ["Bearer synthetic"])


@unittest.skipUnless(sys.platform == "darwin" or sys.platform.startswith("linux"), "a POSIX socket table")
class Live(unittest.TestCase):
    """A real loopback pair in this process: both ends are this account's, on the primary method."""

    def test_both_ends_of_a_real_pair_are_this_account(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        client = socket.create_connection(listener.getsockname())
        accepted, _ = listener.accept()
        try:
            want = "sysctl" if sys.platform == "darwin" else "proc"
            self.assertEqual(P.method(), want, "the macOS record layout failed calibration on this host")
            for owner in (P.peer(accepted), P.server_end(client), P.listener(listener.getsockname()[1])):
                self.assertEqual((owner.kind, owner.uid, owner.method), (P.THIS_ACCOUNT, os.getuid(), want))
            if sys.platform == "darwin":
                self.assertEqual(P.peer(accepted).pid, os.getpid())
        finally:
            for s in (client, accepted, listener):
                s.close()

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux prints an unaccepted connection without its owner")
    def test_a_connection_not_accepted_yet_is_this_accounts(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(4)
        client = socket.create_connection(listener.getsockname())
        try:
            owner = P.server_end(client)                                # before accept(): uid 0, inode 0 in the table
            self.assertEqual((owner.kind, owner.uid, owner.method), (P.THIS_ACCOUNT, os.getuid(), "proc"))
        finally:
            client.close()
            listener.close()

    @unittest.skipUnless(sys.platform == "darwin", "the fallback is macOS's")
    def test_the_netstat_fallback_agrees(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        client = socket.create_connection(listener.getsockname())
        accepted, _ = listener.accept()
        try:
            with mock.patch.object(P, "method", return_value="netstat"):
                owner = P.peer(accepted)
            self.assertEqual((owner.kind, owner.pid, owner.method), (P.THIS_ACCOUNT, os.getpid(), "netstat"))
        finally:
            for s in (client, accepted, listener):
                s.close()


class SelfContained(unittest.TestCase):
    """Other projects vendor peer_owner.py by commit: standard library only, a version in its header."""

    def test_imports_only_the_standard_library(self):
        tree = ast.parse((ROOT / "peer_owner.py").read_text(encoding="utf-8"))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names.add((node.module or "").split(".")[0])
        names.discard("__future__")
        self.assertTrue(names <= set(sys.stdlib_module_names), names - set(sys.stdlib_module_names))

    def test_the_uid_is_read_in_one_function(self):
        # The engine's OS boundary (tests/test_osprivacy.py, STANDALONE_UID) lets this vendorable file
        # read the uid itself, and only in `_posix_uid`.
        tree = ast.parse((ROOT / "peer_owner.py").read_text(encoding="utf-8"))
        readers = {fn.name for fn in ast.walk(tree) if isinstance(fn, ast.FunctionDef)
                   for node in ast.walk(fn) if isinstance(node, ast.Attribute) and node.attr == "getuid"}
        self.assertEqual(readers, {"_posix_uid"})

    def test_names_and_version(self):
        self.assertRegex(P.PEER_OWNER_VERSION, r"^\d+\.\d+\.\d+$")
        self.assertIn(f"peer_owner {P.PEER_OWNER_VERSION}", P.__doc__)
        for name in ("listener", "server_end", "peer", "Owner", "own_connection"):
            self.assertTrue(callable(getattr(P, name)), name)
        self.assertEqual(P.KINDS, ("this-account", "another-account", "system", "unknown", "none"))


class HostAccounts(unittest.TestCase):
    def test_dscl_counts_people_hidden_admins_and_guest_not_service_accounts(self):
        self.assertEqual(host_accounts.parse_dscl("_www 70\nnobody -2\nroot 0\ndaemon 1\nalpha 501\n"), {501})
        self.assertEqual(host_accounts.parse_dscl("alpha 501\nbeta 502\nhidden 503\n"), {501, 502, 503})
        listed = "alpha 501\nGuest 201\nadmin2 450\nrunner 451\n"
        shells = "alpha /bin/zsh\nGuest /bin/zsh\nadmin2 /bin/bash\nrunner /usr/bin/false\n"
        self.assertEqual(host_accounts.parse_dscl(listed, shells), {501, 201, 450})
        # A person's account from 501 up counts whatever its shell: a desktop login does not use it.
        self.assertEqual(host_accounts.parse_dscl("alpha 501\nbeta 502\n", "alpha /bin/zsh\nbeta /usr/bin/false\n"),
                         {501, 502})

    def test_a_directory_binding_is_several(self):
        self.assertFalse(host_accounts.parse_search_path("CSPSearchPath: /Local/Default\n"))
        self.assertFalse(host_accounts.parse_search_path("CSPSearchPath:\n /Local/Default\n /BSD/local\n"))
        self.assertTrue(host_accounts.parse_search_path("CSPSearchPath:\n /Local/Default\n /Active Directory/EX\n"))
        self.assertTrue(host_accounts.parse_nsswitch("passwd:   files sss\ngroup: files\n"))
        self.assertTrue(host_accounts.parse_nsswitch("passwd: files [NOTFOUND=return] ldap\n"))
        self.assertFalse(host_accounts.parse_nsswitch("# passwd: ldap\npasswd: files systemd\n"))

    def test_running_people_are_counted_with_the_database(self):
        procs = host_accounts.parse_ps_uids("0\n  1\n 262\n 501\n 501\n 1702\n")
        self.assertEqual(host_accounts.people(procs, {501}, 501, 2 ** 31 - 1), {501, 1702})

    def test_directory_bound_reads_as_none(self):
        host_accounts._CACHE[:] = [None, None]
        with mock.patch.object(host_accounts, "_read", return_value={"count": None, "reason": "directory-bound"}):
            self.assertIsNone(host_accounts.count())
            self.assertEqual(host_accounts.detail()["reason"], "directory-bound")
        host_accounts._CACHE[:] = [None, None]

    def test_linux_people_who_can_log_in(self):
        lo, hi = host_accounts.parse_login_defs("# comment\nUID_MIN 1000\nUID_MAX 60000\n")
        pw = [SimpleNamespace(pw_uid=1000, pw_shell="/bin/bash"), SimpleNamespace(pw_uid=1001, pw_shell="/usr/sbin/nologin"),
              SimpleNamespace(pw_uid=65534, pw_shell="/bin/sh"), SimpleNamespace(pw_uid=1002, pw_shell="/bin/zsh")]
        self.assertEqual(host_accounts.passwd_uids(pw, lo, hi), {1000, 1002})

    def test_linux_process_owner_is_the_status_real_uid(self):
        text = "Name:\tbash\nUmask:\t0022\nState:\tS (sleeping)\nUid:\t1001\t1001\t1001\t1001\nGid:\t1001\n"
        self.assertEqual(host_accounts.parse_proc_status_uid(text), 1001)
        self.assertIsNone(host_accounts.parse_proc_status_uid("Name:\tkthreadd\n"))

    def test_windows_profiles_whose_folder_exists(self):
        self.assertEqual(host_accounts.count_profiles([("S-1-5-18", True), ("S-1-5-21-1-2-3-500", True),
                                                       ("S-1-5-21-1-2-3-1001", True), ("S-1-5-21-1-2-3-1002", True),
                                                       ("S-1-5-21-1-2-3-1003", False)]), 2)

    def test_count_is_cached_and_unreadable_is_none(self):
        host_accounts._CACHE[:] = [None, None]
        with mock.patch.object(host_accounts, "_read", return_value={"count": None, "reason": "unreadable"}) as read:
            self.assertIsNone(host_accounts.count())
            self.assertIsNone(host_accounts.count())
        self.assertEqual(read.call_count, 1)
        host_accounts._CACHE[:] = [None, None]


if __name__ == "__main__":
    unittest.main()
