# Makefile for sshpytunnel
#
# Source files:
#   clientside.py     – client-side Python 2/3 code (placeholders for server)
#   serverside.py     – server script, Python 2/3 compatible
#   build.py          – assembles the sources into a single output file
#
# Generated file (do not edit directly):
#   out/sshpytunnel.py

PYTHON   := python3
BUILD    := $(PYTHON) build.py
SOURCES  := clientside.py build.py
OUT      := out/sshpytunnel.py

.PHONY: all clean check test

all: $(OUT)

# ── build ─────────────────────────────────────────────────────────────────
$(OUT): $(SOURCES) serverside.py
	$(BUILD) \
	    --server serverside.py \
	    --out $(OUT) \
	    --title "sshpytunnel – SOCKS5 proxy tunnelled over SSH stdin/stdout"

# ── syntax check ──────────────────────────────────────────────────────────
check: all
	$(PYTHON) -c "import ast; ast.parse(open('$(OUT)').read()); print('$(OUT)   OK')"
	$(PYTHON) -c "import ast; ast.parse(open('serverside.py').read()); print('serverside.py  OK')"

# ── integration tests ────────────────────────────────────────────────────
test: check
	$(PYTHON) $(OUT) --test --srvpycmd python3 --port 19080 & P3=$$!; \
	$(PYTHON) $(OUT) --test --srvpycmd python2 --port 19081 & P2=$$!; \
	sleep 1; \
	curl -fs --socks5-hostname 127.0.0.1:19080 http://example.com -o /dev/null -w "python3: HTTP %{http_code}\n" --max-time 10; \
	curl -fs --socks5-hostname 127.0.0.1:19081 http://example.com -o /dev/null -w "python2:  HTTP %{http_code}\n" --max-time 10; \
	kill $$P3 $$P2 2>/dev/null; wait $$P3 $$P2 2>/dev/null; true

# ── clean up generated files ──────────────────────────────────────────────
clean:
	rm -rf out/
