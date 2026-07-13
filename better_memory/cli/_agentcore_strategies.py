"""Memory-strategy definitions used by `agentcore init` and the smoke script.

Lifted from `scripts/agentcore_smoke.py` so the CLI and the smoke share a
single source of truth — diverging the two has bitten us before.
"""

from __future__ import annotations

# Episodic memory: extracts reflections from session events. Metadata schema
# carries the rating counters + polarity classification.
EPISODIC_METADATA_SCHEMA: list[dict] = [
    {
        "key": "polarity",
        "type": "STRING",
        "extractionConfig": {
            "llmExtractionConfig": {
                "definition": (
                    "Whether this reflection prescribes a positive practice "
                    "('do'), warns against a negative practice ('dont'), or "
                    "is informational only ('neutral')."
                ),
                "llmExtractionInstruction": (
                    "Classify this reflection as 'do', 'dont', or 'neutral'."
                ),
                "validation": {
                    "stringValidation": {
                        "allowedValues": ["do", "dont", "neutral"]
                    }
                },
            }
        },
    },
    {"key": "useful_count", "type": "NUMBER"},
    {"key": "missed_count", "type": "NUMBER"},
    {"key": "ignored_count", "type": "NUMBER"},
    {"key": "times_misled", "type": "NUMBER"},
    {"key": "overlooked_count", "type": "NUMBER"},
    {"key": "last_credited_at", "type": "STRING"},
    {"key": "status", "type": "STRING"},
]

# userPreference (semantic) memoryRecordSchema. Unlike the episodic schema
# (which governs only AWS-EXTRACTED records), this top-level schema governs
# CLIENT BASE writes: keys declared here are retained on create, survive
# updates, and are listable; UNDECLARED keys (e.g. source_row_id) are silently
# dropped by AWS (design §1b, proven live). The migration idempotency key
# source_row_id therefore MUST be declared here or semantic reconcile-by-
# source_row_id (design §3.2/§5.3) breaks — the migrator could not detect an
# already-migrated row and would duplicate it on re-run.
#
# OPERATIONAL NOTE for the migrate/--provision path (T5): this schema is baked
# into the userPreferenceMemoryStrategy block at CREATE_MEMORY time. A semantic
# memory PROVISIONED BEFORE source_row_id was added here does NOT have it
# declared, so client writes of source_row_id there are still dropped. Before
# writing, the migrate/--provision path must guarantee the target's live schema
# declares these keys — either widen it in place via update_memory_strategy
# (widening the metadataSchema; verify AWS permits post-hoc schema extension —
# unconfirmed as of §1b's open item) or re-provision a fresh semantic memory.
# Fresh `init` provisions with this (correct) schema and needs no migration.
SEMANTIC_METADATA_SCHEMA: list[dict] = [
    {"key": "useful_count", "type": "NUMBER"},
    {"key": "missed_count", "type": "NUMBER"},
    {"key": "ignored_count", "type": "NUMBER"},
    {"key": "times_misled", "type": "NUMBER"},
    {"key": "overlooked_count", "type": "NUMBER"},
    {"key": "last_credited_at", "type": "STRING"},
    {"key": "status", "type": "STRING"},
    # Migration idempotency key (design §3.2). STRING: it holds the SQLite row
    # id. Not server-indexed (indexed keys are frozen at provision, §5.3) —
    # reconcile scans it client-side.
    {"key": "source_row_id", "type": "STRING"},
]

INDEXED_KEYS: list[dict] = [
    {"key": "status", "type": "STRING"},
    {"key": "last_credited_at", "type": "STRING"},
    {"key": "overlooked_count", "type": "NUMBER"},
]

# Names — must match the AWS regex `[a-zA-Z][a-zA-Z0-9_]{0,47}` (no dashes!).
DEFAULT_EPISODIC_NAME = "better_memory_episodic"
DEFAULT_SEMANTIC_NAME = "better_memory_semantic"
DEFAULT_EPISODIC_STRATEGY_NAME = "episodicReflections"
DEFAULT_SEMANTIC_STRATEGY_NAME = "userPreference"

# Event TTL: episodic events are kept ~90 days (long enough to span a multi-
# month project); semantic records last ~365 days.
DEFAULT_EPISODIC_EVENT_EXPIRY_DAYS = 90
DEFAULT_SEMANTIC_EVENT_EXPIRY_DAYS = 365


def episodic_strategy_block(
    *,
    name: str = DEFAULT_EPISODIC_STRATEGY_NAME,
) -> dict:
    return {
        "episodicMemoryStrategy": {
            "name": name,
            "namespaces": ["projects/{actorId}/reflections/"],
            "namespaceTemplates": ["projects/{actorId}/reflections/"],
            "reflectionConfiguration": {
                "namespaces": ["projects/{actorId}/reflections/"],
                "namespaceTemplates": ["projects/{actorId}/reflections/"],
                "memoryRecordSchema": {
                    "metadataSchema": EPISODIC_METADATA_SCHEMA
                },
            },
        }
    }


def semantic_strategy_block(
    *,
    name: str = DEFAULT_SEMANTIC_STRATEGY_NAME,
) -> dict:
    return {
        "userPreferenceMemoryStrategy": {
            "name": name,
            "namespaces": ["projects/{actorId}/semantic/"],
            "namespaceTemplates": ["projects/{actorId}/semantic/"],
            "memoryRecordSchema": {
                "metadataSchema": SEMANTIC_METADATA_SCHEMA
            },
        }
    }
