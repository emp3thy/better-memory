"""Golden parity: the manifest reproduces the reference machine's wiring.

The fixtures are the managed-relevant subset of the reference PC's live
config. Applying the manifest with that machine's params must change
NOTHING semantically: same hook modules per event, same env key, same MCP
entry, and a CLAUDE.md whose managed block equals the packaged canonical
text. Guards the 'runs as well as it does on this pc' requirement.
"""
import json
from pathlib import Path

from better_memory.setup.engine import (
    extract_managed_block,
    merge_settings,
    patch_mcp_entry,
    splice_managed_block,
)
from better_memory.setup.manifest import CLAUDE_MD_BLOCK, MachineParams

FIXTURES = Path(__file__).parent / "fixtures"
REF_PARAMS = MachineParams(
    venv_py=r"C:\Users\ref\source\better-memory\.venv\Scripts\python.exe",
    venv_pyw=r"C:\Users\ref\source\better-memory\.venv\Scripts\pythonw.exe",
    home=r"C:\Users\ref\.better-memory",
    repo_root=r"C:\Users\ref\source\better-memory",
)


def _modules_by_event(hooks: dict) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for event, groups in hooks.items():
        for group in groups:
            for h in group.get("hooks", []):
                result.setdefault(event, set()).add(h["command"])
    return result


def test_settings_parity_same_commands_per_event():
    ref = json.loads((FIXTURES / "reference_settings.json").read_text("utf-8"))
    merged = merge_settings(json.loads(json.dumps(ref)), REF_PARAMS)
    ref_cmds = _modules_by_event(ref["hooks"])
    new_cmds = _modules_by_event(merged["hooks"])
    # Same events; same better_memory modules per event. The three
    # hand-added entries (loose script + two echoes) are replaced by module
    # equivalents, so compare on module-name level for those.
    assert set(new_cmds) == set(ref_cmds)
    for event in ref_cmds:
        ref_modules = {c for c in ref_cmds[event] if "better_memory" in c}
        new_modules = {c for c in new_cmds[event] if "better_memory" in c}
        assert ref_modules <= new_modules, event
    assert merged["env"]["BETTER_MEMORY_INJECT_MODE"] == "deferred"


def test_mcp_parity_env_preserved():
    ref = json.loads((FIXTURES / "reference_claude.json").read_text("utf-8"))
    patched = patch_mcp_entry(json.loads(json.dumps(ref)), REF_PARAMS)
    entry = patched["mcpServers"]["better-memory"]
    ref_env = ref["mcpServers"]["better-memory"]["env"]
    for key, value in ref_env.items():
        assert entry["env"][key] == value
    assert entry["command"] == REF_PARAMS.venv_py


def test_claude_md_canonical_matches_reference_section():
    ref_md = (FIXTURES / "reference_claude_md.md").read_text("utf-8")
    spliced = splice_managed_block(ref_md, CLAUDE_MD_BLOCK)
    assert extract_managed_block(spliced) == CLAUDE_MD_BLOCK
    # The canonical block's protocol text must equal what the reference
    # machine actually runs with (whitespace-insensitive).
    assert "# better-memory (MANDATORY)" in ref_md
    normalized_ref = " ".join(ref_md.split())
    normalized_block = " ".join(CLAUDE_MD_BLOCK.split())
    assert normalized_block in normalized_ref
