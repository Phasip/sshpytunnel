#!/usr/bin/env python
"""
sshpytunnel – SOCKS5 proxy tunnelled over SSH stdin/stdout

No TCP forwarding or AllowTcpForwarding required on the server.
Requirements:
  client : Python 2.7+ or Python 3, the 'ssh' binary
  server : Python 2 or Python 3, no extra packages

Usage:
  python3 sshpytunnel.py [--port 1080] [--bind 127.0.0.1] \\
                         [ssh-options ...] user@host

  Then point your browser / curl at socks5://127.0.0.1:1080

How it works:
  1. build.py embeds serverside.py as a raw string into this file.
     At launch the client passes it to 'python -u -c' on the remote side
     via SSH stdin/stdout, which remain free for the binary data channel.
  2. A simple binary framing protocol multiplexes N TCP connections over the
     single SSH stdio pipe:
       [4-byte channel-id BE][4-byte length BE][<length> bytes]
     Channel 0 carries JSON control messages; channels 1-N carry raw TCP data.
  3. The client runs a SOCKS5 server locally.  For each CONNECT the server
     script opens a TCP socket on the remote host and streams data back.
"""

from __future__ import print_function
import sys
import os
import socket
import struct
import threading
import subprocess
import json
import argparse
try:
    import queue
except ImportError:
    import Queue as queue
import logging
import signal

PY3 = sys.version_info[0] >= 3

_SERVER_CODE = "@@SERVER_CODE@@"  # injected by build.py

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("sshpytunnel")

_FRAME_HDR   = struct.Struct("!II")
CTRL_CHAN     = 0
_FRAME_HDR_SZ = _FRAME_HDR.size



def _make_frame(chan_id, data):
    return _FRAME_HDR.pack(chan_id, len(data)) + data


class Channel(object):
    """Represents one multiplexed TCP connection."""

    def __init__(self, chan_id):
        self.chan_id     = chan_id
        self.queue       = queue.Queue()
        self.open_event  = threading.Event()
        self.open_ok     = False
        self.open_error  = ""
        self.closed      = False


class Mux(object):
    def __init__(self):
        self.proc        = None
        self._lock       = threading.Lock()
        self._channels   = {}
        self._next_id    = 1
        self._stdin_lock = threading.Lock()
        self.running     = False

    def start(self, ssh_argv, srv_py="python3"):
        """
        Spawn the SSH subprocess and start the reader thread.

        *ssh_argv* is the argv prefix up to (but not including) the remote
        command, e.g. ``["ssh", "-T", "user@host"]``.
        Pass ``None`` to run the server locally via the current Python
        interpreter (used by ``--test`` mode on Windows).

        *srv_py* is the Python interpreter name (or path) used on the remote
        side, e.g. ``"python3"`` (default) or ``"python"``.

        The server script is delivered over stdin so that the remote process
        shows up cleanly in ``ps`` as something like::

            python3 -c 'print("sshpytunnel"); exec(sys.stdin.buffer.read(N))'

        The exact byte count *N* is baked into the command at launch time, so
        no length-prefix handshake is needed; ``exec`` consumes exactly that
        many bytes and the rest of stdin is available for the mux protocol.
        """
        raw_code = _SERVER_CODE.encode("utf-8") if PY3 else _SERVER_CODE
        n = len(raw_code)
        # The byte count is known ahead of time, so bake it into the command.
        # ps output will show:  python3 -u -c 'print("sshpytunnel"); exec(...read(N))'
        # -u forces unbuffered stdout so the banner line arrives immediately.
        # Use double quotes for string literals so the whole thing can safely
        # sit inside single quotes on the remote shell command line.
        _bootstrap = (
            'print("sshpytunnel");'
            'import sys;'
            'exec(getattr(sys.stdin,"buffer",sys.stdin).read({n}))'
        ).format(n=n)

        if ssh_argv is None:
            # Windows / --test: no 'sh', run server via the current interpreter.
            cmd = [sys.executable, "-u", "-c", _bootstrap]
        else:
            remote_cmd = "{py} -u -c '{b}'".format(py=srv_py, b=_bootstrap)
            cmd = ssh_argv + [remote_cmd]
        log.debug("SSH command: %s ...", " ".join(cmd[:6]))
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )
        # Stream the server script over stdin; the remote bootstrap exec()s it
        # and the rest of stdin becomes the mux binary channel.
        self.proc.stdin.write(raw_code)
        self.proc.stdin.flush()

        # The bootstrap prints "sshpytunnel\n" before exec()-ing the server.
        # Read exactly that line to confirm the remote interpreter started
        # correctly.  readline() blocks until newline or EOF.
        banner = self.proc.stdout.readline().rstrip(b"\r\n")
        if banner != b"sshpytunnel":
            self.proc.terminate()
            raise RuntimeError(
                "Server did not start: expected banner 'sshpytunnel', "
                "got {0!r}".format(banner)
            )
        log.info("Server ready (banner OK)")

        self.running = True
        t = threading.Thread(target=self._reader, name="mux-reader")
        t.daemon = True
        t.start()

    def stop(self):
        self.running = False
        if self.proc:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
            try:
                self.proc.terminate()
            except OSError:
                pass
        with self._lock:
            for ch in self._channels.values():
                ch.closed = True
                ch.open_event.set()
                ch.queue.put(None)


    def _alloc_channel(self):
        with self._lock:
            chan_id = self._next_id
            self._next_id += 1
            ch = Channel(chan_id)
            self._channels[chan_id] = ch
        return ch

    def open_channel(self, host, port):
        """Ask the server to CONNECT to (host, port).  Blocks until replied."""
        ch = self._alloc_channel()
        self._send_ctrl({"cmd": "open", "id": ch.chan_id,
                         "host": host, "port": port})
        ch.open_event.wait(timeout=30)
        if not ch.open_ok:
            with self._lock:
                self._channels.pop(ch.chan_id, None)
            raise RuntimeError(ch.open_error or "open timed out")
        return ch

    def send_data(self, chan_id, data):
        self._send_raw(_make_frame(chan_id, data))

    def close_channel(self, chan_id):
        with self._lock:
            self._channels.pop(chan_id, None)
        self._send_ctrl({"cmd": "close", "id": chan_id})

    def _send_ctrl(self, obj):
        raw = json.dumps(obj)
        if PY3:
            raw = raw.encode("utf-8")
        self._send_raw(_make_frame(CTRL_CHAN, raw))

    def _send_raw(self, data):
        if not self.running or not self.proc:
            return
        try:
            with self._stdin_lock:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
        except (OSError, IOError):
            self.running = False

    def _reader(self):
        fp = self.proc.stdout
        try:
            while self.running:
                hdr = self._read_exactly(fp, _FRAME_HDR_SZ)
                if hdr is None:
                    break
                chan_id, length = _FRAME_HDR.unpack(hdr)
                data = self._read_exactly(fp, length) if length else b""
                if data is None:
                    break
                if chan_id == CTRL_CHAN:
                    self._dispatch_ctrl(data)
                else:
                    self._dispatch_data(chan_id, data)
        except Exception as exc:
            log.debug("mux reader exiting: %s", exc)
        finally:
            self.running = False
            with self._lock:
                channels = list(self._channels.values())
            for ch in channels:
                ch.closed = True
                ch.open_event.set()
                ch.queue.put(None)
            log.info("SSH connection closed")

    @staticmethod
    def _read_exactly(fp, n):
        buf = b""
        while len(buf) < n:
            chunk = fp.read(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    def _dispatch_ctrl(self, raw):
        try:
            text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
            msg = json.loads(text)
        except (ValueError, UnicodeDecodeError):
            return
        cmd    = msg.get("cmd")
        chan_id = msg.get("id")
        with self._lock:
            ch = self._channels.get(chan_id)
        if cmd == "result" and ch:
            ch.open_ok    = bool(msg.get("ok"))
            ch.open_error = msg.get("error", "")
            ch.open_event.set()
        elif cmd == "closed" and ch:
            ch.closed = True
            ch.queue.put(None)

    def _dispatch_data(self, chan_id, data):
        with self._lock:
            ch = self._channels.get(chan_id)
        if ch:
            if data:
                ch.queue.put(data)
            else:
                ch.closed = True
                ch.queue.put(None)


_SOCKS5_VERSION = 5
_SOCKS5_NO_AUTH = 0
_ATYP_IPV4      = 1
_ATYP_DOMAIN    = 3
_ATYP_IPV6      = 4


def _recv_exactly(sock, n):
    buf = b""
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except (OSError, socket.error):
            return None
        if not chunk:
            return None
        buf += chunk
    return buf


def _socks5_handshake(sock):
    """
    Perform the SOCKS5 handshake and CONNECT negotiation.

    Returns ``(host, port)`` on success, ``None`` on failure.
    Byte sequences are wrapped in ``bytearray()`` so that indexing yields
    integers in both Python 2 and Python 3.
    """
    hdr = _recv_exactly(sock, 2)
    if not hdr:
        return None
    hdr = bytearray(hdr)
    if hdr[0] != _SOCKS5_VERSION:
        return None
    methods = _recv_exactly(sock, hdr[1])
    if methods is None or _SOCKS5_NO_AUTH not in bytearray(methods):
        sock.sendall(struct.pack("BB", _SOCKS5_VERSION, 0xFF))
        return None
    sock.sendall(struct.pack("BB", _SOCKS5_VERSION, _SOCKS5_NO_AUTH))

    req = _recv_exactly(sock, 4)
    if not req:
        return None
    req = bytearray(req)
    if req[0] != _SOCKS5_VERSION or req[1] != 1:
        return None
    atyp = req[3]

    if atyp == _ATYP_IPV4:
        raw = _recv_exactly(sock, 4)
        if raw is None:
            return None
        host = socket.inet_ntoa(raw)
    elif atyp == _ATYP_DOMAIN:
        nlen = _recv_exactly(sock, 1)
        if nlen is None:
            return None
        domain = _recv_exactly(sock, bytearray(nlen)[0])
        if domain is None:
            return None
        host = domain.decode("utf-8", errors="replace")
    elif atyp == _ATYP_IPV6:
        raw = _recv_exactly(sock, 16)
        if raw is None:
            return None
        try:
            host = socket.inet_ntop(socket.AF_INET6, raw)
        except AttributeError:
            # Python 2 on Windows lacks inet_ntop.
            import binascii
            h = binascii.hexlify(raw).decode("ascii")
            host = ":".join(h[i:i+4] for i in range(0, 32, 4))
    else:
        return None

    port_raw = _recv_exactly(sock, 2)
    if port_raw is None:
        return None
    return host, struct.unpack("!H", port_raw)[0]


def _handle_socks5_client(client_sock, client_addr, mux):
    """Handle one SOCKS5 client connection end-to-end."""
    try:
        result = _socks5_handshake(client_sock)
        if result is None:
            return
        host, port = result
        log.info("CONNECT %s:%d  from %s:%d", host, port,
                 client_addr[0], client_addr[1])

        try:
            ch = mux.open_channel(host, port)
        except RuntimeError as e:
            client_sock.sendall(b"\x05\x04\x00\x01\x00\x00\x00\x00\x00\x00")
            log.warning("CONNECT %s:%d failed: %s", host, port, e)
            return

        client_sock.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")  # success, bind 0.0.0.0:0

        def sock_to_mux():
            try:
                while True:
                    try:
                        data = client_sock.recv(65536)
                    except OSError:
                        break
                    if not data or not mux.running:
                        break
                    mux.send_data(ch.chan_id, data)
            finally:
                mux.close_channel(ch.chan_id)
                try:
                    client_sock.shutdown(socket.SHUT_RD)
                except OSError:
                    pass

        _t = threading.Thread(target=sock_to_mux,
                              name="s2m-{0}".format(ch.chan_id))
        _t.daemon = True
        _t.start()

        try:
            while True:
                item = ch.queue.get()
                if item is None:
                    break
                try:
                    client_sock.sendall(item)
                except OSError:
                    break
        finally:
            try:
                client_sock.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    except Exception as exc:
        log.debug("socks5 handler error: %s", exc)
    finally:
        try:
            client_sock.close()
        except OSError:
            pass


class Socks5Server(object):
    """TCP server that accepts SOCKS5 connections and forwards them over *mux*."""

    def __init__(self, bind_addr, bind_port, mux):
        self.bind_addr    = bind_addr
        self.bind_port    = bind_port
        self.mux          = mux
        self._server_sock = None

    def start(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.bind_addr, self.bind_port))
        s.listen(128)
        self._server_sock = s
        log.info("SOCKS5 proxy listening on %s:%d", self.bind_addr, self.bind_port)
        _t = threading.Thread(target=self._accept_loop, name="socks5-accept")
        _t.daemon = True
        _t.start()

    def _accept_loop(self):
        while True:
            try:
                client_sock, addr = self._server_sock.accept()
            except OSError:
                break
            _t = threading.Thread(
                target=_handle_socks5_client,
                args=(client_sock, addr, self.mux),
                name="socks5-{0}".format(addr[1]),
            )
            _t.daemon = True
            _t.start()

    def stop(self):
        if self._server_sock:
            try:
                self._server_sock.close()
            except OSError:
                pass


def _parse_args():
    """Parse command-line arguments and return ``(args, ssh_argv)``."""
    parser = argparse.ArgumentParser(
        description="SOCKS5 proxy tunnelled over SSH stdin/stdout",
    )
    parser.add_argument("--port", type=int, default=1080, metavar="PORT",
                        help="local SOCKS5 listen port (default: 1080)")
    parser.add_argument("--bind", default="127.0.0.1", metavar="ADDR",
                        help="local bind address (default: 127.0.0.1)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="enable debug logging")
    parser.add_argument("--srvpycmd", default="python3", metavar="CMD",
                        help="Python interpreter to invoke on the server (default: python3)")
    parser.add_argument("--ssh", default="ssh", metavar="PROG",
                        help="SSH program to use (default: ssh, e.g. plink)")
    parser.add_argument("--test", action="store_true",
                        help="run the server locally (no SSH) for testing")
    parser.add_argument("ssh_args", nargs=argparse.REMAINDER,
                        help="ssh arguments: [options ...] [user@]host")
    args = parser.parse_args()
    if args.test:
        if args.ssh_args:
            parser.error("--test mode does not accept SSH arguments.")
        if sys.platform == "win32":
            ssh_argv = None
        else:
            ssh_argv = ["sh", "-c"]
    else:
        if not args.ssh_args:
            parser.error("No SSH destination specified.")
        ssh_argv = [args.ssh, "-T"] + args.ssh_args
    return args, ssh_argv


def main():
    """Entry point: parse arguments, start the mux and SOCKS5 server."""
    args, ssh_argv = _parse_args()
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    mux   = Mux()
    socks = Socks5Server(args.bind, args.port, mux)

    def _shutdown(signum, frame):
        log.info("Shutting down ...")
        socks.stop()
        mux.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _shutdown)

    mux.start(ssh_argv, srv_py=args.srvpycmd)
    socks.start()
    mux.proc.wait()
    log.info("SSH process exited.")
    socks.stop()


if __name__ == "__main__":
    main()
