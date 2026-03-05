# Server-side agent – Python 2 / Python 3 compatible, no extra dependencies.
# Launched by the client via:
#   python3 -c 'import base64,zlib;exec(...)' || python -c '...'
# stdin/stdout are the binary data channel; stderr is left for error messages.
from __future__ import print_function
import sys
import os
import socket
import struct
import threading
import json

PY3 = sys.version_info[0] >= 3

# ── binary stdin / stdout ─────────────────────────────────────────────────
if PY3:
    _stdin  = sys.stdin.buffer
    _stdout = sys.stdout.buffer
else:
    if sys.platform == "win32":
        import msvcrt
        msvcrt.setmode(sys.stdin.fileno(),  os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
    _stdin  = sys.stdin
    _stdout = sys.stdout

_write_lock = threading.Lock()
CTRL_CHAN    = 0

def _read_exactly(n):
    buf = b""
    while len(buf) < n:
        chunk = _stdin.read(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf

def _read_frame():
    hdr = _read_exactly(8)
    if hdr is None:
        return None, None
    chan_id, length = struct.unpack("!II", hdr)
    if length == 0:
        return chan_id, b""
    data = _read_exactly(length)
    if data is None:
        return None, None
    return chan_id, data

def _send_frame(chan_id, data):
    '''Write a framed packet to stdout.'''
    pkt = struct.pack("!II", chan_id, len(data)) + data
    with _write_lock:
        _stdout.write(pkt)
        _stdout.flush()

def _send_ctrl(obj):
    '''Encode *obj* as JSON and send it on the control channel.'''
    raw = json.dumps(obj)
    if PY3:
        raw = raw.encode("utf-8")
    _send_frame(CTRL_CHAN, raw)

# ── per-connection state ──────────────────────────────────────────────────
_conns      = {}   # chan_id -> socket
_conns_lock = threading.Lock()

def _conn_reader(chan_id, sock):
    '''Read from a remote TCP socket and forward data frames to the client.'''
    try:
        while True:
            try:
                chunk = sock.recv(65536)
            except (OSError, socket.error):
                break
            if not chunk:
                break
            _send_frame(chan_id, chunk)
    finally:
        with _conns_lock:
            _conns.pop(chan_id, None)
        try:
            sock.close()
        except OSError:
            pass
        _send_frame(chan_id, b"")  # EOF

def _handle_open(chan_id, host, port):
    '''Open a TCP connection on behalf of the client and start its reader thread.'''
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(15)
        sock.connect((host, int(port)))
        sock.settimeout(None)
        with _conns_lock:
            _conns[chan_id] = sock
        _send_ctrl({"cmd": "result", "id": chan_id, "ok": True})
        t = threading.Thread(target=_conn_reader, args=(chan_id, sock))
        t.daemon = True
        t.start()
    except (OSError, socket.error) as e:
        if sock is not None:
            try:
                sock.close()
            except (OSError, socket.error):
                pass
        _send_ctrl({"cmd": "result", "id": chan_id, "ok": False,
                    "error": str(e)})

def _main():
    '''Dispatch loop: read frames from stdin and act on control or data channels.'''
    while True:
        chan_id, data = _read_frame()
        if chan_id is None:
            break

        if chan_id == CTRL_CHAN:
            try:
                text = data.decode("utf-8") if isinstance(data, bytes) else data
                msg  = json.loads(text)
            except (ValueError, UnicodeDecodeError):
                continue
            cmd = msg.get("cmd")
            if cmd == "open":
                t = threading.Thread(target=_handle_open,
                                     args=(msg["id"], msg["host"], msg["port"]))
                t.daemon = True
                t.start()
            elif cmd == "close":
                with _conns_lock:
                    sock = _conns.pop(msg["id"], None)
                if sock:
                    try:
                        sock.close()
                    except OSError:
                        pass
        else:
            with _conns_lock:
                sock = _conns.get(chan_id)
            if sock:
                if data:
                    try:
                        sock.sendall(data)
                    except (OSError, socket.error):
                        with _conns_lock:
                            _conns.pop(chan_id, None)
                        try:
                            sock.close()
                        except (OSError, socket.error):
                            pass
                else:
                    # client EOF, half-close
                    with _conns_lock:
                        _conns.pop(chan_id, None)
                    try:
                        sock.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass

_main()
