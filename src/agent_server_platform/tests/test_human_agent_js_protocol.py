# Human Agent — 协议互通自检
#
# JS 客户端 (src/pages/human_agent/static/human_agent.js) 构造的帧必须能被后端
# ws_protocol.parse_frame 解析、且形状与 Python 构造器一致。这里用 Python 复刻
# JS 的 makeFrame 逻辑，对每种帧做 round-trip 校验。
import json

import pytest

from core import ws_protocol as P


def js_make_frame(frame_type, payload, task_id=None):
    """Python 复刻 human_agent.js makeFrame：与后端 wire 格式逐字段一致。"""
    return json.dumps({
        "type": frame_type,
        "task_id": task_id if task_id is not None else None,
        "payload": payload or {},
    }, ensure_ascii=False)


CASES = [
    ("register", P.register_frame("human-1", "Human Agent", 8, {"human": True})),
    ("status", P.status_frame(P.STATUS_IDLE, 8, 0, {"human": True})),
    ("task_result", P.task_result_frame("t1", True, {"output": "x", "submitted_by": "human-1"})),
]


@pytest.mark.parametrize("label,raw", CASES)
def test_js_constructed_frames_parse(label, raw):
    """JS 构造的每种帧能被 parse_frame 解析，且 round-trip 等价。"""
    f = P.parse_frame(raw)
    assert f["type"] == label
    assert isinstance(f["payload"], dict)
    # JS 重新构造应得到相同的 payload
    js_raw = js_make_frame(f["type"], f["payload"], f["task_id"])
    assert P.parse_frame(js_raw)["payload"] == f["payload"]


def test_register_payload_shape_matches_js():
    """register 帧字段与 human_agent.js sendRegister 一致。

    人工 Agent 显式声明 source=human_agent，与执行服务器 (execution_server)
    区分，便于注册表/UI 标注来源。"""
    raw = P.register_frame("human-1", "human-1", 8, {"human": True},
                           source="human_agent")
    p = P.parse_frame(raw)["payload"]
    assert p == {
        "server_id": "human-1",
        "name": "human-1",
        "total_quota": 8,
        "env_info": {"human": True},
        "source": "human_agent",
    }


def test_task_result_payload_shape_matches_js():
    """task_result 帧字段与 human_agent.js submit 一致。

    内层 result 必须带 success（finalize_task 读 result.get("success")），
    与 exec-server task_runner 发送 agent 完整 result 的契约一致。
    """
    raw = P.task_result_frame("t1", True,
                              {"success": True, "output": "answer",
                               "submitted_by": "human-1"})
    p = P.parse_frame(raw)["payload"]
    assert p["task_id"] == "t1"
    assert p["success"] is True
    assert p["result"]["success"] is True
    assert p["result"]["output"] == "answer"


def test_task_frame_dispatches_to_js():
    """后端下发的 task 帧能被 JS _onTask 消费的字段都在。"""
    raw = P.task_frame("t1", "p1", "帮我翻译这段话",
                       {"role": "译者", "system_prompt": "你是翻译"})
    p = P.parse_frame(raw)["payload"]
    assert p["task_id"] == "t1"
    assert p["goal"] == "帮我翻译这段话"
    assert p["context"]["role"] == "译者"
    assert p["parent_task_id"] == "p1"


def test_pages_assemble_without_error():
    """human_tasks 页与 home 页装配不抛异常；inject_head 产出含 JS 的 head 内容。"""
    from pages.human_tasks import create_page
    from pages.human_agent import inject_head

    html = inject_head()
    assert "window.__HUMAN_OPTS__" in html
    assert "HumanAgent" in html  # JS 已内联

    # create_page 内部构建 gr.Blocks；只需不抛异常
    create_page(global_state_component=None)


def test_home_page_still_assembles():
    """home 页加 Toast 注入后仍可装配。"""
    from pages.home import create_page
    create_page(global_state_component=None)
