"""
Test creating scene shows preview correctly
"""
import pytest
from unittest.mock import MagicMock, patch
from pages.home import assistant


@patch('pages.home.assistant.ScenarioRepository')
def test_create_scene_shows_preview(mock_repo_class):
    """Test that creating a new scene shows the preview"""
    # Mock repository
    mock_repo = MagicMock()
    mock_repo_class.return_value = mock_repo
    mock_repo.find_by_scenario_id.return_value = None  # No existing scenario

    # Test user request to create a calculation scene
    user_text = "创建人工审查场景 1+2+...+1000="

    # Mock LLM response with scene-spec
    mock_response = """好的，我为您创建了一个计算 1+2+...+1000 的问答场景。

```scene-spec
{
  "scenario_type": "simple_qa",
  "name": "计算 1+2+...+1000",
  "description": "人工审查的计算场景",
  "config": {
    "agent_roles": {
      "scheduling_agent": {"role": "调度 Agent"},
      "execution_agents": [{"name": "计算 Agent", "role": "执行计算"}]
    },
    "manual_acceptance": true,
    "question": "1+2+3+...+1000 = ?"
  }
}
```
"""

    with patch('pages.home.assistant.llm_client') as mock_llm:
        mock_llm.chat.return_value = mock_response

        result = assistant.reply(
            user_text=user_text,
            history=[],
            pending_scene=None,
            saved_id=None,
            summary_content=None
        )

        response_text, new_pending, preview_md, new_saved_id = result

        # Verify: should have pending scene (preview should show)
        assert new_pending is not None, "Should have pending scene for preview"
        assert new_pending["scenario_type"] == "simple_qa"
        assert "1+2+...+1000" in new_pending["name"]
        assert new_pending["config"]["manual_acceptance"] == True

        # Verify: preview should be rendered
        assert preview_md is not None
        assert "场景草稿预览" in preview_md or "计算 1+2+...+1000" in preview_md


@patch('pages.home.assistant.ScenarioRepository')
def test_create_scene_with_numbers_in_name(mock_repo_class):
    """Test that numbers in scene name don't confuse the view detection"""
    # Mock repository
    mock_repo = MagicMock()
    mock_repo_class.return_value = mock_repo
    mock_repo.find_by_scenario_id.return_value = None

    # Test with numbers in the request
    user_text = "创建一个计算12345的场景"

    mock_response = """好的，我为您创建了一个计算场景。

```scene-spec
{
  "scenario_type": "simple_qa",
  "name": "计算场景",
  "description": "测试场景",
  "config": {
    "agent_roles": {
      "scheduling_agent": {"role": "调度"},
      "execution_agents": [{"name": "执行", "role": "执行"}]
    },
    "question": "计算12345"
  }
}
```
"""

    with patch('pages.home.assistant.llm_client') as mock_llm:
        mock_llm.chat.return_value = mock_response

        result = assistant.reply(
            user_text=user_text,
            history=[],
            pending_scene=None,
            saved_id=None,
            summary_content=None
        )

        response_text, new_pending, preview_md, new_saved_id = result

        # Verify: should show preview even with numbers in text
        assert new_pending is not None, "Should show preview for creation"
        assert new_pending["scenario_type"] == "simple_qa"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
