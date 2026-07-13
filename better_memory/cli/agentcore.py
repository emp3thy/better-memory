"""`better-memory agentcore ...` subcommand group."""

from __future__ import annotations

import argparse
import json
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
    SEMANTIC_METADATA_SCHEMA,
    episodic_strategy_block,
    semantic_strategy_block,
)
from better_memory.storage.agentcore_persistence import (
    AgentCoreConfig,
    MemoryRecord,
    load_agentcore_config,
    save_agentcore_config,
)

_POLL_INTERVAL_S = 5
# Bumped to 240s vs the 180s the Plan 2 smoke uses: smoke runs solo against
# a clean account, but `init` is often the user's very first call into a
# fresh / cold region — small extra headroom is cheap and prevents the user
# thinking init hung.
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
    p_init.add_argument(
        "--no-activate",
        action="store_true",
        help=(
            "Provision the AWS memories and write agentcore.json without "
            "activating the agentcore backend in settings.json "
            "(provision-only scripting)"
        ),
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

    p_migrate = subparsers.add_parser(
        "migrate",
        help="Migrate sqlite reflections + semantic memories into AgentCore",
    )
    p_migrate.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Compute the plan and print tallies without any AWS call",
    )
    p_migrate.add_argument(
        "--include",
        default="reflections,semantic",
        help=(
            "Comma list of kinds to migrate: reflections, semantic, "
            "observations (observations are lossy / non-retrievable)"
        ),
    )
    p_migrate.add_argument(
        "--restart",
        action="store_true",
        help="Re-verify/upsert every eligible row, ignoring ledger 'migrated' state",
    )
    p_migrate.add_argument(
        "--provision",
        action="store_true",
        help="Create the target memories if missing/not ACTIVE (else hard error)",
    )
    p_migrate.add_argument(
        "--db", default=None, help="Source SQLite path (default <home>/memory.db)"
    )
    p_migrate.add_argument(
        "--project", default=None, help="Limit migration to one project"
    )
    p_migrate.add_argument("--region", default=None, help="AWS region override")
    p_migrate.add_argument("--home", default=None, help="Override BETTER_MEMORY_HOME")
    p_migrate.add_argument(
        "--batch-size",
        type=int,
        default=25,
        dest="batch_size",
        help="Records per batch_create_memory_records call (default 25)",
    )
    p_migrate.add_argument(
        "--verify",
        action="store_true",
        help="Read a sample back via get_memory_record and diff against SQLite",
    )


def handle(args: argparse.Namespace) -> int:
    if args.subcommand == "init":
        return _handle_init(args)
    if args.subcommand == "status":
        return _handle_status(args)
    if args.subcommand == "smoke":
        return _handle_smoke(args)
    if args.subcommand == "migrate":
        return _handle_migrate(args)
    print(f"unknown subcommand: {args.subcommand}", file=sys.stderr)
    return 2


def _resolve_home(arg_home: str | None) -> Path:
    import os
    if arg_home:
        return Path(arg_home).expanduser()
    return Path(os.environ.get("BETTER_MEMORY_HOME", "~/.better-memory")).expanduser()


def _write_settings_activation(home: Path) -> Path:
    """Persist ``{"storage_backend": "agentcore"}`` into ``<home>/settings.json``.

    Merges into an existing JSON object so unrelated keys survive; a missing
    or corrupt file is replaced wholesale (init IS the remediation path for a
    broken settings.json, so it must not crash on one). Written atomically —
    tmp file + replace, the same pattern as ``save_agentcore_config``.
    """
    settings_path = home / "settings.json"
    data: dict[str, Any] = {}
    try:
        raw = json.loads(settings_path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            data = raw
    except (OSError, json.JSONDecodeError):
        data = {}
    data["storage_backend"] = "agentcore"
    home.mkdir(parents=True, exist_ok=True)
    tmp = settings_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(settings_path)
    return settings_path


def _effective_backend(home: Path) -> tuple[str, str]:
    """Resolve the backend ``home`` would use, plus where the answer came from.

    Mirrors :func:`better_memory.config.resolve_storage_backend` but honours
    the CLI's ``--home`` override (the config resolver only reads
    ``$BETTER_MEMORY_HOME``) and reports the source: ``env`` / ``settings`` /
    ``default``. Where the runtime resolver would RAISE on an invalid env
    value (killing the server at boot), status keeps going and flags the
    value instead — a diagnostic tool must surface the misconfiguration, not
    reproduce the crash. May raise ``ValueError`` on a corrupt settings.json.
    """
    import os

    from better_memory.config import (
        _VALID_STORAGE_BACKENDS,
        _read_settings_storage_backend,
    )

    raw = os.environ.get("BETTER_MEMORY_STORAGE_BACKEND")
    if raw is not None:
        if raw not in _VALID_STORAGE_BACKENDS:
            return raw, "env — INVALID value; the MCP server will refuse to start"
        return raw, "env"
    from_file = _read_settings_storage_backend(home)
    if from_file is not None:
        return from_file, "settings"
    return "sqlite", "default"


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
    created_ids: list[str],
) -> MemoryRecord:
    """Create one AgentCore memory and wait for ACTIVE.

    Appends the raw memory_id to ``created_ids`` immediately after
    CreateMemory returns — BEFORE the polling loop — so the caller can
    clean up even if `_poll_until_active` raises (FAILED state, 240s
    timeout, network blip). Without this, polling failures would leak
    the AWS resource because the helper never returns a MemoryRecord."""
    print(f">> Creating {label} memory ({name!r})...")
    response = control.create_memory(
        name=name,
        eventExpiryDuration=event_expiry_days,
        memoryStrategies=[strategy_block],
        indexedKeys=INDEXED_KEYS,
    )
    initial = response["memory"]
    memory_id = initial["id"]
    created_ids.append(memory_id)
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

    # Mutable list of raw memory_ids appended by _create_one_memory
    # immediately after each CreateMemory returns. On exception we iterate
    # this list and delete every memory we created — covers both the
    # "create #2 raised" case AND the "create #1 succeeded but its
    # _poll_until_active timed out / hit FAILED" case (which a return-value-
    # based cleanup would miss).
    created_ids: list[str] = []
    try:
        episodic = _create_one_memory(
            control,
            name=DEFAULT_EPISODIC_NAME,
            strategy_block=episodic_strategy_block(),
            strategy_name=DEFAULT_EPISODIC_STRATEGY_NAME,
            event_expiry_days=DEFAULT_EPISODIC_EVENT_EXPIRY_DAYS,
            label="episodic",
            created_ids=created_ids,
        )

        semantic = _create_one_memory(
            control,
            name=DEFAULT_SEMANTIC_NAME,
            strategy_block=semantic_strategy_block(),
            strategy_name=DEFAULT_SEMANTIC_STRATEGY_NAME,
            event_expiry_days=DEFAULT_SEMANTIC_EVENT_EXPIRY_DAYS,
            label="semantic",
            created_ids=created_ids,
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

        # Orphan cleanup: every memory_id appended to created_ids gets
        # deleted, including any whose _poll_until_active raised.
        if created_ids:
            print(
                f"\n!! Memory create / poll failed ({exc!r}). "
                f"Deleting {len(created_ids)} orphan "
                f"memor{'y' if len(created_ids) == 1 else 'ies'} "
                f"so a re-run of init starts clean...",
                file=sys.stderr,
            )
            for orphan_id in created_ids:
                try:
                    control.delete_memory(memoryId=orphan_id)
                    print(f"   deleted {orphan_id}", file=sys.stderr)
                except Exception as del_exc:
                    print(
                        f"   WARN: failed to delete orphan {orphan_id}: "
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

    settings_path = home / "settings.json"
    activate = not args.no_activate
    if activate:
        _write_settings_activation(home)

    print()
    print(f"agentcore.json written to {config_path}")
    print(f"  episodic memory_id: {episodic.memory_id}")
    print(f"  semantic memory_id: {semantic.memory_id}")
    print()
    if activate:
        print(
            f"agentcore is now the default backend for {home} (persisted in "
            f"{settings_path}; the BETTER_MEMORY_STORAGE_BACKEND env var "
            f"still overrides — unset it or set it to agentcore). To revert: "
            f"remove 'storage_backend' from settings.json or set the env var "
            f"to sqlite."
        )
    else:
        print(
            f"Activation skipped (--no-activate): agentcore.json is written "
            f"but the backend for {home} is unchanged. To activate later, "
            f'add {{"storage_backend": "agentcore"}} to {settings_path} or '
            f"set BETTER_MEMORY_STORAGE_BACKEND=agentcore."
        )
    print()
    print("Next steps:")
    if activate:
        print(
            "  1. Restart Claude Code (or your MCP server) so it picks up the new backend"
        )
        print(
            "  2. Run `better-memory agentcore status` to confirm the effective backend"
        )
        print("     and that both memories are ACTIVE")
        print("  3. Run `better-memory agentcore smoke` to verify the AWS round-trip")
        print(
            "     (smoke validates AWS credentials and wire access, not MCP registration)"
        )
    else:
        print("  1. Activate when ready (see above) — no backend change was made yet")
        print("  2. Run `better-memory agentcore status` to confirm both memories are")
        print("     ACTIVE (the effective backend is unchanged until you activate)")
        print("  3. Run `better-memory agentcore smoke` to verify the AWS round-trip")
        print(
            "     (smoke validates AWS credentials and wire access, not MCP registration)"
        )
    return 0


def _handle_status(args: argparse.Namespace) -> int:
    home = _resolve_home(args.home)
    cfg = load_agentcore_config(home)
    if cfg is None:
        print(
            f"No agentcore.json found at {home / 'agentcore.json'}. "
            f"Run `better-memory agentcore init` first.",
            file=sys.stderr,
        )
        return 1

    try:
        backend, source = _effective_backend(home)
        print(f"effective backend: {backend} (source: {source})")
    except ValueError as exc:
        # A corrupt settings.json must not take `status` down — it is the
        # diagnostic tool the user reaches for. Warn and keep reporting.
        print(
            f"WARN: could not resolve the effective backend: {exc}",
            file=sys.stderr,
        )

    region = args.region or cfg.region
    control = _build_control_client(region)

    all_active = True
    for label, record in (("episodic", cfg.episodic), ("semantic", cfg.semantic)):
        response = control.get_memory(memoryId=record.memory_id)
        memory = response["memory"]
        status = memory.get("status", "UNKNOWN")
        strategies = memory.get("strategies") or []
        strategy_summary = ", ".join(
            f"{s.get('name','?')}={s.get('status','?')}"
            for s in strategies
        ) or "(none)"
        expiry = memory.get("eventExpiryDuration", "?")
        is_active = (
            status == "ACTIVE"
            and strategies
            and all(s.get("status") == "ACTIVE" for s in strategies)
        )
        if not is_active:
            all_active = False
        print(f"{label}:")
        print(f"  memory_id:   {record.memory_id}")
        print(f"  name:        {memory.get('name', '?')}")
        print(f"  status:      {status}")
        print(f"  strategies:  {strategy_summary}")
        print(f"  expiry_days: {expiry}")

    return 0 if all_active else 1


def _handle_smoke(args: argparse.Namespace) -> int:
    """Minimal observe + closure + retrieve cycle for ops verification."""
    home = _resolve_home(args.home)
    cfg = load_agentcore_config(home)
    if cfg is None:
        print(
            f"No agentcore.json found at {home / 'agentcore.json'}. "
            f"Run `better-memory agentcore init` first.",
            file=sys.stderr,
        )
        return 1

    region = args.region or cfg.region
    actor_id = "smoke"
    session_id = f"smoke-{int(time.time())}"
    from datetime import UTC, datetime

    try:
        # Build client INSIDE try so import / region / credential failures
        # land in the same "smoke FAILED -> rc=1" path as wire errors,
        # rather than escaping as an unhandled traceback.
        data = _build_data_client(region)
        print(">> 1. CreateEvent — observation")
        data.create_event(
            memoryId=cfg.episodic.memory_id,
            actorId=actor_id,
            sessionId=session_id,
            eventTimestamp=datetime.now(UTC),
            payload=[{"conversational": {
                "role": "USER",
                "content": {"text": "smoke test observation"},
            }}],
            metadata={"theme": {"stringValue": "smoke"}},
        )
        print("   ok")

        print(">> 2. CreateEvent — closure marker (role=OTHER)")
        data.create_event(
            memoryId=cfg.episodic.memory_id,
            actorId=actor_id,
            sessionId=session_id,
            eventTimestamp=datetime.now(UTC),
            payload=[{"conversational": {
                "role": "OTHER",
                "content": {"text": "session closed"},
            }}],
        )
        print("   ok")

        print(">> 3. ListEvents — confirm events readable")
        response = data.list_events(
            memoryId=cfg.episodic.memory_id,
            actorId=actor_id,
            sessionId=session_id,
            maxResults=10,
            includePayloads=True,
        )
        events = response.get("events", [])
        if len(events) < 2:
            raise RuntimeError(
                f"list_events returned {len(events)} events; expected >= 2"
            )
        print(f"   ok ({len(events)} events)")

        print(">> 4. BatchCreateMemoryRecords — semantic write")
        # Live-verified request shape (aws_record_dialect.md §1): per-record
        # required keys are requestIdentifier + namespaces + content +
        # timestamp. memoryRecordId is NOT a valid input key (botocore
        # ParamValidationError) — the SERVER mints the durable mem-<uuid4>
        # id, returned in successfulRecords[0].memoryRecordId;
        # requestIdentifier is correlation-only.
        request_identifier = f"smoke-rec-{int(time.time())}"
        create_resp = data.batch_create_memory_records(
            memoryId=cfg.semantic.memory_id,
            records=[{
                "requestIdentifier": request_identifier,
                "namespaces": [f"projects/{actor_id}/semantic/"],
                "content": {"text": "smoke test semantic record"},
                "timestamp": datetime.now(UTC),
                "metadata": {
                    "useful_count": {"numberValue": 0},
                    "status": {"stringValue": "active"},
                },
            }],
        )
        failed = create_resp.get("failedRecords", [])
        if failed:
            raise RuntimeError(f"batch_create failed: {failed!r}")
        successful = create_resp.get("successfulRecords") or []
        if not successful:
            raise RuntimeError(
                f"batch_create returned no successful records: {create_resp!r}"
            )
        real_id = successful[0]["memoryRecordId"]
        print(f"   ok (id={real_id})")

        print(">> 5. GetMemoryRecord — readback")
        # get_memory_record is read-your-write (~1s); list_memory_records
        # shares an index with ~60s lag and would force the smoke to poll
        # for a minute (aws_record_dialect.md §3). Retry a few times to
        # cover the sub-second window all the same.
        record = None
        for attempt in range(1, 6):
            try:
                record = data.get_memory_record(
                    memoryId=cfg.semantic.memory_id,
                    memoryRecordId=real_id,
                )["memoryRecord"]
                break
            except Exception as get_exc:  # noqa: BLE001 — retried, re-raised below
                code = getattr(get_exc, "response", {}).get(
                    "Error", {}
                ).get("Code", "")
                if code != "ResourceNotFoundException" or attempt == 5:
                    raise
                time.sleep(2)
        if record is None or record.get("memoryRecordId") != real_id:
            raise RuntimeError(
                f"get_memory_record readback mismatch: {record!r}"
            )
        print("   ok (readback id matches)")

        print(">> 6. BatchDeleteMemoryRecords — cleanup")
        del_resp = data.batch_delete_memory_records(
            memoryId=cfg.semantic.memory_id,
            records=[{"memoryRecordId": real_id}],
        )
        if del_resp.get("failedRecords"):
            raise RuntimeError(
                f"batch_delete failed: {del_resp['failedRecords']!r}"
            )
        print("   ok")

        print()
        print("AgentCore smoke PASSED")
        return 0
    except Exception as exc:
        print(f"AgentCore smoke FAILED: {exc!r}", file=sys.stderr)
        return 1


_VALID_INCLUDE_KINDS = frozenset({"reflections", "semantic", "observations"})


def _memory_is_active(memory: dict) -> bool:
    """Readiness rule (mirrors ``_poll_until_active``): memory ACTIVE AND
    every strategy ACTIVE."""
    strategies = memory.get("strategies") or []
    return (
        memory.get("status") == "ACTIVE"
        and bool(strategies)
        and all(s.get("status") == "ACTIVE" for s in strategies)
    )


def _collect_declared_metadata_keys(node: Any) -> set[str] | None:
    """Walk a GetMemory ``memory`` dict for any ``metadataSchema`` list and
    collect the declared ``key`` names.

    Returns ``None`` if no ``metadataSchema`` is present anywhere (AWS did not
    expose the strategy schema) so the caller can distinguish "schema lacks the
    key" from "schema not introspectable".
    """
    found: set[str] = set()
    saw_schema = False

    def _walk(obj: Any) -> None:
        nonlocal saw_schema
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key == "metadataSchema" and isinstance(value, list):
                    saw_schema = True
                    for entry in value:
                        if isinstance(entry, dict) and "key" in entry:
                            found.add(str(entry["key"]))
                else:
                    _walk(value)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item)

    _walk(node)
    return found if saw_schema else None


_REPROVISION_GUIDANCE = (
    "Re-provision a fresh semantic memory with `better-memory agentcore init` "
    "(or delete it in the AWS console and re-run migrate --provision) before "
    "migrating semantic rows."
)


def _ensure_semantic_schema(
    control: Any, cfg: AgentCoreConfig, semantic_memory: dict
) -> None:
    """Guarantee the semantic strategy declares ``source_row_id`` before any
    write (design §1b / T3).

    §1b requires the migrate/--provision path to *guarantee* the target schema
    declares the needed keys before writing, because AWS silently drops
    undeclared keys (notably ``source_row_id``) on client BASE writes — and a
    dropped ``source_row_id`` breaks semantic reconcile-by-source_row_id, so
    every re-run re-creates all semantic records (idempotency lost).

    Behaviour:
    - If the live schema already declares ``source_row_id`` → return.
    - Otherwise widen it in place via ``update_memory_strategy``, sending the
      FULL :data:`SEMANTIC_METADATA_SCHEMA` (which declares ``source_row_id``
      plus the counters). The previous empty ``customExtractionConfiguration``
      payload could never add the key.
    - After the update, RE-READ the memory and CONFIRM ``source_row_id`` is now
      declared. AWS may accept a malformed / no-op update without raising; if
      the key is still not declared (or the schema is not introspectable so we
      cannot verify it), we raise rather than proceed — writing into an
      unconfirmed-narrow schema would silently drop ``source_row_id``.
    """
    declared = _collect_declared_metadata_keys(semantic_memory)
    if declared is not None and "source_row_id" in declared:
        return

    strategy_id = cfg.semantic.strategy_id
    if declared is None:
        print(
            "   semantic strategy schema is not introspectable; widening it in "
            "place and re-verifying 'source_row_id' is declared..."
        )
    else:
        print(
            "   semantic strategy is missing the 'source_row_id' key; "
            "widening its schema in place..."
        )
    try:
        control.update_memory_strategy(
            memoryId=cfg.semantic.memory_id,
            memoryStrategyId=strategy_id,
            configuration={
                "userPreferenceOverride": {
                    "memoryRecordSchema": {
                        "metadataSchema": SEMANTIC_METADATA_SCHEMA
                    }
                }
            },
        )
    except Exception as exc:  # noqa: BLE001 — re-raised as actionable guidance
        raise RuntimeError(
            "The target semantic memory was provisioned before "
            "'source_row_id' was added to the userPreference schema, so "
            "migrated records cannot be de-duplicated. Its schema could not "
            f"be widened in place ({exc!r}). {_REPROVISION_GUIDANCE}"
        ) from exc

    # Re-read and CONFIRM the key is now declared — the update above may have
    # been accepted as a no-op without raising.
    try:
        refreshed = control.get_memory(memoryId=cfg.semantic.memory_id)["memory"]
    except Exception as exc:  # noqa: BLE001 — re-raised as actionable guidance
        raise RuntimeError(
            "Could not re-read the semantic memory to confirm its schema was "
            f"widened to declare 'source_row_id' ({exc!r}). {_REPROVISION_GUIDANCE}"
        ) from exc
    redeclared = _collect_declared_metadata_keys(refreshed)
    if redeclared is None or "source_row_id" not in redeclared:
        raise RuntimeError(
            "Widening the semantic strategy schema did not take effect: "
            "'source_row_id' is still not declared after update_memory_strategy "
            "(AWS accepted the call without adding the key, or the schema is not "
            f"introspectable so retention cannot be guaranteed). "
            f"{_REPROVISION_GUIDANCE}"
        )


def _resolve_target_memories(
    control: Any,
    cfg: AgentCoreConfig,
    home: Path,
    *,
    provision: bool,
    need_reflections: bool,
    need_semantic: bool,
) -> tuple[AgentCoreConfig, dict, dict]:
    """Verify (or, with ``--provision``, create) the episodic + semantic
    memories. Returns ``(cfg, episodic_memory, semantic_memory)`` where ``cfg``
    is the (possibly updated) config.

    When ``--provision`` mints a REPLACEMENT for a missing / not-ACTIVE memory,
    the new memory has a fresh memory_id + strategy_id. The returned
    :class:`MemoryRecord` MUST replace the stale one in ``cfg`` and be persisted
    to ``agentcore.json`` — otherwise the migration would push every batch to
    the dead memory id and hit ``ResourceNotFoundException`` on every record.
    The caller re-keys planned records to the (possibly new) strategy ids after
    this returns.

    Raises ``RuntimeError`` when a needed memory is missing / not ACTIVE and
    ``--provision`` was not passed.
    """
    import dataclasses

    memories: dict[str, dict] = {}
    targets = []
    if need_reflections:
        targets.append(("episodic", cfg.episodic))
    if need_semantic:
        targets.append(("semantic", cfg.semantic))

    for label, record in targets:
        memory: dict | None = None
        try:
            memory = control.get_memory(memoryId=record.memory_id)["memory"]
        except Exception as exc:  # noqa: BLE001 — not-found / access errors
            if not provision:
                raise RuntimeError(
                    f"{label} memory {record.memory_id!r} could not be read "
                    f"({exc!r}). Pass --provision to (re)create it, or run "
                    f"`better-memory agentcore init` first."
                ) from exc
            memory = None

        if memory is None or not _memory_is_active(memory):
            if not provision:
                raise RuntimeError(
                    f"{label} memory {record.memory_id!r} is not ACTIVE. "
                    f"Pass --provision to (re)create it, or wait for it to "
                    f"finish provisioning (`better-memory agentcore status`)."
                )
            # Provisioning of a missing/broken memory is a heavy control-plane
            # operation; only reached with --provision. Re-create via init's
            # helper, capture the fresh MemoryRecord, persist it into cfg, and
            # re-read.
            created_ids: list[str] = []
            if label == "episodic":
                new_record = _create_one_memory(
                    control,
                    name=DEFAULT_EPISODIC_NAME,
                    strategy_block=episodic_strategy_block(),
                    strategy_name=DEFAULT_EPISODIC_STRATEGY_NAME,
                    event_expiry_days=DEFAULT_EPISODIC_EVENT_EXPIRY_DAYS,
                    label="episodic",
                    created_ids=created_ids,
                )
                cfg = dataclasses.replace(cfg, episodic=new_record)
            else:
                new_record = _create_one_memory(
                    control,
                    name=DEFAULT_SEMANTIC_NAME,
                    strategy_block=semantic_strategy_block(),
                    strategy_name=DEFAULT_SEMANTIC_STRATEGY_NAME,
                    event_expiry_days=DEFAULT_SEMANTIC_EVENT_EXPIRY_DAYS,
                    label="semantic",
                    created_ids=created_ids,
                )
                cfg = dataclasses.replace(cfg, semantic=new_record)
            # Persist the replacement id/strategy so a crash after this point
            # (or a later run) targets the live memory, not the dead one.
            save_agentcore_config(cfg, home)
            memory = control.get_memory(memoryId=new_record.memory_id)["memory"]

        assert memory is not None  # active, or reassigned via the provision branch
        memories[label] = memory

    return cfg, memories.get("episodic", {}), memories.get("semantic", {})


def _acquire_migration_lock(home: Path) -> int:
    """Single-run advisory lock: a ``<home>/agentcore_migration.lock`` file
    created O_EXCL. Raises ``FileExistsError`` if a run is already in flight."""
    import os

    home.mkdir(parents=True, exist_ok=True)
    lock_path = home / "agentcore_migration.lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.write(fd, str(os.getpid()).encode("ascii"))
    return fd


def _release_migration_lock(home: Path, fd: int) -> None:
    import os

    try:
        os.close(fd)
    except OSError:
        pass
    try:
        (home / "agentcore_migration.lock").unlink()
    except OSError:
        pass


def _eligible_reflection_rows(conn: Any, project: str | None) -> list[Any]:
    if project:
        cur = conn.execute(
            "SELECT * FROM reflections "
            "WHERE status IN ('pending_review', 'confirmed') "
            "  AND (project = ? OR scope = 'general')",
            (project,),
        )
    else:
        cur = conn.execute(
            "SELECT * FROM reflections "
            "WHERE status IN ('pending_review', 'confirmed')"
        )
    return cur.fetchall()


def _retired_reflection_ids(conn: Any, project: str | None) -> list[str]:
    if project:
        cur = conn.execute(
            "SELECT id FROM reflections "
            "WHERE status IN ('retired', 'superseded') "
            "  AND (project = ? OR scope = 'general')",
            (project,),
        )
    else:
        cur = conn.execute(
            "SELECT id FROM reflections WHERE status IN ('retired', 'superseded')"
        )
    return [r["id"] for r in cur.fetchall()]


def _eligible_semantic_rows(conn: Any, project: str | None) -> list[Any]:
    if project:
        cur = conn.execute(
            "SELECT * FROM semantic_memories WHERE (project = ? OR scope = 'general')",
            (project,),
        )
    else:
        cur = conn.execute("SELECT * FROM semantic_memories")
    return cur.fetchall()


def _map_batch_response(
    batch: list[dict[str, Any]], response: dict[str, Any]
) -> list[tuple[dict[str, Any], str | None, str | None]]:
    """Zip a batch response back to its submitted records.

    Returns ``(record, minted_id, error)`` triples: exactly one of
    ``minted_id`` / ``error`` is set. Maps by ``requestIdentifier`` when AWS
    echoes it (the real behaviour), else falls back to submission order.
    """
    successful = response.get("successfulRecords") or []
    failed = response.get("failedRecords") or []
    triples: list[tuple[dict[str, Any], str | None, str | None]] = []

    have_req = bool(successful or failed) and all(
        "requestIdentifier" in r for r in (*successful, *failed)
    )
    if have_req:
        by_req = {r["requestIdentifier"]: r for r in batch}
        for s in successful:
            triples.append(
                (by_req[s["requestIdentifier"]], s.get("memoryRecordId"), None)
            )
        for f in failed:
            triples.append(
                (by_req[f["requestIdentifier"]], None,
                 f.get("errorMessage", "unknown"))
            )
        return triples

    # Positional fallback (submission order preserved): successes first.
    for i, s in enumerate(successful):
        triples.append((batch[i], s.get("memoryRecordId"), None))
    for j, f in enumerate(failed):
        triples.append(
            (batch[len(successful) + j], None, f.get("errorMessage", "unknown"))
        )
    return triples


def _handle_migrate(args: argparse.Namespace) -> int:
    from better_memory.storage import agentcore_migrate as _mig

    home = _resolve_home(args.home)

    includes = {s.strip() for s in args.include.split(",") if s.strip()}
    unknown = includes - _VALID_INCLUDE_KINDS
    if unknown:
        print(
            f"unknown --include kind(s): {', '.join(sorted(unknown))}; "
            f"valid: {', '.join(sorted(_VALID_INCLUDE_KINDS))}",
            file=sys.stderr,
        )
        return 1
    if "observations" in includes:
        print(
            "WARNING: --include=observations is lossy and non-retrievable — "
            "migrated observation events are written under a synthetic session "
            "id that cross-session retrieval cannot re-read, and "
            "reinforcement_score has no event-plane home. Observation replay is "
            "NOT performed; migrate treats observations as already distilled "
            "into reflections.",
            file=sys.stderr,
        )
    need_reflections = "reflections" in includes
    need_semantic = "semantic" in includes
    if not need_reflections and not need_semantic:
        print(
            "nothing to migrate: --include names no supported kind "
            "(reflections, semantic).",
            file=sys.stderr,
        )
        return 1

    cfg = load_agentcore_config(home)
    if cfg is None and not args.provision:
        print(
            f"No agentcore.json found at {home / 'agentcore.json'}. "
            f"Run `better-memory agentcore init` first, or pass --provision.",
            file=sys.stderr,
        )
        return 1

    db_path = Path(args.db).expanduser() if args.db else home / "memory.db"
    if not db_path.exists():
        print(f"Source SQLite db not found at {db_path}.", file=sys.stderr)
        return 1

    strategy_reflections = cfg.episodic.strategy_id if cfg else ""
    strategy_semantic = cfg.semantic.strategy_id if cfg else ""

    # ------------------------------------------------------------------ #
    # Build the per-row plan from SQLite + the ledger. No AWS yet.
    # ------------------------------------------------------------------ #
    from better_memory.db.connection import connect as _db_connect

    conn = _db_connect(db_path)
    try:
        _mig.ensure_ledger(conn)

        # ---------------------------------------------------------------- #
        # Phase A — build the per-row records (no ledger decision yet). The
        # strategy id may still be a placeholder ('' when cfg is None and we
        # will provision); it does not feed the content hash (§5.2) and is
        # re-keyed to the real id before any write.
        # ---------------------------------------------------------------- #
        # planned_raw[kind] = list of (row, record, content_hash, namespace)
        planned_raw: dict[str, list[tuple]] = {"reflection": [], "semantic": []}

        if need_reflections:
            for row in _eligible_reflection_rows(conn, args.project):
                rec = _mig.build_reflection_record(
                    row, strategy_id=strategy_reflections
                )
                if rec is None:  # defensive — query already filters status
                    continue
                chash = _mig.canonical_content_hash(rec)
                planned_raw["reflection"].append(
                    (row, rec, chash, rec["namespaces"][0])
                )

        if need_semantic:
            for row in _eligible_semantic_rows(conn, args.project):
                rec = _mig.build_semantic_record(
                    row, strategy_id=strategy_semantic
                )
                chash = _mig.canonical_content_hash(rec)
                planned_raw["semantic"].append(
                    (row, rec, chash, rec["namespaces"][0])
                )

        def _decide() -> tuple[dict, dict, dict]:
            """Compute create/update/skip/retire decisions from the CURRENT
            ledger state (re-runnable after reconcile mutates the ledger)."""
            tallies: dict[tuple[str, str], dict[str, int]] = {}

            def _bump(kind: str, namespace: str, action: str) -> None:
                slot = tallies.setdefault(
                    (kind, namespace),
                    {"create": 0, "update": 0, "skip": 0, "retire": 0},
                )
                slot[action] += 1

            # planned[kind] = (row, record, content_hash, decision, namespace)
            planned: dict[str, list[tuple]] = {"reflection": [], "semantic": []}
            retires: dict[str, list[tuple[str, str]]] = {"reflection": []}

            for kind in ("reflection", "semantic"):
                for row, rec, chash, ns in planned_raw[kind]:
                    decision = _mig.plan_row(conn, kind, row["id"], chash)
                    if args.restart and decision == "skip":
                        decision = "update"
                    planned[kind].append((row, rec, chash, decision, ns))
                    _bump(kind, ns, decision)

            if need_reflections:
                for retired_id in _retired_reflection_ids(conn, args.project):
                    decision = _mig.plan_row(conn, "reflection", retired_id, None)
                    if decision == "retire":
                        ledger = conn.execute(
                            "SELECT namespace, target_record_id FROM "
                            "agentcore_migration WHERE source_kind = 'reflection' "
                            "AND source_id = ?",
                            (str(retired_id),),
                        ).fetchone()
                        ns = ledger["namespace"] if ledger else ""
                        target = ledger["target_record_id"] if ledger else None
                        if target:
                            retires["reflection"].append((str(retired_id), target))
                            _bump("reflection", ns, "retire")

            return planned, retires, tallies

        # ---------------------------------------------------------------- #
        # Dry-run: decide from the ledger, print tallies, ZERO AWS calls.
        # (No remote reconcile — dry-run may not touch AWS; its tallies are a
        # best-effort estimate from local state.)
        # ---------------------------------------------------------------- #
        if args.dry_run:
            _, _, tallies = _decide()
            _print_migration_plan(tallies, dry_run=True)
            return 0

        # ---------------------------------------------------------------- #
        # Real run. Build clients INSIDE the try so auth/region failures
        # land on rc=1 (smoke pattern).
        # ---------------------------------------------------------------- #
        region = args.region or (cfg.region if cfg else "eu-west-2")
        try:
            control = _build_control_client(region)
            data = _build_data_client(region)

            if cfg is None:
                # --provision with no config: create both fresh, persist.
                created_ids: list[str] = []
                episodic = _create_one_memory(
                    control,
                    name=DEFAULT_EPISODIC_NAME,
                    strategy_block=episodic_strategy_block(),
                    strategy_name=DEFAULT_EPISODIC_STRATEGY_NAME,
                    event_expiry_days=DEFAULT_EPISODIC_EVENT_EXPIRY_DAYS,
                    label="episodic",
                    created_ids=created_ids,
                )
                semantic = _create_one_memory(
                    control,
                    name=DEFAULT_SEMANTIC_NAME,
                    strategy_block=semantic_strategy_block(),
                    strategy_name=DEFAULT_SEMANTIC_STRATEGY_NAME,
                    event_expiry_days=DEFAULT_SEMANTIC_EVENT_EXPIRY_DAYS,
                    label="semantic",
                    created_ids=created_ids,
                )
                cfg = AgentCoreConfig(
                    schema_version=1, region=region,
                    semantic=semantic, episodic=episodic,
                )
                save_agentcore_config(cfg, home)

            # Verify / provision targets. This may MINT REPLACEMENTS and return
            # an updated cfg (new memory + strategy ids), so re-read the strategy
            # ids from the returned cfg afterwards.
            cfg, _epi_mem, _sem_mem = _resolve_target_memories(
                control, cfg, home,
                provision=args.provision,
                need_reflections=need_reflections,
                need_semantic=need_semantic,
            )
            if need_semantic:
                _ensure_semantic_schema(control, cfg, _sem_mem)

            # Re-key planned records to the FINAL strategy ids (covers both the
            # no-config fresh-provision path and a --provision replacement).
            # Hash is strategy-independent (§5.2), so this does not change any
            # content_hash or ledger decision.
            planned_raw = _rekey_strategy(
                planned_raw, cfg.episodic.strategy_id, cfg.semantic.strategy_id
            )

            # §5.3 ledger-loss safety net: reconcile existing remote records by
            # source_row_id BEFORE deciding, so a lost/partial ledger reattaches
            # target ids (yielding 'update') instead of re-creating duplicates.
            _reconcile_from_remote(
                conn, data, cfg, planned_raw,
                need_reflections=need_reflections,
                need_semantic=need_semantic,
            )
        except RuntimeError as exc:
            print(f"migrate FAILED: {exc}", file=sys.stderr)
            return 1
        except Exception as exc:  # noqa: BLE001 — auth/region/client failures
            print(f"migrate FAILED: {exc!r}", file=sys.stderr)
            return 1

        # Decide AFTER reconcile so reattached targets drive update-not-create.
        planned, retires, tallies = _decide()

        try:
            lock_fd = _acquire_migration_lock(home)
        except FileExistsError:
            print(
                f"Another migrate run holds the lock "
                f"({home / 'agentcore_migration.lock'}). Wait for it to "
                f"finish or delete the stale lock file.",
                file=sys.stderr,
            )
            return 1

        failures = 0
        try:
            failures += _execute_kind(
                conn, data, "reflection",
                memory_id=cfg.episodic.memory_id,
                planned=planned["reflection"],
                retires=retires["reflection"],
                batch_size=args.batch_size,
            )
            failures += _execute_kind(
                conn, data, "semantic",
                memory_id=cfg.semantic.memory_id,
                planned=planned["semantic"],
                retires=[],
                batch_size=args.batch_size,
            )

            if args.verify:
                _verify_sample(conn, data, cfg)
        finally:
            _release_migration_lock(home, lock_fd)

        _print_migration_plan(tallies, dry_run=False)
        if failures:
            print(
                f"\nmigrate completed with {failures} failed record(s); "
                f"they are marked 'failed' in the ledger and a re-run will "
                f"retry them.",
                file=sys.stderr,
            )
            return 2
        print("\nmigrate: all eligible rows converged.")
        return 0
    finally:
        conn.close()


def _rekey_strategy(
    planned_raw: dict[str, list[tuple]],
    strategy_reflections: str,
    strategy_semantic: str,
) -> dict[str, list[tuple]]:
    """Rewrite planned records' ``memoryStrategyId`` to the real strategy ids
    after provisioning (fresh, or a --provision replacement).

    Operates on the phase-A ``(row, record, content_hash, namespace)`` tuples.
    The content_hash is deliberately preserved: ``canonical_content_hash``
    excludes ``memoryStrategyId`` (§5.2), so re-keying must NOT change the hash
    — otherwise the ledger's stored hash would never match a later run's hash,
    forcing a spurious update every run."""
    out: dict[str, list[tuple]] = {"reflection": [], "semantic": []}
    for row, rec, chash, ns in planned_raw["reflection"]:
        rec = {**rec, "memoryStrategyId": strategy_reflections}
        out["reflection"].append((row, rec, chash, ns))
    for row, rec, chash, ns in planned_raw["semantic"]:
        rec = {**rec, "memoryStrategyId": strategy_semantic}
        out["semantic"].append((row, rec, chash, ns))
    return out


def _list_namespace_records(
    data: Any, memory_id: str, namespace: str
) -> list[dict[str, Any]]:
    """Page through every record in one namespace via ``list_memory_records``.

    Returns the raw record summaries (each may carry ``memoryRecordId``,
    ``content`` and ``metadata``). Used by the reconcile scan; a client-side
    scan is required because ``source_row_id`` is not a server-indexed key
    (§5.3)."""
    records: list[dict[str, Any]] = []
    next_token: str | None = None
    while True:
        kwargs: dict[str, Any] = {
            "memoryId": memory_id,
            "namespace": namespace,
            "maxResults": 100,
        }
        if next_token:
            kwargs["nextToken"] = next_token
        resp = data.list_memory_records(**kwargs)
        records.extend(resp.get("memoryRecordSummaries") or [])
        next_token = resp.get("nextToken")
        if not next_token:
            break
    return records


def _reflection_source_row_id(record: dict[str, Any]) -> str | None:
    """Extract ``source_row_id`` from a reflection record's JSON content body
    (design §1b: all reflection state lives in the body). Only own migrated
    records (``source_backend='sqlite'``) are eligible (§10 risk 6)."""
    text = (record.get("content") or {}).get("text")
    if not isinstance(text, str):
        return None
    try:
        body = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(body, dict):
        return None
    if body.get("source_backend") != "sqlite":
        return None
    srid = body.get("source_row_id")
    return str(srid) if srid else None


def _semantic_source_row_id(record: dict[str, Any]) -> str | None:
    """Extract ``source_row_id`` from a semantic record's declared metadata
    (design §1b/§3.2: semantic idempotency key is declared metadata)."""
    metadata = record.get("metadata")
    if not isinstance(metadata, dict):
        return None
    entry = metadata.get("source_row_id")
    if not isinstance(entry, dict):
        return None
    srid = entry.get("stringValue")
    return str(srid) if srid else None


def _reconcile_from_remote(
    conn: Any,
    data: Any,
    cfg: AgentCoreConfig,
    planned_raw: dict[str, list[tuple]],
    *,
    need_reflections: bool,
    need_semantic: bool,
) -> int:
    """Client-side reconcile-by-source_row_id (design §5.3, one of the four
    idempotency pillars in §1).

    Scans each target namespace this run would write to, indexes existing
    records by ``source_row_id``, and reattaches the server-minted
    ``target_record_id`` to any ledger row that lacks one. This makes
    ``--restart`` — and any run after a lost/absent ledger — idempotent:
    without it, ``plan_row`` returns 'create' for every eligible row and
    re-creates duplicates (short-window ``requestIdentifier`` dedup, §5.1, is
    no defense across a ledger loss). Returns the number of records reattached.

    Scanned namespaces are exactly those the planned records target, so an
    empty plan performs no list calls.
    """
    from better_memory.storage import agentcore_migrate as _mig

    reattached = 0

    def _scan(kind: str, memory_id: str, extract) -> None:
        nonlocal reattached
        namespaces = {ns for (_row, _rec, _h, ns) in planned_raw[kind]}
        for namespace in sorted(namespaces):
            for record in _list_namespace_records(data, memory_id, namespace):
                srid = extract(record)
                target = record.get("memoryRecordId")
                if not srid or not target:
                    continue
                if _mig.reconcile_ledger(
                    conn, kind=kind, source_id=srid,
                    namespace=namespace, target_record_id=target,
                ):
                    reattached += 1

    if need_reflections:
        _scan("reflection", cfg.episodic.memory_id, _reflection_source_row_id)
    if need_semantic:
        _scan("semantic", cfg.semantic.memory_id, _semantic_source_row_id)

    return reattached


def _execute_kind(
    conn: Any,
    data: Any,
    kind: str,
    *,
    memory_id: str,
    planned: list[tuple],
    retires: list[tuple[str, str]],
    batch_size: int,
) -> int:
    """Execute the create/update/retire plan for one kind. Updates the ledger
    per record; returns the number of failed records (for the exit code)."""
    from better_memory.storage import agentcore_migrate as _mig

    creates = [p for p in planned if p[3] == "create"]
    updates = [p for p in planned if p[3] == "update"]
    failures = 0

    # ---- CREATE (batched) ----
    create_records = [rec for (_row, rec, _h, _d, _ns) in creates]
    meta_by_req = {
        rec["requestIdentifier"]: (row, chash, ns)
        for (row, rec, chash, _d, ns) in creates
    }
    for batch in _mig.chunk(create_records, batch_size):
        try:
            resp = _mig.push_batch(data, memory_id, batch)
        except Exception as exc:  # noqa: BLE001 — whole-batch failure
            for rec in batch:
                row, chash, ns = meta_by_req[rec["requestIdentifier"]]
                _mig.record_failure(
                    conn, kind=kind, source_id=row["id"],
                    last_error=repr(exc), namespace=ns, content_hash=chash,
                )
                failures += 1
            continue
        for rec, minted_id, error in _map_batch_response(batch, resp):
            row, chash, ns = meta_by_req[rec["requestIdentifier"]]
            if error is not None:
                _mig.record_failure(
                    conn, kind=kind, source_id=row["id"],
                    last_error=error, namespace=ns, content_hash=chash,
                )
                failures += 1
            else:
                _mig.record_success(
                    conn, kind=kind, source_id=row["id"], namespace=ns,
                    content_hash=chash, target_record_id=minted_id,
                )

    # ---- UPDATE (batched) ----
    update_records = []
    meta_by_record_id: dict[str, tuple] = {}
    for (row, rec, chash, _d, ns) in updates:
        ledger = conn.execute(
            "SELECT target_record_id FROM agentcore_migration "
            "WHERE source_kind = ? AND source_id = ?",
            (kind, str(row["id"])),
        ).fetchone()
        target = ledger["target_record_id"] if ledger else None
        if not target:
            # No minted id to update against -> re-create instead.
            _mig.record_failure(
                conn, kind=kind, source_id=row["id"],
                last_error="update requested but no target_record_id in ledger",
                namespace=ns, content_hash=chash,
            )
            failures += 1
            continue
        upd = {
            "memoryRecordId": target,
            "timestamp": rec["timestamp"],
            "content": rec["content"],
        }
        if "metadata" in rec:
            upd["metadata"] = rec["metadata"]
        update_records.append(upd)
        meta_by_record_id[target] = (row, chash, ns)

    for batch in _mig.chunk(update_records, batch_size):
        try:
            resp = data.batch_update_memory_records(
                memoryId=memory_id, records=batch
            )
        except Exception as exc:  # noqa: BLE001
            for upd in batch:
                row, chash, ns = meta_by_record_id[upd["memoryRecordId"]]
                _mig.record_failure(
                    conn, kind=kind, source_id=row["id"],
                    last_error=repr(exc), namespace=ns, content_hash=chash,
                )
                failures += 1
            continue
        successful = {
            r.get("memoryRecordId") for r in (resp.get("successfulRecords") or [])
        }
        failed = {
            r.get("memoryRecordId"): r.get("errorMessage", "unknown")
            for r in (resp.get("failedRecords") or [])
        }
        for upd in batch:
            rid = upd["memoryRecordId"]
            row, chash, ns = meta_by_record_id[rid]
            if rid in failed or (successful and rid not in successful):
                _mig.record_failure(
                    conn, kind=kind, source_id=row["id"],
                    last_error=failed.get(rid, "update not confirmed"),
                    namespace=ns, content_hash=chash,
                )
                failures += 1
            else:
                _mig.record_success(
                    conn, kind=kind, source_id=row["id"], namespace=ns,
                    content_hash=chash, target_record_id=rid,
                )

    # ---- RETIRE (read-modify-write body status) ----
    from datetime import UTC, datetime
    for source_id, target in retires:
        try:
            record = data.get_memory_record(
                memoryId=memory_id, memoryRecordId=target
            )["memoryRecord"]
            text = record.get("content", {}).get("text", "")
            body = json.loads(text) if isinstance(text, str) else {}
            if not isinstance(body, dict):
                body = {}
            body["status"] = "retired"
            data.batch_update_memory_records(
                memoryId=memory_id,
                records=[{
                    "memoryRecordId": target,
                    "timestamp": datetime.now(UTC),
                    "content": {"text": json.dumps(body, sort_keys=True)},
                }],
            )
            _mig.record_success(
                conn, kind=kind, source_id=source_id, status="retired",
            )
        except Exception as exc:  # noqa: BLE001
            _mig.record_failure(
                conn, kind=kind, source_id=source_id, last_error=repr(exc),
            )
            failures += 1

    return failures


def _verify_sample(conn: Any, data: Any, cfg: AgentCoreConfig) -> None:
    """Read back one migrated record per kind (read-your-write, retry on
    ResourceNotFoundException) and diff a key field against SQLite."""
    for kind, memory_id in (
        ("reflection", cfg.episodic.memory_id),
        ("semantic", cfg.semantic.memory_id),
    ):
        ledger = conn.execute(
            "SELECT source_id, target_record_id FROM agentcore_migration "
            "WHERE source_kind = ? AND status = 'migrated' "
            "AND target_record_id IS NOT NULL LIMIT 1",
            (kind,),
        ).fetchone()
        if ledger is None:
            continue
        record = None
        for attempt in range(1, 6):
            try:
                record = data.get_memory_record(
                    memoryId=memory_id,
                    memoryRecordId=ledger["target_record_id"],
                )["memoryRecord"]
                break
            except Exception as exc:  # noqa: BLE001
                code = getattr(exc, "response", {}).get(
                    "Error", {}
                ).get("Code", "")
                if code != "ResourceNotFoundException" or attempt == 5:
                    print(
                        f"   verify: could not read back {kind} "
                        f"{ledger['target_record_id']}: {exc!r}",
                        file=sys.stderr,
                    )
                    break
                time.sleep(2)
        if record is not None:
            print(
                f"   verify: {kind} {ledger['target_record_id']} readback ok"
            )


def _print_migration_plan(
    tallies: dict[tuple[str, str], dict[str, int]], *, dry_run: bool
) -> None:
    header = "Migration plan (dry-run — no AWS calls)" if dry_run else \
        "Migration summary"
    print(header)
    totals = {"create": 0, "update": 0, "skip": 0, "retire": 0}
    for (kind, namespace), counts in sorted(tallies.items()):
        print(
            f"  {kind:<10} {namespace:<40} "
            f"create={counts['create']} update={counts['update']} "
            f"skip={counts['skip']} retire={counts['retire']}"
        )
        for action in totals:
            totals[action] += counts[action]
    print(
        f"  {'TOTAL':<10} {'':<40} "
        f"create={totals['create']} update={totals['update']} "
        f"skip={totals['skip']} retire={totals['retire']}"
    )
