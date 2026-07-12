# AgentCore troubleshooting

Errors you'll likely hit when running better-memory in `agentcore` mode, and what to do about them.

## `ValidationException: Memory name does not match required pattern`

AgentCore memory names must match `[a-zA-Z][a-zA-Z0-9_]{0,47}` — letters, digits, and underscores only, **no dashes**. `better-memory agentcore init` uses safe defaults (`better_memory_episodic` and `better_memory_semantic`); if you've edited `agentcore.json` by hand, check both names match the regex.

## `ResourceNotFoundException` on a fresh write/update

AgentCore has roughly 10 seconds of indexing lag between `BatchCreateMemoryRecords` and the new record being mutable. The backend retries `batch_update_memory_records` calls automatically (`_retry_on_transient_404`, 3 attempts, 10s backoff) — if you see this error escaping into MCP tool responses, the record genuinely doesn't exist (wrong memory ID, deleted, or namespace mismatch).

## `400 — Metadata keys cannot use reserved names or prefixes`

AgentCore reserves the `x-amz-agentcore-memory-*` metadata namespace. The backend strips system-managed keys from update payloads before sending (`_full_metadata_snapshot`), but if you're constructing a record by hand via the smoke or a test, do the same: skip any key whose name starts with `x-amz-agentcore-memory-`.

## `NoCredentialsError` or `Unable to locate credentials`

boto3 didn't find AWS credentials. Standard discovery order:

1. `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` env vars
2. `~/.aws/credentials` (named profile via `AWS_PROFILE`)
3. EC2/EKS instance role
4. ECS task role

If you're on a laptop, `aws configure` is the fastest fix. If you're in a container, mount credentials or attach a task role.

## `ResourceConflictException: A memory with this name already exists`

`init` refuses to create a duplicate memory. Either:

- Delete the existing memory via the AWS console and re-run, **or**
- Manually populate `agentcore.json` with the existing memory's IDs (see the file format in `better_memory/storage/agentcore_persistence.py`).

## `MEMORY_FAILED` after `init`

Memory creation entered a terminal `FAILED` state. Check the AWS console for the actual failure reason (strategy-config errors, Bedrock region availability). Common cause: requesting a region where AgentCore Memory isn't GA yet. Stick to `eu-west-2` unless you've verified GA elsewhere.

## Closure events not firing

In agentcore mode, the Stop hook emits a `CreateEvent(role=OTHER)` to tell the episodic strategy "this session is done, extract now." If episodic extraction is taking 15+ minutes instead of 1-3, check:

- The effective backend is actually `agentcore`: run `better-memory agentcore status` and read the `effective backend: <backend> (source: env|settings|default)` line. The hook resolves the backend exactly like the server — `BETTER_MEMORY_STORAGE_BACKEND` env var first, else `settings.json`, else `sqlite`. A stale `BETTER_MEMORY_STORAGE_BACKEND=sqlite` in your shell or `~/.claude.json` env block **overrides** the `settings.json` written by `init` — unset it or set it to `agentcore`.
- `settings.json` carries `"storage_backend": "agentcore"`. `agentcore.json` existing on its own does **not** activate the closure — existence is not consent.
- `~/.better-memory/settings.json` and `agentcore.json` exist at session-close time. Hooks don't receive the `BETTER_MEMORY_HOME` the installer puts into the MCP server's env block, so they fall back to `~/.better-memory` — if you ran `init --home <custom>`, the hook won't find either file there.
- The IAM principal has `bedrock-agentcore:CreateEvent` permission.
- The Stop hook's error log (`hook_errors` table in `memory.db` — or stdout if running interactively) shows no `session_close_agentcore` entries.

Failure to fire is non-fatal; the episodic strategy still triggers eventually via 15-20 minute idle detection.

## `ModuleNotFoundError: boto3 is required for the agentcore storage backend`

The full message is:

```
boto3 is required for the agentcore storage backend. Install it with: pip install 'better-memory[agentcore]'
```

The agentcore backend's AWS dependencies live in an optional extra so sqlite-only installs stay lean. Install the extra into the same environment the MCP server (or hook) runs from: `pip install 'better-memory[agentcore]'` or `uv pip install '.[agentcore]'`. Both the server factory and the Stop hook's lazy import raise this same hint.

## `AgentCoreConfigError` — corrupt or unreadable `agentcore.json`

Parse failures, a missing required field, a malformed `semantic`/`episodic` block, or an unsupported `schema_version` all raise `AgentCoreConfigError` naming the file, ending with:

```
Delete the file and re-run `better-memory agentcore init` (or use `--force`); existing AWS memories can be re-linked by hand-editing the file.
```

`init` without `--force` refuses to run while the file exists, so either delete it first or pass `--force`. Re-running `init` creates **new** AWS memories; if you want to keep the existing ones, fix the file by hand instead (schema in `better_memory/storage/agentcore_persistence.py`). The `schema_version` variant additionally notes the file may have been written by a newer better-memory.

## Corrupt `settings.json`

A malformed `settings.json` (or an invalid `storage_backend` value in it) raises a `ValueError` naming the file:

```
Fix or delete the file, or set BETTER_MEMORY_STORAGE_BACKEND to override it. It is written by `better-memory agentcore init`.
```

`better-memory agentcore status` survives this (it prints a `WARN` and keeps reporting), and re-running `init` rewrites the file. Hooks record the error to `hook_errors` and degrade to doing nothing rather than failing.

## Region mismatch — events written but never extracted

`init` writes the region to `agentcore.json`, and that file is the single source of truth: the MCP server's clients and the Stop hook's closure client all build against it, so the server and hooks can no longer disagree about region. To change region, re-run `init` in the new region (`--force`, or delete `agentcore.json` first) — or hand-edit `agentcore.json` if the memories genuinely live elsewhere. There is no region environment variable.
