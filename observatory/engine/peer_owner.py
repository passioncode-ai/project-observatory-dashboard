"""peer_owner: which OS account owns each end of a loopback TCP connection.

    peer_owner 1.1.0. One file, Python standard library only, no imports from any other
    module of its project, so another project can vendor it by commit: copy this file and
    record the commit it came from. Its tests and their fixtures live beside the engine's
    tests (`tests/test_peer_owner.py`, `tests/peer_owner_fixtures.py`).

WHY. Loopback is one per machine, not one per OS account. On a host with several accounts,
every account's processes reach every service any account listens with on 127.0.0.1, and a
loopback socket proves nothing about who is on the other end. This module answers that
question from the host's socket table, read without privileges:

    listener(port) -> Owner       who listens on 127.0.0.1:<port> (also 0.0.0.0, ::1, ::)
    server_end(sock) -> Owner     for a client's connected socket: who accepted it
    peer(sock) -> Owner           for a server's accepted socket: who connected
    peer_at(server, client)       the same as `peer`, from two (host, port) addresses
    own_connection(port, ...)     a CheckedHTTPConnection, connected: every connect, the first and
                                  any reconnect http.client makes after `Connection: close`, checks
                                  that the server end is this account's, or raises ListenerRefused
                                  before a byte of the request is sent

`Owner.kind` is one of:

    this-account      the socket belongs to the account running this process
    another-account   another OS account's
    system            uid 0, pid 1 (launchd holds the sockets of socket-activated jobs) or, on
                      Windows, the system's own accounts and processes
    unknown           the owner could not be told; `reason` says why
    none              `listener` only: nothing listens on that port

A caller that must not leak (a client about to send a bearer, a server on a multi-account
host about to read a request) treats everything but `this-account` as a refusal, `unknown`
included: fail closed.

HOW, ON EACH OS. Every read is bounded (MAX_TABLE_BYTES of table, TIMEOUT_SECONDS) and the
answer is never cached across connections; only `listener` is cached, for LISTENER_TTL. A
TIME_WAIT row is a connection already closed and is never an answer; two live rows with the same
addresses are `unknown` (`several-rows`), never a guess.

- macOS, primary (`sysctl`): `sysctl net.inet.tcp.pcblist_n` through ctypes, the record list
  `netstat` itself reads. Each connection is an `xinpcb_n` (addresses and ports) followed by an
  `xsocket_n` that carries `so_uid` and `so_last_pid` (XNU `bsd/netinet/in_pcb.h` and
  `bsd/sys/socketvar.h`, both under `#pragma pack(4)`); the two are paired by the pcb handle
  (`xi_inpp` equals `so_pcb`). Measured on macOS 26: about 4 ms for 770 sockets, every pid equal
  to the one `netstat` printed. The `xtcpcb_n` record that follows gives the TCP state
  (`t_state`), so listeners and TIME_WAIT rows are told apart. The layout is not an API, so the
  first read in a process calibrates it: it opens a connected pair of its own (listen, connect,
  accept) and the listener and both ends of the pair must read back with their exact addresses,
  this process's uid and its pid. A layout that fails calibration is not used again in that
  process.
- macOS, fallback (`netstat`): `/usr/sbin/netstat -anv -p tcp`, its `process:pid` column, then
  `ps -o uid= -p PID` for the owner of the one row a question needs. Measured: about 20 ms over
  846 sockets, and it names the pids of root's listeners, which `lsof` run by an ordinary account
  cannot see. Another local service implemented the same check this way; it is kept as the
  documented fallback for a macOS whose record layout the calibration refuses.
- Linux (`proc`): `/proc/net/tcp` and `/proc/net/tcp6`, whose `uid` column every account may
  read (also under `hidepid=2`, which hides `/proc/<pid>` and not this file). No pid: that is
  only readable for this account's own processes, and the decision needs the uid alone.
- Windows (`iphlpapi`): `GetExtendedTcpTable(TCP_TABLE_OWNER_PID_ALL)` gives each row's owning
  pid; `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)`, `OpenProcessToken(TOKEN_QUERY)` and
  `GetTokenInformation(TokenUser)` give its account SID, compared with this process's. A process
  that cannot be opened (another account's, usually) is `unknown`, never `another-account`:
  the caller refuses either way, and the reason says which it was. `Owner.uid` is None there.
- Anything else: `unknown`, reason `unsupported-platform`.

`LOCAL_PEERCRED`, `getpeereid` and Linux `SO_PEERCRED` answer this question for Unix-domain
sockets only, not for TCP, which is why a socket table is read here at all.

WHAT IT NEVER DOES. It never reads a byte from the socket it is asked about, never resolves a
name, never sends anything, and never needs a privilege. It reports the process names
`netstat` prints only as far as the pid behind them.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import dataclasses
import http.client
import ipaddress
import os
import socket
import struct
import subprocess
import sys
import threading
import time

VERSION = "1.1.0"

THIS_ACCOUNT = "this-account"
ANOTHER_ACCOUNT = "another-account"
SYSTEM = "system"
UNKNOWN = "unknown"
NONE = "none"
KINDS = (THIS_ACCOUNT, ANOTHER_ACCOUNT, SYSTEM, UNKNOWN, NONE)

#: The most table bytes one read accepts. The spec of this module asked for 1 MB; measured,
#: `pcblist_n` costs about 860 bytes a socket, so 1 MB would hold about 1,200 sockets and a busy
#: developer machine passes that. 4 MiB holds about 4,800.
MAX_TABLE_BYTES = 4 * 1024 * 1024
#: Every subprocess this module starts is stopped after this many seconds.
TIMEOUT_SECONDS = 2.0
#: `listener(port)` answers from a read at most this old (a doctor asks several times in a row).
LISTENER_TTL = 1.0

NETSTAT = "/usr/sbin/netstat"
PS = "/bin/ps"

#: Client codes of `own_connection` (ListenerRefused.code).
SERVER_NOT_OWN = "server-not-own"
SERVER_UNVERIFIED = "server-unverified"
SERVER_NOT_THIS_WORKSPACE = "server-not-this-workspace"


@dataclasses.dataclass(frozen=True)
class Owner:
    """Who owns one end of a connection, or a listener. `reason` says why when `kind` is unknown."""
    kind: str
    uid: int | None = None
    pid: int | None = None
    method: str = "none"
    reason: str | None = None

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class Row:
    """One TCP socket of the host's table. `uid` is None where the table carries no owner
    (netstat, Windows): it is resolved from `pid` only for the row a question selects."""
    local: tuple[str, int]
    remote: tuple[str, int]
    listening: bool
    uid: int | None = None
    pid: int | None = None
    time_wait: bool = False


class TableUnreadable(Exception):
    """The socket table could not be read; the message is the reason, never a socket's content."""


class ListenerRefused(Exception):
    """`own_connection` refused the server end; nothing was sent on the connection."""

    def __init__(self, code: str, message: str, owner: Owner | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.owner = owner


# -- addresses --------------------------------------------------------------------------------------------------------

def norm_host(host: str) -> str:
    """One spelling per address: an IPv4-mapped IPv6 address is its IPv4 form, `*` is the
    wildcard, `localhost` is 127.0.0.1. Anything unparseable is returned unchanged (and matches
    only itself)."""
    if host in ("*", ""):
        return "*"
    if host == "localhost":
        return "127.0.0.1"
    host = host.split("%", 1)[0]
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_unspecified:
        return "*"
    return str(ip)


def _address(addr) -> tuple[str, int]:
    """A socket address (2-tuple for IPv4, 4-tuple for IPv6) as a normalized (host, port)."""
    return norm_host(str(addr[0])), int(addr[1])


# -- classification ---------------------------------------------------------------------------------------------------

def classify(uid: int | None, pid: int | None, *, method: str, own_uid: int | None = None) -> Owner:
    """`system` for uid 0 or pid 1, `this-account` for this process's uid, else `another-account`."""
    if uid is None:
        return Owner(UNKNOWN, None, pid, method, "no-owner")
    if own_uid is None:
        own_uid = os.getuid() if hasattr(os, "getuid") else None
    if uid == 0 or pid == 1:
        return Owner(SYSTEM, uid, pid, method)
    if own_uid is not None and uid == own_uid:
        return Owner(THIS_ACCOUNT, uid, pid, method)
    return Owner(ANOTHER_ACCOUNT, uid, pid, method)


# -- parsers: pure functions over captured text or bytes (the tests' fixtures) ---------------------------------------

def parse_netstat(text: str) -> list[Row]:
    """`netstat -anv -p tcp` on macOS. Process names may hold spaces ("Some App:123"), so the
    `process:pid` column is what is left between the leading and the trailing columns, both
    counted from the header. A header without `process:pid` (an older macOS) is unreadable."""
    lines = text.splitlines()
    header = next((i for i, line in enumerate(lines) if "process:pid" in line and "Proto" in line), None)
    if header is None:
        raise TableUnreadable("netstat-format: no process:pid column")
    names = lines[header].split()
    at = names.index("process:pid")
    # "Local Address" and "Foreign Address" are two header words each and one data column each.
    leading = at - sum(1 for w in names[:at] if w == "Address")
    trailing = len(names) - at - 1
    rows: list[Row] = []
    for line in lines[header + 1:]:
        tok = line.split()
        if len(tok) < leading + trailing + 1 or not tok[0].startswith("tcp"):
            continue
        local = _netstat_address(tok[3])
        remote = _netstat_address(tok[4])
        if local is None or remote is None:
            continue
        process = " ".join(tok[leading:len(tok) - trailing])
        pid = None
        if ":" in process:
            tail = process.rsplit(":", 1)[1]
            pid = int(tail) if tail.isdigit() else None
        rows.append(Row(local, remote, tok[5] == "LISTEN", None, pid, tok[5] == "TIME_WAIT"))
    return rows


def _netstat_address(text: str) -> tuple[str, int] | None:
    """`127.0.0.1.47311`, `::1.47070`, `*.47555`, `*.*`: the port after the last dot. A long IPv6
    address is cut short by netstat, which only ever matters off loopback."""
    if "." not in text:
        return None
    host, port = text.rsplit(".", 1)
    try:
        return norm_host(host), (0 if port == "*" else int(port))
    except ValueError:
        return None


def parse_ps_uid(text: str) -> int | None:
    """`ps -o uid= -p PID`: one number, or nothing when the process is gone."""
    text = text.strip()
    return int(text) if text.isdigit() else None


def parse_proc_net_tcp(text: str, *, family: int, byteorder: str | None = None) -> list[Row]:
    """`/proc/net/tcp` (family AF_INET) or `/proc/net/tcp6` (AF_INET6). The kernel prints each
    32-bit word of an address in host byte order and each port in network order, in hex."""
    order = byteorder or sys.byteorder
    rows: list[Row] = []
    for line in text.splitlines()[1:]:
        tok = line.split()
        if len(tok) < 10 or not tok[0].endswith(":"):
            continue
        try:
            local = _proc_address(tok[1], family, order)
            remote = _proc_address(tok[2], family, order)
            uid = int(tok[7])
        except ValueError:
            continue
        rows.append(Row(local, remote, tok[3].upper() == "0A", uid, None, tok[3].upper() == "06"))
    return rows


def _proc_address(text: str, family: int, order: str) -> tuple[str, int]:
    host, port = text.split(":")
    raw = bytes.fromhex(host)
    if order == "little":
        raw = b"".join(raw[i:i + 4][::-1] for i in range(0, len(raw), 4))
    want = 4 if family == socket.AF_INET else 16
    if len(raw) != want:
        raise ValueError("address length")
    return norm_host(socket.inet_ntop(family, raw)), int(port, 16)


#: XNU's export records (`#pragma pack(4)`): kinds and the offsets this module reads.
XSO_SOCKET = 0x001
XSO_INPCB = 0x010
XSO_TCPCB = 0x020
_TCP_STATE = 36                   # xtcpcb_n.t_state: TCPS_LISTEN 1, TCPS_TIME_WAIT 10
_TCP_MIN = 40
_TCPS_LISTEN = 1
_TCPS_TIME_WAIT = 10
_INP_HANDLE = slice(8, 16)        # xinpcb_n.xi_inpp
_INP_PORTS = 16                   # inp_fport, inp_lport, network order
_INP_VFLAG = 44                   # inp_vflag: INP_IPV4 0x1, INP_IPV6 0x2
_INP_FOREIGN = 48                 # 16 bytes; an IPv4 address in its last 4 (in_addr_4in6)
_INP_LOCAL = 64
_INP_MIN = 80
_SO_PCB = slice(28, 36)           # xsocket_n.so_pcb, equal to the inpcb's xi_inpp
_SO_UID = 64                      # so_uid, then so_last_pid
_SO_MIN = 72


def parse_pcblist_n(blob: bytes) -> list[Row]:
    """The `net.inet.tcp.pcblist_n` record list: an `xinpgen` header, then for each socket a run
    of records each starting with (length, kind) and padded to 8 bytes, ended by a second
    `xinpgen`. Records of other kinds (socket buffers, stats, the tcpcb) are skipped by length."""
    if len(blob) < 8:
        raise TableUnreadable("pcblist_n: short table")
    first = struct.unpack_from("<I", blob, 0)[0]
    if first < 8 or first > 64:
        raise TableUnreadable("pcblist_n: unknown header")
    off = (first + 7) & ~7
    pcbs: dict[bytes, tuple] = {}
    owners: dict[bytes, tuple[int, int]] = {}
    states: dict[bytes, int] = {}
    current: bytes | None = None                                    # the inpcb the records that follow belong to
    while off + 8 <= len(blob):
        length, kind = struct.unpack_from("<II", blob, off)
        if length < 8:
            break
        if length == first and off + length >= len(blob) - 8:
            break                                                   # the closing xinpgen
        if off + length > len(blob):
            raise TableUnreadable("pcblist_n: a record runs past the table")
        if kind == XSO_INPCB and length >= _INP_MIN:
            rec = blob[off:off + length]
            fport, lport = struct.unpack_from(">HH", rec, _INP_PORTS)
            if rec[_INP_VFLAG] & 0x1:
                foreign = socket.inet_ntop(socket.AF_INET, rec[_INP_FOREIGN + 12:_INP_FOREIGN + 16])
                local = socket.inet_ntop(socket.AF_INET, rec[_INP_LOCAL + 12:_INP_LOCAL + 16])
            else:
                foreign = socket.inet_ntop(socket.AF_INET6, rec[_INP_FOREIGN:_INP_FOREIGN + 16])
                local = socket.inet_ntop(socket.AF_INET6, rec[_INP_LOCAL:_INP_LOCAL + 16])
            current = bytes(rec[_INP_HANDLE])
            pcbs[current] = (norm_host(local), lport, norm_host(foreign), fport)
        elif kind == XSO_SOCKET and length >= _SO_MIN:
            uid, pid = struct.unpack_from("<Ii", blob, off + _SO_UID)
            owners[bytes(blob[off + _SO_PCB.start:off + _SO_PCB.stop])] = (uid, pid)
        elif kind == XSO_TCPCB and length >= _TCP_MIN and current is not None:
            states[current] = struct.unpack_from("<i", blob, off + _TCP_STATE)[0]
        off += (length + 7) & ~7
    rows = []
    for handle, (local, lport, foreign, fport) in pcbs.items():
        uid, pid = owners.get(handle, (None, None))
        state = states.get(handle)
        # Without a tcpcb, a listener is the row with no peer: foreign port 0, foreign address the wildcard.
        listening = state == _TCPS_LISTEN if state is not None else (fport == 0 and foreign == "*")
        rows.append(Row((local, lport), (foreign, fport), listening, uid, pid, state == _TCPS_TIME_WAIT))
    return rows


#: Windows' MIB rows (iphlpapi): sizes and the LISTEN and TIME_WAIT states.
_MIB_TCP_STATE_LISTEN = 2
_MIB_TCP_STATE_TIME_WAIT = 11


def parse_windows_tcp_table(blob: bytes, family: int) -> list[Row]:
    """A `GetExtendedTcpTable(..., TCP_TABLE_OWNER_PID_ALL)` buffer: a DWORD count, then
    MIB_TCPROW_OWNER_PID (24 bytes) or MIB_TCP6ROW_OWNER_PID (56 bytes) rows. Addresses are in
    network order in memory; a port is the low 16 bits of its DWORD, in network order."""
    if len(blob) < 4:
        raise TableUnreadable("iphlpapi: short table")
    count = struct.unpack_from("<I", blob, 0)[0]
    size, base = (24, 4) if family == socket.AF_INET else (56, 4)
    rows = []
    for i in range(count):
        at = base + i * size
        if at + size > len(blob):
            break
        if family == socket.AF_INET:
            state = struct.unpack_from("<I", blob, at)[0]
            local = socket.inet_ntop(family, blob[at + 4:at + 8])
            lport = struct.unpack_from(">H", blob, at + 8)[0]
            remote = socket.inet_ntop(family, blob[at + 12:at + 16])
            rport = struct.unpack_from(">H", blob, at + 16)[0]
            pid = struct.unpack_from("<I", blob, at + 20)[0]
        else:
            local = socket.inet_ntop(family, blob[at:at + 16])
            lport = struct.unpack_from(">H", blob, at + 20)[0]
            remote = socket.inet_ntop(family, blob[at + 24:at + 40])
            rport = struct.unpack_from(">H", blob, at + 44)[0]
            state = struct.unpack_from("<I", blob, at + 48)[0]
            pid = struct.unpack_from("<I", blob, at + 52)[0]
        rows.append(Row((norm_host(local), lport), (norm_host(remote), rport),
                        state == _MIB_TCP_STATE_LISTEN, None, pid, state == _MIB_TCP_STATE_TIME_WAIT))
    return rows


# -- readers: the live table on this host ------------------------------------------------------------------------------

_LOCK = threading.Lock()
#: macOS: None until the first read calibrates the sysctl layout, then True or False for this process.
_SYSCTL_OK: list = [None]


def _run(argv: list[str]) -> str:
    try:
        done = subprocess.run(argv, capture_output=True, timeout=TIMEOUT_SECONDS, check=False,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TableUnreadable(f"{os.path.basename(argv[0])}: {type(exc).__name__}") from None
    if done.returncode != 0:
        raise TableUnreadable(f"{os.path.basename(argv[0])}: exit {done.returncode}")
    if len(done.stdout) > MAX_TABLE_BYTES:
        raise TableUnreadable(f"{os.path.basename(argv[0])}: table over {MAX_TABLE_BYTES} bytes")
    return done.stdout.decode("utf-8", "replace")


def _sysctl_blob(name: bytes = b"net.inet.tcp.pcblist_n") -> bytes:
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    except OSError as exc:
        raise TableUnreadable(f"sysctl: libc: {type(exc).__name__}") from None
    fn = libc.sysctlbyname
    fn.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p,
                   ctypes.c_size_t]
    fn.restype = ctypes.c_int
    for _attempt in range(3):
        need = ctypes.c_size_t(0)
        if fn(name, None, ctypes.byref(need), None, 0) != 0:
            raise TableUnreadable(f"sysctl: errno {ctypes.get_errno()}")
        size = min(int(need.value * 1.25) + 4096, MAX_TABLE_BYTES)
        if need.value > MAX_TABLE_BYTES:
            raise TableUnreadable(f"sysctl: table over {MAX_TABLE_BYTES} bytes")
        buf = ctypes.create_string_buffer(size)
        got = ctypes.c_size_t(size)
        if fn(name, buf, ctypes.byref(got), None, 0) == 0:
            return buf.raw[:got.value]
        if ctypes.get_errno() != 12:                                 # ENOMEM: the table grew, ask again
            raise TableUnreadable(f"sysctl: errno {ctypes.get_errno()}")
    raise TableUnreadable("sysctl: the table kept growing")


def calibration_holds(rows: list[Row], server: tuple[str, int], client: tuple[str, int], *,
                      uid: int, pid: int) -> bool:
    """The listener and both ends of one connected pair, each exactly once, live, with their exact
    addresses and this uid and pid: what a layout must read back to be trusted."""
    def one(local, remote, listening):
        hits = [r for r in rows if r.local == local and r.remote == remote and r.listening == listening
                and not r.time_wait]
        return len(hits) == 1 and hits[0].uid == uid and hits[0].pid == pid
    return (one(server, ("*", 0), True) and one(server, client, False) and one(client, server, False))


def _calibrate_sysctl() -> bool:
    """Open a connected pair of this process's own (listen, connect, accept) and read it back
    (`calibration_holds`). A layout that does not hold is never trusted in this process, and
    `netstat` answers instead."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client = accepted = None
    try:
        probe.bind(("127.0.0.1", 0))
        probe.listen(1)
        client = socket.create_connection(probe.getsockname(), timeout=TIMEOUT_SECONDS)
        accepted, _ = probe.accept()
        rows = parse_pcblist_n(_sysctl_blob())
        return calibration_holds(rows, _address(probe.getsockname()), _address(client.getsockname()),
                                 uid=os.getuid(), pid=os.getpid())
    except (TableUnreadable, OSError, struct.error, ValueError):
        return False
    finally:
        for s in (client, accepted, probe):
            if s is not None:
                s.close()


def method() -> str:
    """The method the next read on this host uses: sysctl, netstat, proc, iphlpapi or none."""
    if sys.platform == "darwin":
        with _LOCK:
            if _SYSCTL_OK[0] is None:
                _SYSCTL_OK[0] = _calibrate_sysctl()
            return "sysctl" if _SYSCTL_OK[0] else "netstat"
    if sys.platform.startswith("linux"):
        return "proc"
    if sys.platform == "win32":
        return "iphlpapi"
    return "none"


def read_table() -> tuple[list[Row], str]:
    """The host's TCP sockets and the method that read them; TableUnreadable with the reason."""
    how = method()
    if how == "sysctl":
        try:
            return parse_pcblist_n(_sysctl_blob()), how
        except (TableUnreadable, struct.error, ValueError):
            how = "netstat"                                          # this read only; calibration stands
    if how == "netstat":
        return parse_netstat(_run([NETSTAT, "-anv", "-p", "tcp"])), how
    if how == "proc":
        rows: list[Row] = []
        for name, family in (("/proc/net/tcp", socket.AF_INET), ("/proc/net/tcp6", socket.AF_INET6)):
            try:
                with open(name, "rb") as fh:
                    raw = fh.read(MAX_TABLE_BYTES + 1)
            except FileNotFoundError:
                continue                                             # no IPv6 on this kernel
            except OSError as exc:
                raise TableUnreadable(f"proc: {type(exc).__name__}") from None
            if len(raw) > MAX_TABLE_BYTES:
                raise TableUnreadable(f"proc: table over {MAX_TABLE_BYTES} bytes")
            rows += parse_proc_net_tcp(raw.decode("ascii", "replace"), family=family)
        return rows, how
    if how == "iphlpapi":
        return _windows_table(), how
    raise TableUnreadable("unsupported-platform")


def resolve(row: Row, how: str) -> Owner:
    """The owner of one selected row: its uid when the table carried one, else the pid's."""
    if sys.platform == "win32" and how == "iphlpapi":
        return _windows_owner(row.pid, how)
    if row.uid is not None:
        return classify(row.uid, row.pid, method=how)
    if row.pid is None or row.pid <= 0:
        return Owner(UNKNOWN, None, row.pid, how, "no-pid")
    if row.pid == 1:
        return Owner(SYSTEM, None, 1, how)
    try:
        uid = parse_ps_uid(_run([PS, "-o", "uid=", "-p", str(row.pid)]))
    except TableUnreadable as exc:
        return Owner(UNKNOWN, None, row.pid, how, f"ps: {exc}")
    if uid is None:
        return Owner(UNKNOWN, None, row.pid, how, "process-gone")
    return classify(uid, row.pid, method=how)


# -- Windows ----------------------------------------------------------------------------------------------------------

def _windows_table() -> list[Row]:  # pragma: no cover - exercised on Windows only
    from ctypes import wintypes
    iphlpapi = ctypes.WinDLL("iphlpapi")
    fn = iphlpapi.GetExtendedTcpTable
    fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL, wintypes.ULONG,
                   ctypes.c_int, wintypes.ULONG]
    fn.restype = wintypes.DWORD
    rows: list[Row] = []
    for family in (socket.AF_INET, socket.AF_INET6):
        size = wintypes.DWORD(0)
        fn(None, ctypes.byref(size), False, family, 5, 0)            # 5: TCP_TABLE_OWNER_PID_ALL
        for _attempt in range(3):
            if size.value > MAX_TABLE_BYTES:
                raise TableUnreadable(f"iphlpapi: table over {MAX_TABLE_BYTES} bytes")
            buf = ctypes.create_string_buffer(max(size.value, 4))
            code = fn(buf, ctypes.byref(size), False, family, 5, 0)
            if code == 0:
                rows += parse_windows_tcp_table(buf.raw[:size.value], family)
                break
            if code != 122:                                          # ERROR_INSUFFICIENT_BUFFER
                raise TableUnreadable(f"iphlpapi: error {code}")
        else:
            raise TableUnreadable("iphlpapi: the table kept growing")
    return rows


#: Accounts of the system itself: LocalSystem, LocalService, NetworkService.
_SYSTEM_SIDS = frozenset({"S-1-5-18", "S-1-5-19", "S-1-5-20"})


def _windows_sid(pid: int | None) -> str:  # pragma: no cover - exercised on Windows only
    """The account SID of a process (None: this one). Raises OSError when it cannot be read."""
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                             ctypes.POINTER(wintypes.DWORD)]
    advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    if pid is None:
        process, close = kernel32.GetCurrentProcess(), False
    else:
        process = kernel32.OpenProcess(0x1000, False, pid)           # PROCESS_QUERY_LIMITED_INFORMATION
        close = True
        if not process:
            raise OSError(ctypes.get_last_error(), "OpenProcess")
    token = wintypes.HANDLE()
    try:
        if not advapi32.OpenProcessToken(process, 0x0008, ctypes.byref(token)):   # TOKEN_QUERY
            raise OSError(ctypes.get_last_error(), "OpenProcessToken")
        try:
            size = wintypes.DWORD(0)
            advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))   # TokenUser
            buf = ctypes.create_string_buffer(max(size.value, 1))
            if not advapi32.GetTokenInformation(token, 1, buf, size, ctypes.byref(size)):
                raise OSError(ctypes.get_last_error(), "GetTokenInformation")
            sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]            # TOKEN_USER.User.Sid
            text = wintypes.LPWSTR()
            if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)):
                raise OSError(ctypes.get_last_error(), "ConvertSidToStringSidW")
            try:
                return str(text.value)
            finally:
                kernel32.LocalFree(text)
        finally:
            kernel32.CloseHandle(token)
    finally:
        if close:
            kernel32.CloseHandle(process)


def classify_sid(sid: str | None, pid: int | None, *, own_sid: str, method: str = "iphlpapi") -> Owner:
    """Windows: the System process and the system's own accounts are `system`, this process's
    SID `this-account`, any other SID `another-account`, an unreadable one `unknown`."""
    if pid in (0, 4):
        return Owner(SYSTEM, None, pid, method)
    if sid is None:
        return Owner(UNKNOWN, None, pid, method, "process-not-readable")
    if sid in _SYSTEM_SIDS:
        return Owner(SYSTEM, None, pid, method)
    if sid == own_sid:
        return Owner(THIS_ACCOUNT, None, pid, method)
    return Owner(ANOTHER_ACCOUNT, None, pid, method)


def _windows_owner(pid: int | None, how: str) -> Owner:  # pragma: no cover - exercised on Windows only
    try:
        own = _windows_sid(None)
    except OSError as exc:
        return Owner(UNKNOWN, None, pid, how, f"own-sid: {exc.args[-1] if exc.args else type(exc).__name__}")
    if pid in (0, 4):
        return classify_sid(None, pid, own_sid=own, method=how)
    try:
        sid = _windows_sid(pid)
    except OSError:
        sid = None
    return classify_sid(sid, pid, own_sid=own, method=how)


# -- the questions ----------------------------------------------------------------------------------------------------

def find(rows: list[Row], local: tuple[str, int], remote: tuple[str, int]) -> list[Row]:
    """The live connected rows with exactly this local and remote address: one, or the caller
    cannot tell. A TIME_WAIT row is a connection already closed and is left out."""
    return [row for row in rows
            if not row.listening and not row.time_wait and row.local == local and row.remote == remote]


def _owner_of(local: tuple[str, int], remote: tuple[str, int]) -> Owner:
    try:
        rows, how = read_table()
    except TableUnreadable as exc:
        return Owner(UNKNOWN, None, None, method(), str(exc))
    found = find(rows, local, remote)
    if not found:
        return Owner(UNKNOWN, None, None, how, "no-row")
    if len(found) > 1:
        return Owner(UNKNOWN, None, None, how, "several-rows")
    return resolve(found[0], how)


def peer_at(server: tuple, client: tuple) -> Owner:
    """The owner of the CLIENT socket of the connection between `server` and `client`, as a
    server sees them (its own address, the peer's address): the row whose local address is the
    client's and whose remote address is the server's."""
    return _owner_of(_address(client), _address(server))


def peer(sock: socket.socket) -> Owner:
    """For a server's accepted socket: who connected. Reads nothing from `sock`."""
    try:
        server, client = sock.getsockname(), sock.getpeername()
    except OSError as exc:
        return Owner(UNKNOWN, None, None, "none", f"socket: {type(exc).__name__}")
    return peer_at(server, client)


def server_end(sock: socket.socket) -> Owner:
    """For a client's connected socket: who accepted it. The row whose local address is the
    server's and whose remote address is this socket's own, which is the server process's
    accepted socket, so a listener swapped after `listen` cannot pass for the one that answers."""
    try:
        own, server = sock.getsockname(), sock.getpeername()
    except OSError as exc:
        return Owner(UNKNOWN, None, None, "none", f"socket: {type(exc).__name__}")
    return _owner_of(_address(server), _address(own))


#: port -> (time.monotonic() of the read, the Owner)
_LISTENERS: dict[int, tuple[float, Owner]] = {}

#: The binds that shadow a connection to 127.0.0.1, most specific first.
_LISTEN_ORDER = ("127.0.0.1", "*", "::1")


def pick_listener(rows: list[Row], port: int) -> list[Row]:
    """The listeners a connection to 127.0.0.1:<port> reaches: those on the most specific bind
    that has any. More than one (a port reused by two sockets) cannot be told apart."""
    for host in _LISTEN_ORDER:
        hits = [row for row in rows if row.listening and row.local == (host, port)]
        if hits:
            return hits
    return []


def listener(port: int) -> Owner:
    """Who listens on 127.0.0.1:<port>, or on 0.0.0.0 or ::1, which take the same connections.
    `none` when nothing does, `unknown` (`several-rows`) when two sockets do. Cached for
    LISTENER_TTL seconds."""
    now = time.monotonic()
    hit = _LISTENERS.get(port)
    if hit is not None and now - hit[0] < LISTENER_TTL:
        return hit[1]
    try:
        rows, how = read_table()
    except TableUnreadable as exc:
        owner = Owner(UNKNOWN, None, None, method(), str(exc))
    else:
        found = pick_listener(rows, port)
        if not found:
            owner = Owner(NONE, None, None, how)
        elif len(found) > 1:
            owner = Owner(UNKNOWN, None, None, how, "several-rows")
        else:
            owner = resolve(found[0], how)
    _LISTENERS[port] = (now, owner)
    return owner


def refusal(owner: Owner, port: int, expect_pid: int | None = None) -> tuple[str, str] | None:
    """(code, why) when a client must not send on a connection whose server end is `owner`."""
    if owner.kind in (ANOTHER_ACCOUNT, SYSTEM):
        return SERVER_NOT_OWN, f"the server on port {port} belongs to {owner.kind}"
    if owner.kind != THIS_ACCOUNT:
        return SERVER_UNVERIFIED, f"the owner of the server on port {port} could not be read ({owner.reason})"
    if expect_pid is not None and owner.pid is not None and owner.pid != expect_pid:
        return SERVER_NOT_THIS_WORKSPACE, (f"the server on port {port} is pid {owner.pid}, "
                                           f"not the expected pid {expect_pid}")
    return None


class CheckedHTTPConnection(http.client.HTTPConnection):
    """An `http.client.HTTPConnection` whose every `connect()` checks the server end before
    returning. `http.client` connects again by itself (`auto_open`) when the server answered
    `Connection: close` or the caller closed the connection, and that new connection may reach a
    different listener; so the check is in `connect()`, not once in `own_connection`. A refused
    connect closes the socket and raises ListenerRefused before any byte of a request is sent."""

    def __init__(self, host: str, port: int, *, expect_pid: int | None = None, timeout: float = 10.0) -> None:
        super().__init__(host, port, timeout=timeout)
        self.expect_pid = expect_pid

    def set_tunnel(self, *args, **kwargs):  # noqa: D401 - a tunnel would put a proxy's socket under the check
        raise ListenerRefused(SERVER_UNVERIFIED, "a tunnel would check the proxy, not the server; nothing was sent")

    def connect(self) -> None:
        super().connect()
        owner = server_end(self.sock)
        refused = refusal(owner, self.port, self.expect_pid)
        if refused is not None:
            self.close()
            raise ListenerRefused(refused[0], f"{refused[1]}; nothing was sent", owner)


def own_connection(port: int, *, expect_pid: int | None = None, timeout: float = 10.0,
                   host: str = "127.0.0.1") -> CheckedHTTPConnection:
    """A `CheckedHTTPConnection` to `host:port`, connected, whose server end is this account's
    (and, with `expect_pid`, that process's). The check runs on the very connection the caller
    then sends on, so no listener can change between the check and the request, and it runs
    again on every reconnect `http.client` makes. No proxy is consulted. Raises ListenerRefused
    after closing the connection:

        server-not-own             another account's or the system's process accepted it
        server-unverified          its owner could not be told (nothing is sent: fail closed)
        server-not-this-workspace  this account's process, but not the pid expected
    """
    conn = CheckedHTTPConnection(host, port, expect_pid=expect_pid, timeout=timeout)
    conn.connect()
    return conn
