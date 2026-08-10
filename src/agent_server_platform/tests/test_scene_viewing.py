"""
Test viewing existing scenes doesn't show draft preview
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import pytest
from unittest.mock import Mock, patch
from pages.home import assistant


def test_viewing_scene_does_not_set_pending():
    """
    Test that viewing an existing scene (e.g., "查看d13460e9的配置")
    does not set pending_scene, so preview is not shown
    """
    # Mock LLM response with scene-spec code block (even though prompt says not to use it)
    mock_llm_response = """这是场景 d13460e9 的配置：

```json
{
  "scenario_type": "simple_qa",
  "name": "测试场景",
  "description": "测试描述",
  "config": {
    "agent_roles": {
      "scheduling_agent": {"role": "调度"},
      "execution_agents": [{"name": "测试Agent", "role": "测试"}]
    },
    "question": "测试问题",
    "timeout": 3600
  }
}
```

这个场景用于测试问答功能。

```scene-spec
{
  "scenario_type": "simple_qa",
  "name": "测试场景",
  "description": "测试描述",
  "config": {
    "agent_roles": {
      "scheduling_agent": {"role": "调度"},
      "execution_agents": [{"name": "测试Agent", "role": "测试"}]
    },
    "question": "测试问题",
    "timeout": 3600
  }
}
```"""

    with patch('pages.home.assistant.llm_client') as mock_client:
        mock_client.chat = Mock(return_value=mock_llm_response)
        mock_client.client = Mock()

        # Mock ScenarioRepository to return an existing scenario
        with patch('pages.home.assistant.ScenarioRepository') as mock_repo_class:
            mock_repo = Mock()
            mock_repo_class.return_value = mock_repo
            # Mock scenario object
            mock_scenario = Mock()
            mock_scenario.scenario_id = "d13460e9"
            mock_repo.find_by_scenario_id.return_value = mock_scenario
            # Mock find_all for gather_scene_index
            mock_repo.find_all.return_value = []

            # User viewing existing scene (no pending, no saved_id)
            user_text = "查看d13460e9的配置"
            history = []

            visible, new_pending, preview, new_saved_id = assistant.reply(
                user_text=user_text,
                history=history,
                pending_scene=None,
                saved_id=None,
                summary_content=None
            )

            # Verify: pending should be None (no preview should be shown)
            assert new_pending is None, "Viewing scene should not set pending_scene"
            # Verify: preview should be empty
            assert preview == assistant.EMPTY_PREVIEW, "Preview should be empty when viewing"
            # Verify: saved_id should remain None
            assert new_saved_id is None, "saved_id should not be set when viewing"


def test_creating_scene_sets_pending():
    """
    Test that creating a new scene sets pending_scene
    """
    mock_llm_response = """好的，我来帮你创建一个问答场景。

```scene-spec
{
  "scenario_type": "simple_qa",
  "name": "新场景",
  "description": "新场景描述",
  "config": {
    "agent_roles": {
      "scheduling_agent": {"role": "调度"},
      "execution_agents": [{"name": "Agent", "role": "执行"}]
    },
    "question": "问题？",
    "timeout": 3600
  }
}
```"""

    with patch('pages.home.assistant.llm_client') as mock_client:
        mock_client.chat = Mock(return_value=mock_llm_response)
        mock_client.client = Mock()

        user_text = "帮我创建一个问答场景"
        history = []

        visible, new_pending, preview, new_saved_id = assistant.reply(
            user_text=user_text,
            history=history,
            pending_scene=None,
            saved_id=None,
            summary_content=None
        )

        # Verify: pending should be set (preview should be shown)
        assert new_pending is not None, "Creating scene should set pending_scene"
        assert new_pending["scenario_type"] == "simple_qa"
        assert preview != assistant.EMPTY_PREVIEW, "Preview should be shown when creating"


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
