import json

from better_memory.setup.engine import (
    TargetPaths,
    diff,
    extract_managed_block,
    fingerprint,
    merge_settings,
    patch_mcp_entry,
    render,
    splice_managed_block,
)
from better_memory.setup.manifest import (
    BEGIN_MARKER,
    CLAUDE_MD_BLOCK,
    END_MARKER,
    MachineParams,
)

PARAMS = MachineParams(
    venv_py="/repo/.venv/bin/python", venv_pyw="/repo/.venv/bin/python",
    home="/home/u/.better-memory", repo_root="/repo",
)


def test_merge_settings_preserves_foreign_hooks_and_env():
    existing = {
        "env": {"OTHER": "1"},
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo hi"}]}]},
        "model": "fable",
    }
    merged = merge_settings(existing, PARAMS)
    assert merged["model"] == "fable"
    assert merged["env"]["OTHER"] == "1"
    assert merged["env"]["BETTER_MEMORY_INJECT_MODE"] == "deferred"
    stop_cmds = [h["command"] for g in merged["hooks"]["Stop"] for h in g["hooks"]]
    assert "echo hi" in stop_cmds
    assert any("session_close" in c for c in stop_cmds)
    assert any("stop_sweep" in c for c in stop_cmds)


def test_merge_settings_scrubs_legacy_loose_script_and_echo_hooks():
    existing = {"hooks": {
        "PreCompact": [{"hooks": [{"type": "command",
            "command": 'python "C:\\u\\.claude\\hooks\\pre-compact-better-memory.py"'}]}],
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command",
            "command": "echo '{...MEMORY CHECKPOINT before commit...}'"}]}],
    }}
    merged = merge_settings(existing, PARAMS)
    pre_compact_cmds = [h["command"] for g in merged["hooks"]["PreCompact"]
                        for h in g["hooks"]]
    assert all("pre-compact-better-memory.py" not in c for c in pre_compact_cmds)
    assert any("better_memory.hooks.pre_compact" in c for c in pre_compact_cmds)
    pretool_cmds = [h["command"] for g in merged["hooks"]["PreToolUse"]
                    for h in g["hooks"]]
    assert all("MEMORY CHECKPOINT" not in c for c in pretool_cmds)
    assert any("commit_checkpoint" in c for c in pretool_cmds)


def test_merge_settings_is_idempotent():
    once = merge_settings({}, PARAMS)
    twice = merge_settings(json.loads(json.dumps(once)), PARAMS)
    assert once == twice


def test_patch_mcp_entry_touches_only_our_key():
    existing = {"mcpServers": {"other": {"type": "http"}}, "projects": {"x": 1}}
    patched = patch_mcp_entry(existing, PARAMS)
    assert patched["mcpServers"]["other"] == {"type": "http"}
    assert patched["projects"] == {"x": 1}
    assert patched["mcpServers"]["better-memory"]["command"] == PARAMS.venv_py


def test_patch_mcp_entry_preserves_user_env_extras():
    existing = {"mcpServers": {"better-memory": {
        "type": "stdio", "command": "old", "args": [],
        "env": {"BETTER_MEMORY_EMBED_LOG": "1"},
    }}}
    patched = patch_mcp_entry(existing, PARAMS)
    env = patched["mcpServers"]["better-memory"]["env"]
    assert env["BETTER_MEMORY_EMBED_LOG"] == "1"
    assert env["BETTER_MEMORY_HOME"] == PARAMS.home


def test_splice_appends_block_when_absent_and_replaces_when_stale():
    doc = "# Global Preferences\n\n- No whimsy.\n"
    spliced = splice_managed_block(doc, CLAUDE_MD_BLOCK)
    assert spliced.startswith(doc)
    assert extract_managed_block(spliced) == CLAUDE_MD_BLOCK
    stale = spliced.replace("MANDATORY", "OPTIONAL")
    healed = splice_managed_block(stale, CLAUDE_MD_BLOCK)
    assert extract_managed_block(healed) == CLAUDE_MD_BLOCK
    assert healed.count(BEGIN_MARKER) == 1 and healed.count(END_MARKER) == 1


def test_splice_absorbs_legacy_unmarked_section():
    doc = (
        "# Global Preferences\n\n- No whimsy.\n\n"
        + CLAUDE_MD_BLOCK
        + "\n# Process Discipline\n\nrules\n"
    )
    spliced = splice_managed_block(doc, CLAUDE_MD_BLOCK)
    assert spliced.count("# better-memory (MANDATORY)") == 1
    assert spliced.count(BEGIN_MARKER) == 1
    assert spliced.count(END_MARKER) == 1
    assert extract_managed_block(spliced) == CLAUDE_MD_BLOCK
    assert "# Global Preferences" in spliced
    assert "- No whimsy." in spliced
    assert "# Process Discipline" in spliced
    assert "rules" in spliced


def test_splice_absorb_is_idempotent():
    doc = (
        "# Global Preferences\n\n- No whimsy.\n\n"
        + CLAUDE_MD_BLOCK
        + "\n# Process Discipline\n\nrules\n"
    )
    once = splice_managed_block(doc, CLAUDE_MD_BLOCK)
    twice = splice_managed_block(once, CLAUDE_MD_BLOCK)
    assert once == twice


def test_fingerprint_stable_and_param_sensitive():
    assert fingerprint(PARAMS) == fingerprint(PARAMS)
    other = MachineParams(venv_py="/x", venv_pyw="/x", home="/h", repo_root="/r")
    assert fingerprint(PARAMS) != fingerprint(other)


def test_diff_reports_missing_wiring(tmp_path):
    paths = TargetPaths(
        claude_json=tmp_path / ".claude.json",
        settings_json=tmp_path / "settings.json",
        claude_md=tmp_path / "CLAUDE.md",
        skills_dir=tmp_path / "skills",
    )
    drifts = diff(PARAMS, paths)
    assert any("mcp" in d.lower() for d in drifts)
    assert any("hook" in d.lower() for d in drifts)
    assert any("claude.md" in d.lower() for d in drifts)


def test_diff_empty_after_render_applied(tmp_path):
    paths = TargetPaths(
        claude_json=tmp_path / ".claude.json",
        settings_json=tmp_path / "settings.json",
        claude_md=tmp_path / "CLAUDE.md",
        skills_dir=tmp_path / "skills",
    )
    paths.claude_json.write_text(
        json.dumps(patch_mcp_entry({}, PARAMS)), encoding="utf-8")
    paths.settings_json.write_text(
        json.dumps(merge_settings({}, PARAMS)), encoding="utf-8")
    paths.claude_md.write_text(
        splice_managed_block("", CLAUDE_MD_BLOCK), encoding="utf-8")
    drifts = diff(PARAMS, paths)
    assert [d for d in drifts if "skill" not in d.lower()] == []


def test_diff_reports_drift_for_non_dict_settings_json_without_raising(tmp_path):
    paths = TargetPaths(
        claude_json=tmp_path / ".claude.json",
        settings_json=tmp_path / "settings.json",
        claude_md=tmp_path / "CLAUDE.md",
        skills_dir=tmp_path / "skills",
    )
    paths.settings_json.write_text("5", encoding="utf-8")
    drifts = diff(PARAMS, paths)
    assert any(
        "settings.json" in d and "unparseable" in d.lower() for d in drifts
    )


def test_diff_reports_drift_for_non_dict_claude_json_without_raising(tmp_path):
    paths = TargetPaths(
        claude_json=tmp_path / ".claude.json",
        settings_json=tmp_path / "settings.json",
        claude_md=tmp_path / "CLAUDE.md",
        skills_dir=tmp_path / "skills",
    )
    paths.claude_json.write_text("[1, 2, 3]", encoding="utf-8")
    drifts = diff(PARAMS, paths)
    assert any(
        ".claude.json" in d and "unparseable" in d.lower() for d in drifts
    )
