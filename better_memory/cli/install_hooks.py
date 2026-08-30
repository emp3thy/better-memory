"""DEPRECATED: superseded by `better-memory setup` (better_memory/cli/setup_cmd.py).

Kept as a thin shim: parses the legacy flags, ignores them (machine params
are now auto-detected), and delegates to the setup engine.
"""
from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="better_memory.cli.install_hooks")
    parser.add_argument("--venv-py")
    parser.add_argument("--venv-pyw")
    parser.add_argument("--home")
    parser.parse_args(argv)
    print("[install_hooks] DEPRECATED — running `better-memory setup` instead.",
          file=sys.stderr)
    from better_memory.cli.setup_cmd import handle_setup
    sys.exit(handle_setup(argparse.Namespace()))


if __name__ == "__main__":
    main()
