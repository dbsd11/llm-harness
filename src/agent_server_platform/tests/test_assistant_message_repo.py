"""Tests for AssistantMessageRepository — new methods for history compression."""
import pytest
from datetime import datetime, timedelta
from database.repositories.assistant_message_repository import AssistantMessageRepository


@pytest.fixture
def repo():
    return AssistantMessageRepository()


def _ts(minutes_ago: int) -> datetime:
    return datetime.now() - timedelta(minutes=minutes_ago)


def test_ensure_columns_adds_summary(repo):
    """_ensure_columns should be idempotent — safe to call twice."""
    repo._ensure_columns()
    repo._ensure_columns()  # second call must not raise
    # Verify we can save and retrieve a message with summary field
    msg_id = repo.save("summary", "压缩摘要内容", "test_session")
    msg = repo.find_by_id(msg_id)
    assert msg is not None
    assert msg.summary is None  # save() doesn't set summary; that's for save_summary


def test_count_messages_excludes_summaries(repo):
    """count_messages should count only user/assistant, not summary role."""
    repo.save("user", "hello", "s1")
    repo.save("assistant", "hi", "s1")
    # Insert a summary message directly
    from database.models.assistant_message import AssistantMessage
    summary = AssistantMessage(role="summary", content="old stuff", session_id="s1",
                               timestamp=datetime.now())
    repo.create(summary)

    count = repo.count_messages("s1")
    assert count == 2


def test_count_messages_isolates_sessions(repo):
    repo.save("user", "hello", "s1")
    repo.save("user", "hello", "s2")
    assert repo.count_messages("s1") == 1
    assert repo.count_messages("s2") == 1


def test_find_old_messages_returns_oldest_first(repo):
    """find_old_messages should return non-summary messages ordered by timestamp ASC."""
    ids = []
    for i in range(5):
        ids.append(repo.save("user", f"msg{i}", "s1"))
    # Insert a summary — should be excluded
    from database.models.assistant_message import AssistantMessage
    repo.create(AssistantMessage(role="summary", content="x", session_id="s1",
                                 timestamp=datetime.now()))

    old = repo.find_old_messages("s1", limit=3)
    assert len(old) == 3
    assert all(m.role != "summary" for m in old)
    # Should be the oldest 3
    assert old[0].content == "msg0"
    assert old[2].content == "msg2"


def test_delete_messages_by_ids(repo):
    """delete_messages_by_ids should remove specified messages and return count."""
    id1 = repo.save("user", "a", "s1")
    id2 = repo.save("assistant", "b", "s1")
    id3 = repo.save("user", "c", "s1")

    deleted = repo.delete_messages_by_ids([id1, id3])
    assert deleted == 2

    remaining = repo.find_by_session("s1")
    assert len(remaining) == 1
    assert remaining[0].id == id2


def test_delete_messages_by_ids_empty_list(repo):
    assert repo.delete_messages_by_ids([]) == 0


def test_find_latest_summary_returns_none_when_no_summary(repo):
    repo.save("user", "hello", "s1")
    assert repo.find_latest_summary("s1") is None


def test_find_latest_summary_returns_most_recent(repo):
    from database.models.assistant_message import AssistantMessage
    # Two summaries — should return the newer one
    s1 = AssistantMessage(role="summary", content="older summary", session_id="s1",
                          timestamp=_ts(100))
    s2 = AssistantMessage(role="summary", content="newer summary", session_id="s1",
                          timestamp=_ts(50))
    repo.create(s1)
    repo.create(s2)

    result = repo.find_latest_summary("s1")
    assert result is not None
    assert result.content == "newer summary"


def test_delete_pair_deletes_user_and_reply(repo):
    """Pair deletion: user msg + next assistant msg."""
    u1 = repo.save("user", "question1", "s1")
    a1 = repo.save("assistant", "answer1", "s1")
    u2 = repo.save("user", "question2", "s1")
    a2 = repo.save("assistant", "answer2", "s1")

    # Delete pair for u2 → should delete u2 + a2
    deleted = repo.delete_pair(u2)
    assert deleted == 2

    remaining = repo.find_by_session("s1")
    remaining_ids = {m.id for m in remaining}
    assert u1 in remaining_ids
    assert a1 in remaining_ids
    assert u2 not in remaining_ids
    assert a2 not in remaining_ids


def test_delete_pair_with_no_reply(repo):
    """If no assistant reply follows the user msg, delete only user msg."""
    u1 = repo.save("user", "orphan question", "s1")
    deleted = repo.delete_pair(u1)
    assert deleted == 1


def test_delete_pair_for_assistant_message(repo):
    """Pair deletion triggered on an assistant message: delete it + preceding user msg."""
    u1 = repo.save("user", "question1", "s1")
    a1 = repo.save("assistant", "answer1", "s1")

    deleted = repo.delete_pair(a1)
    assert deleted == 2

    remaining = repo.find_by_session("s1")
    assert len(remaining) == 0
