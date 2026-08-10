import pytest
from unittest.mock import Mock
from pages.home.assistant import SceneAssistant


def test_reply_injects_summary_into_system_prompt():
    mock_llm = Mock()
    mock_llm.chat.return_value = "Assistant response"
    mock_repo = Mock()
    mock_repo.find_pending_scene.return_value = None

    assistant = SceneAssistant(mock_llm, mock_repo)

    summary_content = "这是一段对话摘要"
    result = assistant.reply("user message", session_id="test_session", summary_content=summary_content)

    assert result == "Assistant response"

    # Verify LLM was called with summary in system prompt
    call_args = mock_llm.chat.call_args
    messages = call_args[0][0]
    system_message = messages[0]

    assert system_message["role"] == "system"
    assert summary_content in system_message["content"]


def test_reply_works_without_summary():
    mock_llm = Mock()
    mock_llm.chat.return_value = "Assistant response"
    mock_repo = Mock()
    mock_repo.find_pending_scene.return_value = None

    assistant = SceneAssistant(mock_llm, mock_repo)

    result = assistant.reply("user message", session_id="test_session")

    assert result == "Assistant response"

    # Verify LLM was called
    assert mock_llm.chat.called


def test_reply_handles_pending_scene():
    mock_llm = Mock()
    mock_llm.chat.return_value = "Assistant response"
    mock_repo = Mock()
    mock_repo.find_pending_scene.return_value = {"name": "test_scene", "type": "simple_qa"}

    assistant = SceneAssistant(mock_llm, mock_repo)

    result = assistant.reply("user message", session_id="test_session")

    assert result == "Assistant response"

    # Verify pending scene was injected
    call_args = mock_llm.chat.call_args
    messages = call_args[0][0]
    system_message = messages[0]
    assert "test_scene" in system_message["content"]
