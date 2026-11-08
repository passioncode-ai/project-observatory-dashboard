"""Captured-format fixtures for `peer_owner.py`, reusable by any project that vendors it.

Every table here has the exact layout its tool prints or its kernel returns (measured on
macOS 26 and a Linux kernel on 2026-11-06), with synthetic rows: process names, pids and
uids are invented, and no address leaves loopback. Copy this file beside the vendored module.

    NETSTAT_HEADER            the two header lines of `netstat -anv -p tcp` (macOS)
    NETSTAT_SINGLE_ACCOUNT    one account (uid 501): a server on 47311, this account's client
    NETSTAT_MULTI_ACCOUNT     the same plus another account's client (pid 5200, uid 502),
                              root's listener and launchd's socket-activated listener (pid 1)
    PS_UID                    pid -> what `ps -o uid= -p PID` prints for it ("" when gone)
    PROC_NET_TCP_*            `/proc/net/tcp` (Linux, little-endian host), one and two accounts
    PROC_NET_TCP6_MULTI       `/proc/net/tcp6`, an IPv6 loopback pair of two accounts
    PROC_NET_TCP_UNACCEPTED   a connection the server has not accept()ed yet: its server end
                              printed with uid 0 and inode 0
    PROC_NET_TCP6_WILDCARD_UNACCEPTED  the same through another account's `::` listener, the
                              connection IPv4-mapped
    pcblist_n(rows)           a `net.inet.tcp.pcblist_n` blob built from XNU's pack(4) layout,
                              with each socket's TCP state in its xtcpcb_n
    windows_table(rows, af)   a `GetExtendedTcpTable(TCP_TABLE_OWNER_PID_ALL)` buffer
    socket_fdinfo(...)        one macOS `proc_pidfdinfo(PROC_PIDFDSOCKETINFO)` record (792 bytes,
                              `sys/proc_info.h`, offsets measured with clang on macOS 26)

The server in every fixture listens on 127.0.0.1:47311; this account's client is port 50001
(pid 4300), another account's client port 50002 (pid 5200).
"""
from __future__ import annotations

import socket
import struct

OWN_UID = 501
OTHER_UID = 502
SERVER_PORT = 47311
SERVER_PID = 4100
OWN_CLIENT = ("127.0.0.1", 50001)
OWN_CLIENT_PID = 4300
FOREIGN_CLIENT = ("127.0.0.1", 50002)
FOREIGN_CLIENT_PID = 5200

NETSTAT_HEADER = (
    "Active Internet connections (including servers)\n"
    "Proto Recv-Q Send-Q  Local Address                                 Foreign Address                               "
    "(state)          rxbytes      txbytes  rhiwat  shiwat          process:pid    state  options           gencnt    "
    "flags   flags1 usecnt rtncnt fltrs\n")


def _ns(proto: str, local: str, foreign: str, state: str, process: str) -> str:
    return (f"{proto}       0      0  {local:<22} {foreign:<22} {state:<12}          0            0  408064  146988 "
            f"{process:>21}  00102 00000000 000000000e1cd9e0 00000080 01000800      2      0 000000\n")


NETSTAT_SINGLE_ACCOUNT = NETSTAT_HEADER + "".join((
    _ns("tcp4", "127.0.0.1.47311", "*.*", "LISTEN", f"Python:{SERVER_PID}"),
    _ns("tcp4", "127.0.0.1.47311", "127.0.0.1.50001", "ESTABLISHED", f"Python:{SERVER_PID}"),
    _ns("tcp4", "127.0.0.1.50001", "127.0.0.1.47311", "ESTABLISHED", f"Example App:{OWN_CLIENT_PID}"),
    _ns("tcp6", "::1.47070", "*.*", "LISTEN", "example-agent:4400"),
    _ns("tcp4", "192.0.2.10.50100", "198.51.100.7.443", "ESTABLISHED", "curl:4500"),
))

NETSTAT_MULTI_ACCOUNT = NETSTAT_SINGLE_ACCOUNT + "".join((
    _ns("tcp4", "127.0.0.1.47311", "127.0.0.1.50002", "ESTABLISHED", f"Python:{SERVER_PID}"),
    _ns("tcp4", "127.0.0.1.50002", "127.0.0.1.47311", "ESTABLISHED", f"curl:{FOREIGN_CLIENT_PID}"),
    _ns("tcp4", "127.0.0.1.8021", "*.*", "LISTEN", "launchd:1"),
    _ns("tcp4", "*.47555", "*.*", "LISTEN", "example-daemon:300"),
))

#: A TIME_WAIT row whose addresses equal a live connection's: a closed connection, never an answer.
NETSTAT_TIME_WAIT = NETSTAT_MULTI_ACCOUNT + _ns("tcp4", "127.0.0.1.50001", "127.0.0.1.47311", "TIME_WAIT", ":0")

#: An older netstat without the process column: unreadable, so the caller fails closed.
NETSTAT_NO_PROCESS = (
    "Active Internet connections (including servers)\n"
    "Proto Recv-Q Send-Q  Local Address          Foreign Address        (state)\n"
    "tcp4       0      0  127.0.0.1.47311        *.*                    LISTEN\n")

PS_UID = {SERVER_PID: f"  {OWN_UID}\n", OWN_CLIENT_PID: f"  {OWN_UID}\n", 4400: f"  {OWN_UID}\n",
          FOREIGN_CLIENT_PID: f"  {OTHER_UID}\n", 300: "    0\n", 4500: f"  {OWN_UID}\n", 9999: ""}

_PROC_HEADER = ("  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n")


def _proc(sl: int, local: str, remote: str, st: str, uid: int, inode: int) -> str:
    return (f"{sl:>4}: {local} {remote} {st} 00000000:00000000 00:00000000 00000000 {uid:>5}        0 {inode} 1 "
            f"0000000000000000 20 4 30 10 -1\n")


# 127.0.0.1 is 0100007F on a little-endian host; 47311 is B8CF; 50001 C351; 50002 C352.
PROC_NET_TCP_SINGLE = _PROC_HEADER + "".join((
    _proc(0, "0100007F:B8CF", "00000000:0000", "0A", OWN_UID, 1001),
    _proc(1, "0100007F:B8CF", "0100007F:C351", "01", OWN_UID, 1002),
    _proc(2, "0100007F:C351", "0100007F:B8CF", "01", OWN_UID, 1003),
))

PROC_NET_TCP_MULTI = PROC_NET_TCP_SINGLE + "".join((
    _proc(3, "0100007F:B8CF", "0100007F:C352", "01", OWN_UID, 1004),
    _proc(4, "0100007F:C352", "0100007F:B8CF", "01", OTHER_UID, 1005),
    _proc(5, "0100007F:1F55", "00000000:0000", "0A", 0, 1006),
))

# ::1 is four words 00000000 00000000 00000000 01000000 on a little-endian host.
_V6_LOOP = "00000000000000000000000001000000"
PROC_NET_TCP6_MULTI = _PROC_HEADER + "".join((
    _proc(0, f"{_V6_LOOP}:B8CF", f"{'0' * 32}:0000", "0A", OWN_UID, 2001),
    _proc(1, f"{_V6_LOOP}:B8CF", f"{_V6_LOOP}:C352", "01", OWN_UID, 2002),
    _proc(2, f"{_V6_LOOP}:C352", f"{_V6_LOOP}:B8CF", "01", OTHER_UID, 2003),
))

#: Linux prints a connection the server has not accept()ed yet with uid 0 and inode 0: until
#: accept() its child socket has no `struct socket`, so `sock_i_uid` falls back to root. The
#: client end, created by socket() before connect(), has its own inode and its real uid.
#: 50004 is C354.
UNACCEPTED_CLIENT = ("127.0.0.1", 50004)
PROC_NET_TCP_UNACCEPTED = PROC_NET_TCP_SINGLE + "".join((
    _proc(3, "0100007F:B8CF", "0100007F:C354", "01", 0, 0),
    _proc(4, "0100007F:C354", "0100007F:B8CF", "01", OWN_UID, 1007),
))

#: Another account listens on `::` port 47400 (B928) and has not accepted this account's IPv4
#: connection from 127.0.0.1:50005 (C355). ::ffff:127.0.0.1 is the words 00000000 00000000
#: FFFF0000 0100007F on a little-endian host.
WILDCARD_PORT = 47400
WILDCARD_CLIENT = ("127.0.0.1", 50005)
_V4_MAPPED_LOOP = "0000000000000000FFFF00000100007F"
PROC_NET_TCP6_WILDCARD_UNACCEPTED = _PROC_HEADER + "".join((
    _proc(0, f"{'0' * 32}:B928", f"{'0' * 32}:0000", "0A", OTHER_UID, 3001),
    _proc(1, f"{_V4_MAPPED_LOOP}:B928", f"{_V4_MAPPED_LOOP}:C355", "01", 0, 0),
    _proc(2, f"{_V4_MAPPED_LOOP}:C355", f"{_V4_MAPPED_LOOP}:B928", "01", OWN_UID, 3002),
))


TCPS_LISTEN, TCPS_ESTABLISHED, TCPS_TIME_WAIT = 1, 4, 10


def pcblist_n(rows) -> bytes:
    """A `pcblist_n` blob: rows of (local (host, port), remote (host, port), uid, pid[, t_state]);
    the state defaults to LISTEN for a row with no peer, else ESTABLISHED. Each
    socket is the record run the kernel writes: xinpcb_n (104), xsocket_n (104), two xsockbuf_n
    (32), xsockstat_n (136), xtcpcb_n (204), each padded to 8, between two xinpgen headers."""
    def pad(rec: bytes) -> bytes:
        return rec + b"\0" * (-len(rec) % 8)

    def header(count: int) -> bytes:
        return pad(struct.pack("<IIQQ", 24, count, 7, 7))

    out = [header(len(rows))]
    for i, row in enumerate(rows):
        local, remote, uid, pid = row[:4]
        state = row[4] if len(row) > 4 else (TCPS_LISTEN if remote == ("*", 0) else TCPS_ESTABLISHED)
        handle = struct.pack("<Q", 0x1000 + i)
        inp = bytearray(104)
        struct.pack_into("<II", inp, 0, 104, 0x10)
        inp[8:16] = handle
        struct.pack_into(">HH", inp, 16, remote[1], local[1])
        v6 = ":" in local[0] or ":" in remote[0]
        inp[44] = 0x2 if v6 else 0x1
        for at, host in ((48, remote[0]), (64, local[0])):
            if v6:
                inp[at:at + 16] = socket.inet_pton(socket.AF_INET6, "::" if host == "*" else host)
            else:
                inp[at + 12:at + 16] = socket.inet_pton(socket.AF_INET, "0.0.0.0" if host == "*" else host)
        so = bytearray(104)
        struct.pack_into("<II", so, 0, 104, 0x1)
        so[8:16] = struct.pack("<Q", 0x9000 + i)
        so[28:36] = handle
        struct.pack_into("<Ii", so, 64, uid, pid)
        out += [pad(bytes(inp)), pad(bytes(so))]
        for kind, length in ((0x2, 32), (0x4, 32), (0x8, 136), (0x20, 204)):
            rec = bytearray(length)
            struct.pack_into("<II", rec, 0, length, kind)
            if kind == 0x20:
                struct.pack_into("<i", rec, 36, state)                  # xtcpcb_n.t_state
            out.append(pad(bytes(rec)))
    out.append(header(len(rows)))
    return b"".join(out)


PCBLIST_MULTI_ROWS = [
    (("127.0.0.1", SERVER_PORT), ("*", 0), OWN_UID, SERVER_PID),
    (("127.0.0.1", SERVER_PORT), OWN_CLIENT, OWN_UID, SERVER_PID),
    (OWN_CLIENT, ("127.0.0.1", SERVER_PORT), OWN_UID, OWN_CLIENT_PID),
    (("127.0.0.1", SERVER_PORT), FOREIGN_CLIENT, OWN_UID, SERVER_PID),
    (FOREIGN_CLIENT, ("127.0.0.1", SERVER_PORT), OTHER_UID, FOREIGN_CLIENT_PID),
    (("127.0.0.1", 8021), ("*", 0), 0, 1),
    (("::1", 47070), ("*", 0), OWN_UID, 4400),
]


def windows_table(rows, family: int) -> bytes:
    """A `GetExtendedTcpTable` buffer: rows of (local, remote, listening, pid)."""
    out = [struct.pack("<I", len(rows))]
    for local, remote, listening, pid in rows:
        state = 2 if listening else 5                                # MIB_TCP_STATE_LISTEN, _ESTAB
        if family == socket.AF_INET:
            out.append(struct.pack("<I", state) + socket.inet_pton(family, local[0]) + struct.pack(">H", local[1])
                       + b"\0\0" + socket.inet_pton(family, remote[0]) + struct.pack(">H", remote[1]) + b"\0\0"
                       + struct.pack("<I", pid))
        else:
            out.append(socket.inet_pton(family, local[0]) + b"\0" * 4 + struct.pack(">H", local[1]) + b"\0\0"
                       + socket.inet_pton(family, remote[0]) + b"\0" * 4 + struct.pack(">H", remote[1]) + b"\0\0"
                       + struct.pack("<II", state, pid))
    return b"".join(out)


WINDOWS_MULTI_ROWS = [
    (("127.0.0.1", SERVER_PORT), ("0.0.0.0", 0), True, SERVER_PID),
    (("127.0.0.1", SERVER_PORT), FOREIGN_CLIENT, False, SERVER_PID),
    (FOREIGN_CLIENT, ("127.0.0.1", SERVER_PORT), False, FOREIGN_CLIENT_PID),
]


def socket_fdinfo(local, remote, state: int, handle: int, *, family: int = socket.AF_INET, kind: int = 2) -> bytes:
    """A `struct socket_fdinfo` for one socket: `soi_so` (the handle) at 160, `soi_kind` at 256
    (SOCKINFO_TCP 2), then the `in_sockinfo` at 264: ports as ints holding a network-order port,
    `insi_vflag` at +24, foreign and local `in4in6_addr` at +32 and +48, `tcpsi_state` at +80."""
    blob = bytearray(792)
    struct.pack_into("<Q", blob, 160, handle)
    struct.pack_into("<i", blob, 256, kind)
    ini = 264
    struct.pack_into(">H", blob, ini, remote[1])
    struct.pack_into(">H", blob, ini + 4, local[1])
    if family == socket.AF_INET:
        blob[ini + 24] = 0x1
        for at, host in ((ini + 32, remote[0]), (ini + 48, local[0])):
            blob[at + 12:at + 16] = socket.inet_pton(socket.AF_INET, "0.0.0.0" if host == "*" else host)
    else:
        blob[ini + 24] = 0x2
        for at, host in ((ini + 32, remote[0]), (ini + 48, local[0])):
            blob[at:at + 16] = socket.inet_pton(socket.AF_INET6, "::" if host == "*" else host)
    struct.pack_into("<i", blob, ini + 80, state)
    return bytes(blob)
