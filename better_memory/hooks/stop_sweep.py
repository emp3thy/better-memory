"""Stop hook: reminder to sweep session observations into better-memory."""
from __future__ import annotations

import json
import sys


def main() -> None:
    try:
        sys.stdin.read()
    except BaseException:  # noqa: BLE001
        pass
    print(json.dumps({
        "systemMessage": (
            "MEMORY SWEEP: record any non-obvious observations from this "
            "session before stopping. See CLAUDE.md mandatory triggers — "
            "review-fix commits, phase boundaries, reviewer-flagged bugs."
        ),
    }), flush=True)


if __name__ == "__main__":
    main()
