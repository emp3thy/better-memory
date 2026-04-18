"""Module entry point — ``python -m better_memory.mcp``."""

from __future__ import annotations

import asyncio

from better_memory.mcp.server import run


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
