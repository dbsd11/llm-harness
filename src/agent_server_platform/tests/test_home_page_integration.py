"""
Home page integration test - 测试场景管理助手的完整功能流程
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import pytest
from datetime import datetime
from database import init_database
from database.repositories.assistant_message_repository import AssistantMessageRepository
import gradio as gr


@pytest.fixture(autouse=True)
def setup_database():
    """初始化数据库"""
    init_database()
    yield


@pytest.fixture
def msg_repo():
    """提供消息仓储"""
    return AssistantMessageRepository()


def test_chatbot_messages_format(msg_repo):
    """测试 Chatbot 消息格式是否符合 Gradio 5.x 要求"""
    session_id = 'test_format'
    msg_repo.clear_session(session_id)

    # 添加测试消息
    msg1_id = msg_repo.save('user', 'Hello', session_id)
    msg2_id = msg_repo.save('assistant', 'Hi there!', session_id)

    # 模拟 load_history 函数
    messages = msg_repo.find_by_session(session_id, limit=100)
    history = []
    for m in messages:
        history.append({'role': m.role, 'content': m.content, 'id': m.id})

    # 验证格式
    assert len(history) == 2
    assert history[0]['role'] == 'user'
    assert history[0]['content'] == 'Hello'
    assert 'id' in history[0]
    assert history[1]['role'] == 'assistant'
    assert history[1]['content'] == 'Hi there!'
    assert 'id' in history[1]

    # 测试 Gradio Chatbot 是否能接受这个格式
    chatbot = gr.Chatbot(type='messages', label='Test')
    chatbot.value = history  # 不应该抛出异常


def test_on_send_flow(msg_repo):
    """测试 on_send 函数的完整流程"""
    session_id = 'test_on_send'
    msg_repo.clear_session(session_id)

    # 模拟 on_send 函数的逻辑
    user_text = '测试消息'
    chatbot = []  # 初始空状态

    # 步骤1: 添加用户消息到历史
    history = list(chatbot or [])
    history.append({'role': 'user', 'content': user_text})
    user_msg_id = msg_repo.save('user', user_text, session_id)
    history[-1]['id'] = user_msg_id

    # 步骤2: 添加助手回复到历史
    assistant_text = '这是助手回复'
    history.append({'role': 'assistant', 'content': assistant_text})
    assistant_msg_id = msg_repo.save('assistant', assistant_text, session_id)
    history[-1]['id'] = assistant_msg_id

    # 验证返回的 history 格式
    assert len(history) == 2
    assert history[0] == {'role': 'user', 'content': '测试消息', 'id': user_msg_id}
    assert history[1] == {'role': 'assistant', 'content': '这是助手回复', 'id': assistant_msg_id}

    # 测试 Gradio Chatbot 是否能接受
    chatbot_component = gr.Chatbot(type='messages', label='Test')
    chatbot_component.value = history  # 不应该抛出异常

    # 验证数据库中的消息
    db_messages = msg_repo.find_by_session(session_id)
    assert len(db_messages) == 2
    assert db_messages[0].role == 'user'
    assert db_messages[0].content == '测试消息'
    assert db_messages[1].role == 'assistant'
    assert db_messages[1].content == '这是助手回复'


def test_multiple_sends(msg_repo):
    """测试多次发送消息"""
    session_id = 'test_multi'
    msg_repo.clear_session(session_id)

    history = []

    # 第一轮对话
    history.append({'role': 'user', 'content': '第一轮问题'})
    history[-1]['id'] = msg_repo.save('user', '第一轮问题', session_id)
    history.append({'role': 'assistant', 'content': '第一轮回复'})
    history[-1]['id'] = msg_repo.save('assistant', '第一轮回复', session_id)

    # 第二轮对话
    history.append({'role': 'user', 'content': '第二轮问题'})
    history[-1]['id'] = msg_repo.save('user', '第二轮问题', session_id)
    history.append({'role': 'assistant', 'content': '第二轮回复'})
    history[-1]['id'] = msg_repo.save('assistant', '第二轮回复', session_id)

    # 验证
    assert len(history) == 4
    chatbot_component = gr.Chatbot(type='messages', label='Test')
    chatbot_component.value = history  # 不应该抛出异常

    db_messages = msg_repo.find_by_session(session_id)
    assert len(db_messages) == 4


def test_delete_pair(msg_repo):
    """测试删除消息对"""
    session_id = 'test_delete'
    msg_repo.clear_session(session_id)

    # 添加两轮对话
    u1 = msg_repo.save('user', '问题1', session_id)
    a1 = msg_repo.save('assistant', '回复1', session_id)
    u2 = msg_repo.save('user', '问题2', session_id)
    a2 = msg_repo.save('assistant', '回复2', session_id)

    # 删除第二轮对话
    deleted_count = msg_repo.delete_pair(u2)

    # 验证
    assert deleted_count == 2  # 删除了用户消息和助手回复
    remaining = msg_repo.find_by_session(session_id)
    assert len(remaining) == 2
    assert remaining[0].id == u1
    assert remaining[1].id == a1


def test_summary_injection(msg_repo):
    """测试摘要注入"""
    session_id = 'test_summary'
    msg_repo.clear_session(session_id)

    # 添加一些消息
    for i in range(5):
        msg_repo.save('user', f'用户消息{i}', session_id)
        msg_repo.save('assistant', f'助手回复{i}', session_id)

    # 模拟压缩（直接添加摘要）
    from database.models.assistant_message import AssistantMessage
    summary = AssistantMessage(
        role='summary',
        content='这是对话摘要',
        session_id=session_id,
        timestamp=datetime.now()
    )
    msg_repo.create(summary)

    # 模拟 load_history 包含摘要
    messages = msg_repo.find_by_session(session_id, limit=100)
    history = []

    # 添加摘要（如果有）
    latest_summary = msg_repo.find_latest_summary(session_id)
    if latest_summary:
        history.append({
            'role': 'assistant',
            'content': f'📝 **之前的对话摘要**: {latest_summary.content}',
            'id': None
        })

    # 添加普通消息
    for m in messages:
        if m.role != 'summary':  # 跳过摘要消息本身
            history.append({'role': m.role, 'content': m.content, 'id': m.id})

    # 验证
    assert len(history) == 11  # 1个摘要 + 10条消息（5轮对话）
    assert history[0]['role'] == 'assistant'
    assert '摘要' in history[0]['content']
    assert history[0]['id'] is None  # 摘要消息没有 id

    # 测试 Gradio Chatbot 是否能接受
    chatbot_component = gr.Chatbot(type='messages', label='Test')
    chatbot_component.value = history  # 不应该抛出异常


def test_compression_threshold(msg_repo):
    """测试压缩阈值"""
    from pages.home.compression import should_compress, COMPRESS_THRESHOLD

    # 测试阈值以下
    assert not should_compress(COMPRESS_THRESHOLD - 1)
    assert not should_compress(COMPRESS_THRESHOLD)

    # 测试阈值以上
    assert should_compress(COMPRESS_THRESHOLD + 1)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
