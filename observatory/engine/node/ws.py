"""A WebSocket client (RFC 6455) on the standard library alone: what a node needs to hold one relay connection.

Text frames only, client-masked; a ping is answered with a pong; a close is answered and raised as `Closed` with the
peer's code. Messages are limited to the relay's 1 MiB frame. TLS uses the platform's default context (certificate
and host name verified); plain `ws://` is accepted only on a loopback literal, as the relay origin rule allows.
"""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import os
import socket
import ssl
import struct
import threading
import time
from typing import Optional
from urllib.parse import urlsplit

GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
MAX_MESSAGE_BYTES = 1_048_576
MAX_HANDSHAKE_BYTES = 16_384
#: Fragments one message may arrive in; empty continuation frames would otherwise never reach the byte limit.
MAX_FRAGMENTS = 1024
#: Close codes a peer may send (RFC 6455 §7.4): 1005, 1006 and 1015 are never on the wire.
_SENDABLE = frozenset({1000, 1001, 1002, 1003, 1007, 1008, 1009, 1010, 1011, 1012, 1013, 1014})
OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class WebSocketError(Exception):
    """The connection broke the protocol, or could not be opened."""


class Closed(Exception):
    """The peer closed the connection; `code` is its close code (1005 when it sent none)."""

    def __init__(self, code: int, reason: str = ''):
        super().__init__(f'closed {code}{": " + reason if reason else ""}')
        self.code = code
        self.reason = reason


def _loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host.strip('[]')).is_loopback
    except ValueError:
        return False


class WebSocket:
    def __init__(self, sock: socket.socket):
        self._sock = sock
        self._buffer = b''
        self._send_lock = threading.Lock()
        self.closed = False

    @classmethod
    def connect(cls, url: str, *, subprotocol: str, timeout: float = 15.0,
                context: Optional[ssl.SSLContext] = None) -> 'WebSocket':
        parts = urlsplit(url)
        if parts.scheme not in ('ws', 'wss') or not parts.hostname:
            raise WebSocketError('a WebSocket URL is ws:// or wss://')
        host = parts.hostname
        if parts.scheme == 'ws' and not _loopback(host):
            raise WebSocketError('plain ws:// is only for a relay on this machine (a loopback literal)')
        port = parts.port or (443 if parts.scheme == 'wss' else 80)
        raw = socket.create_connection((host, port), timeout=timeout)
        try:
            if parts.scheme == 'wss':
                raw = (context or ssl.create_default_context()).wrap_socket(raw, server_hostname=host)
            ws = cls(raw)
            ws._handshake(parts, host, port, subprotocol)
            return ws
        except BaseException:
            raw.close()
            raise

    def _handshake(self, parts, host: str, port: int, subprotocol: str) -> None:
        key = base64.b64encode(os.urandom(16)).decode('ascii')
        default = 443 if parts.scheme == 'wss' else 80
        authority = (f'[{host}]' if ':' in host else host) + ('' if port == default else f':{port}')
        path = (parts.path or '/') + (f'?{parts.query}' if parts.query else '')
        request = (f'GET {path} HTTP/1.1\r\nHost: {authority}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                   f'Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n'
                   f'Sec-WebSocket-Protocol: {subprotocol}\r\n\r\n')
        self._sock.sendall(request.encode('ascii'))
        while b'\r\n\r\n' not in self._buffer:
            if len(self._buffer) > MAX_HANDSHAKE_BYTES:
                raise WebSocketError('the upgrade answer is too long')
            chunk = self._sock.recv(4096)
            if not chunk:
                raise WebSocketError('the server closed during the upgrade')
            self._buffer += chunk
        head, self._buffer = self._buffer.split(b'\r\n\r\n', 1)
        lines = head.decode('iso-8859-1').split('\r\n')
        status = lines[0].split(' ', 2)
        if len(status) < 2 or status[1] != '101':
            raise WebSocketError('the server refused the upgrade: ' + (status[1] if len(status) > 1 else lines[0][:40]))
        headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(':')
            headers[name.strip().lower()] = value.strip()
        expected = base64.b64encode(hashlib.sha1((key + GUID).encode('ascii')).digest()).decode('ascii')
        if headers.get('sec-websocket-accept') != expected:
            raise WebSocketError('the upgrade answer does not prove the key')
        if headers.get('upgrade', '').lower() != 'websocket' or 'upgrade' not in headers.get('connection', '').lower():
            raise WebSocketError('the server did not upgrade to websocket')
        if headers.get('sec-websocket-protocol') != subprotocol:
            raise WebSocketError('the server did not agree to ' + subprotocol)

    # ---- Frames --------------------------------------------------------------------------------------------------

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            header = struct.pack('!BB', 0x80 | opcode, 0x80 | n)
        elif n < 65536:
            header = struct.pack('!BBH', 0x80 | opcode, 0x80 | 126, n)
        else:
            header = struct.pack('!BBQ', 0x80 | opcode, 0x80 | 127, n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload)) if n < 4096 else _xor(payload, mask)
        with self._send_lock:
            self._sock.sendall(header + mask + masked)

    def _read(self, n: int) -> bytes:
        while len(self._buffer) < n:
            chunk = self._sock.recv(max(4096, n - len(self._buffer)))
            if not chunk:
                self.closed = True
                raise Closed(1006, 'the connection dropped')
            self._buffer += chunk
        data, self._buffer = self._buffer[:n], self._buffer[n:]
        return data

    def _frame(self):
        first, second = self._read(2)
        fin, opcode = first & 0x80, first & 0x0F
        if first & 0x70:
            raise WebSocketError('reserved bits set')
        if second & 0x80:
            raise WebSocketError('a server frame is never masked')
        n = second & 0x7F
        if n == 126:
            n = struct.unpack('!H', self._read(2))[0]
        elif n == 127:
            n = struct.unpack('!Q', self._read(8))[0]
        if opcode >= 0x8 and (n > 125 or not fin):
            raise WebSocketError('a control frame is short and whole')
        if n > MAX_MESSAGE_BYTES:
            raise WebSocketError('a frame over 1 MiB')
        return bool(fin), opcode, self._read(n)

    def send_text(self, text: str) -> None:
        data = text.encode('utf-8')
        if len(data) > MAX_MESSAGE_BYTES:
            raise WebSocketError('a message over 1 MiB')
        self._send_frame(OP_TEXT, data)

    def _answer_close(self, code: Optional[int]) -> None:
        """Answers a peer's close once: its code echoed when it may be sent, else an empty close."""
        if self.closed:
            return
        self.closed = True
        try:
            self._send_frame(OP_CLOSE, struct.pack('!H', code) if code in _SENDABLE or (code and 3000 <= code <= 4999)
                             else b'')
        except OSError:
            pass

    def recv_text(self, deadline: Optional[float] = None) -> str:
        """The next text message; answers pings, raises `Closed` on a close and `WebSocketError` on a binary message,
        an unknown opcode, a message over 1 MiB or `MAX_FRAGMENTS` frames, or text that is not UTF-8."""
        parts, opcode, total = [], None, 0
        while True:
            if deadline is not None:
                # One limit over the whole wait (a peer that keeps pinging cannot hold it open), `time.monotonic()`.
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise socket.timeout('no message before the deadline')
                self._sock.settimeout(remaining)
            fin, op, payload = self._frame()
            if op == OP_PING:
                self._send_frame(OP_PONG, payload)
                continue
            if op == OP_PONG:
                continue
            if op == OP_CLOSE:
                if len(payload) == 1:                       # a status code is two bytes or absent
                    self._answer_close(1002)
                    raise Closed(1002, 'a one-byte close payload')
                code = struct.unpack('!H', payload[:2])[0] if len(payload) >= 2 else 1005
                try:
                    reason = payload[2:].decode('utf-8')
                except UnicodeDecodeError:
                    self._answer_close(1007)
                    raise Closed(1007, 'a close reason that is not UTF-8') from None
                self._answer_close(code if len(payload) >= 2 else None)
                raise Closed(code, reason)
            if op == OP_BINARY:
                raise WebSocketError('a binary message: the relay speaks text frames only')
            if op not in (OP_TEXT, OP_CONT):
                raise WebSocketError('an unknown opcode %d' % op)
            if op == OP_TEXT and opcode is None:
                opcode = op
            elif op != OP_CONT or opcode is None:
                raise WebSocketError('a fragment out of order')
            parts.append(payload)
            total += len(payload)
            if total > MAX_MESSAGE_BYTES:
                raise WebSocketError('a message over 1 MiB')
            if len(parts) > MAX_FRAGMENTS:
                raise WebSocketError('a message in more than %d fragments' % MAX_FRAGMENTS)
            if fin:
                try:
                    return b''.join(parts).decode('utf-8')
                except UnicodeDecodeError as error:
                    raise WebSocketError('a text message that is not UTF-8') from error

    def settimeout(self, seconds: Optional[float]) -> None:
        self._sock.settimeout(seconds)

    def abort(self, code: int = 1000) -> None:
        """Ends the connection from another thread than the reader: a close frame, then `shutdown`, which wakes a
        reader blocked in `recv`. The descriptor stays open for that reader to close: closing it here races the
        pending `recv`, which would then wait out its whole timeout on a closed descriptor."""
        if not self.closed:
            self.closed = True
            try:
                self._send_frame(OP_CLOSE, struct.pack('!H', code))
            except OSError:
                pass
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def close(self, code: int = 1000) -> None:
        """Ends the connection and frees the descriptor; the reader's own thread calls this."""
        self.abort(code)
        try:
            self._sock.close()
        except OSError:
            pass


def _xor(payload: bytes, mask: bytes) -> bytes:
    """Masks a long payload in one integer operation instead of byte by byte."""
    n = len(payload)
    key = int.from_bytes((mask * (n // 4 + 1))[:n], 'big')
    return (int.from_bytes(payload, 'big') ^ key).to_bytes(n, 'big')
