import pytest
from datetime import datetime, timedelta

from pages.home.compression import (
    should_compress,
    build_summary_prompt,
    compress_if_needed,
    COMPRESS_THRESHOLD,
    COMPRESS_BATCH,
)


def _ts(minutes_ago: int) -> datetime:
    return datetime.now() - timedelta(minutes=minutes_ago)


class FakeMsg:
    def __init__(self, role: str, content: str, timestamp=None):
        self.role = role
        self.content = content
        self.timestamp = timestamp or datetime.now()
        self.id = None


def test_should_compress_false_when_under_threshold():
    assert not should_compress(COMPRESS_THRESHOLD - 1)


def test_should_compress_false_when_at_threshold():
    assert not should_compress(COMPRESS_THRESHOLD)


def test_should_compress_true_when_over_threshold():
    assert should_compress(COMPRESS_THRESHOLD + 1)


def test_compress_batch_is_reasonable():
    assert COMPRESS_BATCH > 0
    assert COMPRESS_BATCH <= COMPRESS_THRESHOLD


def test_build_summary_prompt_formats_messages():
    messages = [
        FakeMsg("user", "Hello, how are you?"),
        FakeMsg("assistant", "I'm doing well, thanks!"),
        FakeMsg("user", "What's the weather like?"),
    ]
    prompt = build_summary_prompt(messages)
    assert "用户" in prompt
    assert "助手" in prompt
    assert "Hello, how are you?" in prompt
    assert "I'm doing well, thanks!" in prompt
    assert "2-3句话" in prompt


def test_compress_if_needed_returns_none_when_under_threshold():
    class FakeRepo:
        def __init__(self):
            self.deleted = []
        def count_messages(self, session_id): return 10
        def find_old_messages(self, session_id, limit): return []
        def delete_messages_by_ids(self, ids): self.deleted.extend(ids)
        def save(self, role, content, session_id): return 999

    repo = FakeRepo()
    result = compress_if_needed("test_session", repo, llm_client=None)
    assert result is None
    assert repo.deleted == []


def test_compress_if_needed_returns_none_when_not_enough_messages():
    class FakeRepo:
        def __init__(self):
            self.deleted = []
        def count_messages(self, session_id): return 50
        def find_old_messages(self, session_id, limit): return [FakeMsg("user", f"msg{i}") for i in range(20)]
        def delete_messages_by_ids(self, ids): self.deleted.extend(ids)
        def save(self, role, content, session_id): return 999

    repo = FakeRepo()
    result = compress_if_needed("test_session", repo, llm_client=None)
    assert result is None
    assert repo.deleted == []


def test_compress_if_needed_compresses_when_threshold_exceeded():
    messages = [FakeMsg("user" if i % 2 == 0 else "assistant", f"Message {i}") for i in range(35)]
    for i, msg in enumerate(messages):
        msg.id = i + 1

    class FakeLLMClient:
        def chat(self, messages, temperature):
            return "This is a summary of the conversation."

    class FakeRepo:
        def __init__(self):
            self.deleted = []
            self.saved = []
        def count_messages(self, session_id): return 50
        def find_old_messages(self, session_id, limit): return messages[:limit]
        def delete_messages_by_ids(self, ids): self.deleted.extend(ids)
        def save(self, role, content, session_id):
            self.saved.append((role, content, session_id))
            return 999

    repo = FakeRepo()
    llm = FakeLLMClient()
    result = compress_if_needed("test_session", repo, llm_client=llm)

    assert result == "This is a summary of the conversation."
    assert len(repo.deleted) == 30
    assert repo.deleted == [msg.id for msg in messages[:30]]
    assert len(repo.saved) == 1
    assert repo.saved[0] == ("summary", "This is a summary of the conversation.", "test_session")


def test_compress_if_needed_returns_none_when_llm_fails():
    messages = [FakeMsg("user" if i % 2 == 0 else "assistant", f"Message {i}") for i in range(35)]

    class FakeLLMClient:
        def chat(self, messages, temperature):
            return None

    class FakeRepo:
        def __init__(self):
            self.deleted = []
            self.saved = []
        def count_messages(self, session_id): return 50
        def find_old_messages(self, session_id, limit): return messages[:limit]
        def delete_messages_by_ids(self, ids): self.deleted.extend(ids)
        def save(self, role, content, session_id): self.saved.append((role, content, session_id))

    repo = FakeRepo()
    llm = FakeLLMClient()
    result = compress_if_needed("test_session", repo, llm_client=llm)

    assert result is None
    assert repo.deleted == []
    assert repo.saved == []
