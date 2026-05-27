"""`better-memory agentcore ...` subcommand group.

Subcommands: init, status, smoke, migrate-from-sqlite. Implemented in
Tasks 2-5 of Plan 3. This module is loaded only when the user invokes
`better-memory agentcore <subcmd>` so sqlite-only users never pay the
boto3 import cost.
"""

from __future__ import annotations

import argparse


def add_subparsers(parent: argparse.ArgumentParser) -> None:
    """Register agentcore subcommands on the given parent parser."""
    subparsers = parent.add_subparsers(
        dest="subcommand",
        required=True,
        metavar="<subcommand>",
    )
    # Subcommands land in Tasks 2-5
    for name in ("init", "status", "smoke", "migrate-from-sqlite"):
        subparsers.add_parser(name, help=f"(not yet implemented) {name}")


def handle(args: argparse.Namespace) -> int:
    """Route to the right subcommand handler."""
    raise NotImplementedError(
        f"agentcore {args.subcommand} is implemented in a later Plan-3 task"
    )
