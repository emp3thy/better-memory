---
id: chore-agentcore-backend-error-path-tests-2026-06-10
type: chore
status: inbox
severity: high
attempts: 0
depends_on: []
target_repo: https://github.com/emp3thy/better-memory
source_design: C:\Users\gethi\source\better-memory\.tech-debt\design.md
category: test-gaps
debt_type: test
effort: M
---

# AgentCoreBackend observe/retrieve error paths and malformed JSON untested

### Reasoning

Merged three findings on the same root cause: tests/storage/test_agentcore_unit.py only exercises happy paths of the boto3-backed backend. agentcore.py is a hotspot (2308 complexity) and sits on the network boundary where ClientError, timeouts, and malformed records are the realistic failure modes. Effort M, high confidence â€” deliverable soon with mocked boto3.

### Evidence

- `better_memory/storage/agentcore.py:89` - observe() wraps boto3 create_event in a thread pool; no tests for ClientError, timeout, or invalid session_id exception propagation.
- `better_memory/storage/agentcore.py:230` - ThreadPoolExecutor.result() on futures dict can raise from list_memory_records; no test verifies partial or total polarity-fetch failure handling.
- `better_memory/storage/agentcore.py:254` - json.JSONDecodeError silently returns empty dict for malformed reflection content; graceful degradation never verified.
- `tests/storage/test_agentcore_unit.py:80` - Tests cover only happy path and no-op cases; ClientError/transport/thread-pool failures not exercised.

### Suggested fix

Add unit tests with mocked boto3 client and executor: ClientError variants and timeouts in observe(); one-polarity-fails, all-fail, and worker-timeout scenarios in retrieve(); _parse_reflection_record with malformed JSON asserting the empty-dict fallback yields a valid output shape; missing session_id validation.
