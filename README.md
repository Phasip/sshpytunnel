# sshpytunnel

A SOCKS5 proxy tunnelled over a plain SSH connection — no special server
configuration required.

Works even when the server has disabled TCP forwarding
(`AllowTcpForwarding no`) and tunnelling (`PermitTunnel no`).  The only
channel used is the SSH session's stdin/stdout, which is always available.

---

## Requirements

| Side   | Requirement |
|--------|-------------|
| Client | Python 2.7+ or Python 3, the `ssh` binary |
| Server | Python 3 **or** Python 2.6+ — no extra packages |

No third-party Python packages are needed anywhere.

---

## Quick start

```bash
# 1. Build the single-file output
make

# 2. Start the proxy
python3 out/sshpytunnel.py --port 1080 user@yourserver.com

# 3. In another terminal, use the proxy
curl --socks5 127.0.0.1:1080 https://example.com
```

Point any application that supports SOCKS5 at `127.0.0.1:1080`.

---

## Usage

```
python3 out/sshpytunnel.py [OPTIONS] [SSH-OPTIONS] user@host
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--port PORT` | `1080` | Local SOCKS5 listen port |
| `--bind ADDR` | `127.0.0.1` | Local bind address |
| `--verbose` / `-v` | off | Enable debug logging |
| `--srvpycmd CMD` | `python3` | Python interpreter to invoke on the server (use `python` for Python 2 targets) |
| `--ssh PROG` | `ssh` | SSH binary to use (e.g. `plink` on Windows) |
| `--test` | off | Run server locally — no SSH, for testing |

Any arguments after the `sshpytunnel` options are passed verbatim to `ssh`,
so you can use every SSH flag you know:

```bash
# Non-standard SSH port
python3 out/sshpytunnel.py -p 2222 user@host

# Specific identity file
python3 out/sshpytunnel.py -i ~/.ssh/id_ed25519 user@host

# Jump host
python3 out/sshpytunnel.py -J jumpuser@jumphost user@host

# Bind SOCKS5 on all interfaces (e.g. to share with a VM)
python3 out/sshpytunnel.py --bind 0.0.0.0 --port 1080 user@host
```

### Test mode

Runs the server script locally through a shell — no SSH connection made.
Useful for verifying that everything works before involving a remote host:

```bash
python3 out/sshpytunnel.py --test --verbose
# then in another terminal:
curl --socks5 127.0.0.1:1080 https://example.com
```

---

## How it works

```
┌─────────────┐   SOCKS5   ┌──────────────────────────────────────┐
│  Browser /  │ ─────────► │  sshpytunnel.py  (Python 3, client)  │
│  curl / app │            │                                       │
└─────────────┘            │  ┌───────┐   binary frames   ┌─────┐ │
                           │  │ SOCKS5│ ◄───────────────► │ Mux │ │
                           │  │ server│    over a Queue    │     │ │──── ssh ──►
                           │  └───────┘                   └─────┘ │    stdin/
                           └──────────────────────────────────────┘   stdout
                                                                          │
                                                          ┌───────────────┘
                                                          ▼
                                              ┌──────────────────────┐
                                              │  serverside script   │
                                              │  (Python 2/3, remote)│
                                              │  opens TCP sockets   │
                                              └──────────────────────┘
```

### Bootstrap — no files written to disk

At build time `build.py` embeds `serverside.py` as a raw string literal
directly into `out/sshpytunnel.py`.  At launch the client opens an SSH
connection and passes the embedded server code to `python -u -c` on the
remote host via stdin — the exact byte count is baked into the command so
no length-prefix handshake is needed, and the rest of stdin is then used
exclusively for the binary data channel.

The remote interpreter is `python3` by default; use `--srvpycmd python`
(or any interpreter path) to target a server where only Python 2 is
available.  The `-u` flag is passed unconditionally to force unbuffered
stdout so the startup banner arrives immediately.

### Framing protocol

Every message on the SSH stdio pipe is a frame:

```
┌──────────────────┬──────────────────┬──────────────────────┐
│  channel-id (4B) │  length    (4B)  │  data  (length bytes)│
│  big-endian      │  big-endian      │                      │
└──────────────────┴──────────────────┴──────────────────────┘
```

- **Channel 0** — JSON control messages (`open`, `close`, `result`)
- **Channel N** — raw TCP data for the Nth proxied connection

A zero-length data frame signals EOF (half-close) on the corresponding
connection.

---

## Building from source

The single-file output is assembled from these source files:

```
clientside.py     Client code — SOCKS5 server + multiplexer
serverside.py     Remote agent, Python 2/3 compatible
build.py          Assembler (injects server code into client template)
Makefile          Drives the build
```

```bash
# Build the output file
make

# Build + syntax-check all files
make check

# Build, syntax-check, and run integration tests
make test

# Remove generated files
make clean
```

`build.py` validates the generated file with `ast.parse()` before writing,
so a syntax error in any source file is caught at build time.  The output
is written to `out/sshpytunnel.py`.

---

## Security notes

- The tunnel inherits whatever trust you place in the SSH connection.  Use
  key-based authentication and verify host keys.
- By default the SOCKS5 proxy binds to `127.0.0.1` only.  If you use
  `--bind 0.0.0.0` make sure untrusted clients cannot reach that port.
- SSH has full TTY access, so host key verification works normally — SSH will
  prompt interactively on first connection to an unknown host, just as it would
  in a regular session.
