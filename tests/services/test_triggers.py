"""Tests for the pure trigger grammar, validator and matcher."""
from __future__ import annotations

import pytest

from better_memory.services.triggers import match, parse_triggers, validate_triggers

LARGE = 8000


def _m(triggers, tool, inp):
    return match(triggers, tool, inp, large_write_chars=LARGE)


def test_tool_trigger_exact_name():
    assert _m(["tool:WebFetch"], "WebFetch", {}) == "tool:WebFetch"
    assert _m(["tool:WebFetch"], "webfetch", {}) is None


def test_skill_trigger_exact_and_prefix():
    assert _m(["skill:superpowers:writing-plans"], "Skill",
              {"skill": "superpowers:writing-plans"}) == "skill:superpowers:writing-plans"
    assert _m(["skill:superpowers:*"], "Skill",
              {"skill": "superpowers:brainstorming"}) == "skill:superpowers:*"
    assert _m(["skill:superpowers:writing-plans"], "Skill",
              {"skill": "superpowers:brainstorming"}) is None
    assert _m(["skill:superpowers:*"], "Bash", {"command": "superpowers:x"}) is None


def test_bash_trigger_substring_case_insensitive():
    assert _m(["bash:<<"], "Bash", {"command": "cat > f <<'EOF'\nx\nEOF"}) == "bash:<<"
    assert _m(["bash:GIT PUSH"], "Bash", {"command": "git push origin"}) == "bash:GIT PUSH"
    assert _m(["bash:<<"], "Write", {"content": "<<"}) is None


def test_path_trigger_normalises_separators():
    t = ["path:docs/superpowers/specs"]
    assert _m(t, "Write", {"file_path": r"C:\Users\x\docs\superpowers\specs\a.md"}) == t[0]
    assert _m(t, "Read", {"file_path": "/home/x/docs/superpowers/specs/a.md"}) == t[0]
    assert _m(t, "Edit", {"file_path": "/home/x/DOCS/Superpowers/specs/a.md"}) == t[0]
    assert _m(t, "Bash", {"command": "docs/superpowers/specs"}) is None


def test_write_large_threshold():
    assert _m(["write:large"], "Write", {"content": "x" * 8001}) == "write:large"
    assert _m(["write:large"], "Write", {"content": "x" * 8000}) is None
    assert _m(["write:large"], "Edit", {"new_string": "x" * 9000}) is None


def test_first_matching_trigger_wins():
    assert _m(["tool:Agent", "bash:<<"], "Bash", {"command": "<<"}) == "bash:<<"
    assert _m(["bash:<<", "tool:Bash"], "Bash", {"command": "ls"}) == "tool:Bash"


def test_non_string_inputs_never_match_or_raise():
    assert _m(["bash:<<"], "Bash", {"command": None}) is None
    assert _m(["write:large"], "Write", {"content": {"a": 1}}) is None
    assert _m(["path:x"], "Write", {}) is None
    assert _m(["skill:a"], "Skill", {"skill": 5}) is None
    assert _m(["bash:<<"], "Bash", None) is None  # type: ignore[arg-type]


def test_unknown_trigger_strings_are_ignored_by_match():
    # A corrupt stored value must never fire or raise at serve time.
    assert _m(["bogus:x", "tool:Bash"], "Bash", {}) == "tool:Bash"


def test_validate_accepts_grammar_and_cleans():
    assert validate_triggers([" tool:WebFetch ", "", "bash:<<", "tool:WebFetch"]) == [
        "tool:WebFetch", "bash:<<",
    ]
    assert validate_triggers(["write:large"]) == ["write:large"]
    assert validate_triggers([]) == []


@pytest.mark.parametrize(
    "bad", ["webfetch", "tool:", "write:small", "agent:x", "skill:", "bash:", "path:"],
)
def test_validate_rejects_unknown(bad):
    with pytest.raises(ValueError, match="invalid trigger"):
        validate_triggers([bad])


@pytest.mark.parametrize("raw", [None, "", "not json", "{}", '["ok", 3]', "[1]", '"str"'])
def test_parse_triggers_tolerates_garbage(raw):
    assert parse_triggers(raw) == []


def test_parse_triggers_roundtrip():
    assert parse_triggers('["tool:WebFetch", "bash:<<"]') == ["tool:WebFetch", "bash:<<"]
