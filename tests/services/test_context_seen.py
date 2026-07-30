from datetime import UTC, datetime

from better_memory.services.context_seen import SeenStore, prune_stale


def _store(tmp_path):
    return SeenStore(tmp_path, "sess-1")


def test_first_exposure_passes_then_suppressed(tmp_path):
    s = _store(tmp_path)
    s.bump_turn()
    ids = [("reflection", "r1"), ("semantic", "m1")]
    assert s.filter_unseen(ids, reinject_turns=0) == ids
    s.mark_seen(ids)
    s2 = _store(tmp_path)  # fresh instance = fresh hook process
    s2.bump_turn()
    assert s2.filter_unseen(ids, reinject_turns=0) == []


def test_reinject_after_n_turns(tmp_path):
    s = _store(tmp_path)
    s.bump_turn()
    s.mark_seen([("reflection", "r1")])
    for _ in range(3):
        s2 = _store(tmp_path)
        s2.bump_turn()
    s3 = _store(tmp_path)
    s3.bump_turn()  # turn 5; injected at turn 1
    assert s3.filter_unseen([("reflection", "r1")], reinject_turns=3) == [("reflection", "r1")]
    assert s3.filter_unseen([("reflection", "r1")], reinject_turns=10) == []


def test_corrupt_file_treated_as_empty(tmp_path):
    (tmp_path / "context_seen_sess-1.json").write_text("{not json", encoding="utf-8")
    s = _store(tmp_path)
    assert s.bump_turn() == 1
    assert s.filter_unseen([("reflection", "r1")], reinject_turns=0) == [("reflection", "r1")]


def test_sessions_are_isolated(tmp_path):
    a = SeenStore(tmp_path, "sess-a")
    a.bump_turn()
    a.mark_seen([("reflection", "r1")])
    b = SeenStore(tmp_path, "sess-b")
    b.bump_turn()
    assert b.filter_unseen([("reflection", "r1")], reinject_turns=0) == [("reflection", "r1")]


def test_prune_stale_removes_old_files_only(tmp_path):
    import os
    old = tmp_path / "context_seen_old.json"
    new = tmp_path / "context_seen_new.json"
    old.write_text("{}", encoding="utf-8")
    new.write_text("{}", encoding="utf-8")
    ten_days_ago = datetime(2026, 7, 1, tzinfo=UTC).timestamp()
    os.utime(old, (ten_days_ago, ten_days_ago))
    prune_stale(tmp_path, now=datetime(2026, 7, 11, tzinfo=UTC))
    assert not old.exists()
    assert new.exists()


def test_missing_state_dir_never_raises(tmp_path):
    s = SeenStore(tmp_path / "does-not-exist-yet", "sess-1")
    assert s.bump_turn() == 1  # creates the dir
    s.mark_seen([("reflection", "r1")])


class TestPretoolLatch:
    def test_defaults_false_then_persists(self, tmp_path):
        s = SeenStore(tmp_path, "sess")
        assert s.pretool_fired() is False
        s.mark_pretool_fired()
        assert SeenStore(tmp_path, "sess").pretool_fired() is True

    def test_corrupt_state_means_not_fired(self, tmp_path):
        (tmp_path / "context_seen_sess.json").write_text("{", encoding="utf-8")
        assert SeenStore(tmp_path, "sess").pretool_fired() is False

    def test_try_claim_pretool_fired_only_first_caller_wins(self, tmp_path):
        # #107: PreToolUse "one real firing per session" was a check-then-set
        # across two file operations, so N parallel hook processes could all
        # observe pretool_fired == False and all proceed. The sentinel-based
        # atomic claim guarantees exactly one True return.
        stores = [SeenStore(tmp_path, "sess") for _ in range(4)]
        wins = [s.try_claim_pretool_fired() for s in stores]
        assert wins.count(True) == 1
        assert wins.count(False) == 3
        # And every subsequent instance sees the latch as fired.
        assert SeenStore(tmp_path, "sess").pretool_fired() is True

    def test_prune_stale_removes_pretool_sentinel(self, tmp_path):
        import os
        SeenStore(tmp_path, "sess").mark_pretool_fired()
        sentinel = tmp_path / "context_seen_sess.pretool"
        assert sentinel.exists()
        ten_days_ago = datetime(2026, 7, 1, tzinfo=UTC).timestamp()
        os.utime(sentinel, (ten_days_ago, ten_days_ago))
        prune_stale(tmp_path, now=datetime(2026, 7, 11, tzinfo=UTC))
        assert not sentinel.exists()


class TestConcurrentMutators:
    def test_save_is_atomic_via_temp_replace(self, tmp_path):
        # #107: previously plain write_text left a window where a
        # concurrent reader could see a truncated file. Assert the temp
        # sibling is cleaned up and the final file is complete JSON.
        s = SeenStore(tmp_path, "sess")
        s.bump_turn()
        s.mark_seen([("reflection", "r1"), ("semantic", "m1")])
        path = tmp_path / "context_seen_sess.json"
        assert path.exists()
        import json as _json
        loaded = _json.loads(path.read_text(encoding="utf-8"))
        assert loaded["turn"] == 1
        assert loaded["seen"] == {"reflection:r1": 1, "semantic:m1": 1}
        # temp sibling from atomic write must not linger on success
        assert not (tmp_path / "context_seen_sess.json.tmp").exists()

    def test_mark_seen_merges_concurrent_writers_state(self, tmp_path):
        # #107: two hook processes at the same turn each marked disjoint
        # ids; the second _save from a stale snapshot dropped the first
        # process's entries. Both writers now re-read + merge, so the
        # union survives.
        a = SeenStore(tmp_path, "sess")
        b = SeenStore(tmp_path, "sess")
        a.bump_turn()  # a and b both loaded turn=0; a advances the file to 1
        b.mark_seen([("reflection", "r1")])  # b writes with its snapshot
        a.mark_seen([("semantic", "m1")])   # a writes with its snapshot
        # Union of both writers' entries survives on disk.
        fresh = SeenStore(tmp_path, "sess")
        # Neither key should be filter_unseen-visible on a fresh read.
        assert fresh.filter_unseen(
            [("reflection", "r1"), ("semantic", "m1")], reinject_turns=0,
        ) == []
