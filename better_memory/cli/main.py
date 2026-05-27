"""Top-level CLI dispatcher: `better-memory <subcommand>`.

Registered via `[project.scripts]` in `pyproject.toml`. Subcommand modules
live alongside this one in `better_memory/cli/`. Today: `agentcore`.
"""

from __future__ import annotations

import argparse
import sys
from typing import Sequence


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="better-memory",
        description="better-memory operator CLI.",
    )
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        metavar="<command>",
    )

    # ----- agentcore subcommand group -----
    ac_parser = subparsers.add_parser(
        "agentcore",
        help="Manage AWS Bedrock AgentCore Memory backend resources.",
    )
    # The agentcore subgroup builds its own subparsers — import lazily so
    # `better-memory --help` doesn't pull in boto3 (it's an optional dep).
    from better_memory.cli import agentcore as agentcore_cli
    agentcore_cli.add_subparsers(ac_parser)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "agentcore":
        from better_memory.cli import agentcore as agentcore_cli
        return agentcore_cli.handle(args)

    parser.error(f"unknown command: {args.command}")
    return 2  # unreachable; parser.error raises SystemExit


if __name__ == "__main__":
    sys.exit(main())
