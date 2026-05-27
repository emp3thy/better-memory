"""`better-memory agentcore ...` subcommand group."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

from better_memory.cli._agentcore_strategies import (
    DEFAULT_EPISODIC_EVENT_EXPIRY_DAYS,
    DEFAULT_EPISODIC_NAME,
    DEFAULT_EPISODIC_STRATEGY_NAME,
    DEFAULT_SEMANTIC_EVENT_EXPIRY_DAYS,
    DEFAULT_SEMANTIC_NAME,
    DEFAULT_SEMANTIC_STRATEGY_NAME,
    INDEXED_KEYS,
    episodic_strategy_block,
    semantic_strategy_block,
)
from better_memory.storage.agentcore_persistence import (
    AgentCoreConfig,
    MemoryRecord,
    save_agentcore_config,
)

_POLL_INTERVAL_S = 5
# Bumped to 240s vs the 180s the Plan 2 smoke uses: smoke runs solo against
# a clean account, but `init` runs after the user has just `export`ed env
# vars and may be hitting a fresh / cold region — small extra headroom is
# cheap and prevents the user thinking init hung.
_POLL_TIMEOUT_S = 240


def add_subparsers(parent: argparse.ArgumentParser) -> None:
    subparsers = parent.add_subparsers(
        dest="subcommand", required=True, metavar="<subcommand>",
    )

    p_init = subparsers.add_parser(
        "init",
        help="Create AgentCore memories and write agentcore.json",
    )
    p_init.add_argument("--home", default=None, help="Override BETTER_MEMORY_HOME")
    p_init.add_argument("--region", default="eu-west-2", help="AWS region")
    p_init.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing agentcore.json",
    )

    p_status = subparsers.add_parser(
        "status",
        help="Show memory IDs and ACTIVE/CREATING/FAILED states",
    )
    p_status.add_argument("--home", default=None)
    p_status.add_argument("--region", default=None)

    p_smoke = subparsers.add_parser("smoke", help="Run an observe + retrieve smoke loop")
    p_smoke.add_argument("--home", default=None)
    p_smoke.add_argument("--region", default=None)

    subparsers.add_parser(
        "migrate-from-sqlite",
        help="(deferred) Bulk-migrate sqlite data to AgentCore",
    )


def handle(args: argparse.Namespace) -> int:
    if args.subcommand == "init":
        return _handle_init(args)
    if args.subcommand == "status":
        return _handle_status(args)
    if args.subcommand == "smoke":
        return _handle_smoke(args)
    if args.subcommand == "migrate-from-sqlite":
        return _handle_migrate(args)
    print(f"unknown subcommand: {args.subcommand}", file=sys.stderr)
    return 2


def _resolve_home(arg_home: str | None) -> Path:
    import os
    if arg_home:
        return Path(arg_home).expanduser()
    return Path(os.environ.get("BETTER_MEMORY_HOME", "~/.better-memory")).expanduser()


def _build_control_client(region: str) -> Any:
    """Build the bedrock-agentcore-control boto3 client. Patched out in tests."""
    import boto3
    from botocore.config import Config as BotoConfig
    return boto3.client(
        "bedrock-agentcore-control",
        config=BotoConfig(region_name=region, retries={"mode": "standard", "max_attempts": 5}),
    )


def _build_data_client(region: str) -> Any:
    import boto3
    from botocore.config import Config as BotoConfig
    return boto3.client(
        "bedrock-agentcore",
        config=BotoConfig(region_name=region, retries={"mode": "standard", "max_attempts": 5}),
    )


def _poll_until_active(control: Any, memory_id: str, *, label: str) -> dict:
    """Poll GetMemory until the memory AND every strategy are ACTIVE.

    Prints progress every poll so the user sees the long ~90-115s creation
    isn't a hang. Returns the final memory dict."""
    start = time.monotonic()
    while time.monotonic() - start < _POLL_TIMEOUT_S:
        response = control.get_memory(memoryId=memory_id)
        memory = response["memory"]
        memory_status = memory.get("status")
        strategies = memory.get("strategies", [])
        all_strategies_active = strategies and all(
            s.get("status") == "ACTIVE" for s in strategies
        )
        print(
            f"  .. {label} memory_status={memory_status} "
            f"strategies_active={bool(all_strategies_active)}"
        )
        if memory_status == "ACTIVE" and all_strategies_active:
            return memory
        if memory_status == "FAILED":
            raise RuntimeError(f"{label} memory entered FAILED state: {memory!r}")
        time.sleep(_POLL_INTERVAL_S)
    raise TimeoutError(
        f"{label} memory did not become ACTIVE within {_POLL_TIMEOUT_S}s"
    )


def _find_existing_memory(control: Any, name: str) -> str | None:
    """Return memory_id if a non-deleting memory with this name already exists."""
    paginator = control.get_paginator("list_memories")
    for page in paginator.paginate():
        for summary in page.get("memories", []):
            if summary.get("status") == "DELETING":
                continue
            try:
                memory = control.get_memory(memoryId=summary["id"])["memory"]
            except Exception:
                continue
            if memory.get("name") == name:
                return memory["id"]
    return None


def _create_one_memory(
    control: Any,
    *,
    name: str,
    strategy_block: dict,
    strategy_name: str,
    event_expiry_days: int,
    label: str,
) -> MemoryRecord:
    print(f">> Creating {label} memory ({name!r})...")
    response = control.create_memory(
        name=name,
        eventExpiryDuration=event_expiry_days,
        memoryStrategies=[strategy_block],
        indexedKeys=INDEXED_KEYS,
    )
    initial = response["memory"]
    memory_id = initial["id"]
    print(f"   created: memory_id={memory_id}")

    final = _poll_until_active(control, memory_id, label=label)
    strategies = final.get("strategies") or []
    if not strategies:
        raise RuntimeError(f"{label} memory has no strategies after ACTIVE: {final!r}")
    return MemoryRecord(
        memory_id=memory_id,
        memory_arn=final["arn"],
        memory_name=final.get("name", name),
        strategy_id=strategies[0]["strategyId"],
        strategy_name=strategies[0].get("name", strategy_name),
        event_expiry_duration_days=event_expiry_days,
    )


def _handle_init(args: argparse.Namespace) -> int:
    home = _resolve_home(args.home)
    config_path = home / "agentcore.json"

    if config_path.exists() and not args.force:
        print(
            f"agentcore.json already exists at {config_path}. "
            f"Pass --force to recreate (this will leave the old memories "
            f"in AWS — clean them up via the console if you no longer "
            f"want them).",
            file=sys.stderr,
        )
        return 1

    control = _build_control_client(args.region)

    # Pre-flight name check for BOTH names so partial existing state is
    # surfaced before any CreateMemory runs (and we don't get a half-done
    # account where one name is taken and the other isn't).
    for name in (DEFAULT_EPISODIC_NAME, DEFAULT_SEMANTIC_NAME):
        if _find_existing_memory(control, name) is not None:
            print(
                f"A memory named {name!r} already exists in {args.region}. "
                f"Either delete it via the AWS console or re-use it by "
                f"hand-editing agentcore.json. init refuses to create a "
                f"second copy.",
                file=sys.stderr,
            )
            return 1

    episodic: MemoryRecord | None = None
    try:
        episodic = _create_one_memory(
            control,
            name=DEFAULT_EPISODIC_NAME,
            strategy_block=episodic_strategy_block(),
            strategy_name=DEFAULT_EPISODIC_STRATEGY_NAME,
            event_expiry_days=DEFAULT_EPISODIC_EVENT_EXPIRY_DAYS,
            label="episodic",
        )

        semantic = _create_one_memory(
            control,
            name=DEFAULT_SEMANTIC_NAME,
            strategy_block=semantic_strategy_block(),
            strategy_name=DEFAULT_SEMANTIC_STRATEGY_NAME,
            event_expiry_days=DEFAULT_SEMANTIC_EVENT_EXPIRY_DAYS,
            label="semantic",
        )
    except Exception as exc:
        # ValidationException on the name regex is the most common
        # operator error — surface the boto3 message + a pointer to the
        # troubleshooting page rather than dumping a raw ClientError trace.
        code = ""
        try:
            from botocore.exceptions import ClientError
            if isinstance(exc, ClientError):
                code = exc.response.get("Error", {}).get("Code", "")
        except Exception:
            pass

        # Orphan cleanup: if episodic was created but semantic failed,
        # delete the episodic memory so a re-run of `init` starts clean.
        if episodic is not None:
            print(
                f"\n!! Second memory create failed ({exc!r}). "
                f"Deleting orphan episodic memory {episodic.memory_id} "
                f"so a re-run starts clean...",
                file=sys.stderr,
            )
            try:
                control.delete_memory(memoryId=episodic.memory_id)
                print(f"   deleted {episodic.memory_id}", file=sys.stderr)
            except Exception as del_exc:
                print(
                    f"   WARN: failed to delete orphan {episodic.memory_id}: "
                    f"{del_exc!r}. Delete it manually via the AWS console "
                    f"before re-running init.",
                    file=sys.stderr,
                )

        if code == "ValidationException":
            print(
                f"\nAWS rejected the memory create as invalid: {exc}. "
                f"Memory names must match `[a-zA-Z][a-zA-Z0-9_]{{0,47}}` "
                f"— underscores only, no dashes. See "
                f"docs/troubleshooting/agentcore.md for the full list.",
                file=sys.stderr,
            )
            return 1
        raise

    cfg = AgentCoreConfig(
        schema_version=1,
        region=args.region,
        semantic=semantic,
        episodic=episodic,
    )
    save_agentcore_config(cfg, home)

    print()
    print(f"agentcore.json written to {config_path}")
    print(f"  episodic memory_id: {episodic.memory_id}")
    print(f"  semantic memory_id: {semantic.memory_id}")
    print()
    print("Next steps:")
    print("  1. Export BETTER_MEMORY_STORAGE_BACKEND=agentcore")
    print("  2. Restart your MCP server (or Claude Code session)")
    print("  3. Run `better-memory agentcore smoke` to verify the round-trip")
    return 0


def _handle_status(args: argparse.Namespace) -> int:
    raise NotImplementedError("status lands in Task 3")


def _handle_smoke(args: argparse.Namespace) -> int:
    raise NotImplementedError("smoke lands in Task 4")


def _handle_migrate(args: argparse.Namespace) -> int:
    raise NotImplementedError("migrate-from-sqlite lands in Task 5")
