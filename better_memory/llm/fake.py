"""In-memory fake :class:`ChatCompleter` for tests.

Behaviour: pops a canned response per call. Raises if called more times
than the test seeded.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FakeChat:
    """Pops from ``responses`` on each ``complete`` call."""

    responses: list[str]
    calls: list[str] = field(default_factory=list)

    async def complete(self, prompt: str) -> str:
        self.calls.append(prompt)
        if not self.responses:
            raise AssertionError(
                f"FakeChat ran out of responses; last prompt:\n{prompt}"
            )
        return self.responses.pop(0)
