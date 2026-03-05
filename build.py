#!/usr/bin/env python3
"""
build.py – combines clientside.py with serverside.py into a single file.

Usage (called by the Makefile):
  python3 build.py --server serverside.py --out out/sshpytunnel.py [--title "..."]

The script replaces the placeholder in clientside.py with the contents
of serverside.py:
  "@@SERVER_CODE@@"  ->  r\"""<contents of serverside.py>\"""
"""

import sys
import argparse
import ast
import pathlib

PLACEHOLDER    = '"@@SERVER_CODE@@"'
CLIENT_SRC     = pathlib.Path(__file__).parent / "clientside.py"


def _embed(code: str) -> str:
    """Wrap *code* as a raw triple-quoted string literal."""
    if '"""' in code:
        raise ValueError(
            'Server code contains triple-double-quotes (\"\"\") which '
            'cannot be safely embedded.  Use single-quoted docstrings or '
            '# comments in the server-side scripts.'
        )
    if not code.endswith("\n"):
        code += "\n"
    return 'r"""\n' + code + '"""'


def build(server_path: str, out_path: str, title: str = "") -> None:
    """Assemble *out_path* by embedding *server_path* into clientside.py."""
    client      = CLIENT_SRC.read_text(encoding="utf-8")
    server_code = pathlib.Path(server_path).read_text(encoding="utf-8")

    if PLACEHOLDER not in client:
        raise RuntimeError("Placeholder {0!r} not found in clientside.py".format(PLACEHOLDER))

    result = client.replace(PLACEHOLDER, _embed(server_code))

    if title:
        result = result.replace(
            "sshpytunnel – SOCKS5 proxy tunnelled over SSH stdin/stdout",
            title,
        )

    try:
        ast.parse(result)
    except SyntaxError as e:
        print("ERROR: generated file has a syntax error at line {0}: {1}".format(e.lineno, e.msg),
              file=sys.stderr)
        lines = result.splitlines()
        for i in range(max(0, e.lineno - 3), min(len(lines), e.lineno + 2)):
            print("  {0:5d}: {1}".format(i + 1, lines[i]), file=sys.stderr)
        sys.exit(1)

    out = pathlib.Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(result, encoding="utf-8")
    print("Built {0}  ({1} chars, {2} lines)".format(out_path, len(result), result.count(chr(10))))


def main():
    parser = argparse.ArgumentParser(description="Build sshpytunnel.py from sources")
    parser.add_argument("--server", required=True, metavar="FILE",
                        help="server-side script (serverside.py)")
    parser.add_argument("--out",    required=True, metavar="FILE",
                        help="output file path")
    parser.add_argument("--title",  default="",
                        help="optional variant title for the module docstring")
    args = parser.parse_args()
    build(args.server, args.out, args.title)


if __name__ == "__main__":
    main()
