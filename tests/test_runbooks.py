from tools.runbooks import InMemoryRunbookKB, Runbook


def test_query_matches_by_token_overlap():
    kb = InMemoryRunbookKB()
    kb.write(Runbook(failure="checksum mismatch on vol-1", action="resync_volume"))
    kb.write(Runbook(failure="AccessDenied while assuming the target role", action="rotate_credentials"))
    matches = kb.query("checksum mismatch on vol-9")
    assert matches and matches[0].action == "resync_volume"


def test_no_overlap_returns_empty():
    kb = InMemoryRunbookKB()
    kb.write(Runbook(failure="checksum mismatch on vol-1", action="resync_volume"))
    assert kb.query("something completely different") == []


def test_len_reflects_entries():
    kb = InMemoryRunbookKB()
    assert len(kb) == 0
    kb.write(Runbook(failure="x", action="y"))
    assert len(kb) == 1
