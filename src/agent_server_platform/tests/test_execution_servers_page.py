# Execution Servers page - handler-level tests.
#
# The page exposes module-level handlers (_remove_server, _cleanup_offline,
# _load_servers, _server_detail, _source_label) so the remove/cleanup logic
# and source annotation are testable without launching Gradio. These tests
# drive those handlers against a fresh DB.
import pytest

from database.repositories.execution_server_repository import ExecutionServerRepository
from pages.execution_servers import (
    _remove_server, _cleanup_offline, _load_servers, _server_detail,
    _source_label,
)


class TestSourceLabel:
    def test_known_sources_map_to_chinese_labels(self):
        assert _source_label("execution_server") == "执行服务器"
        assert _source_label("human_agent") == "人工 Agent"

    def test_unknown_and_empty_fall_back(self):
        assert _source_label(None) == "-"
        assert _source_label("") == "-"
        assert _source_label("robot") == "robot"


class TestLoadServersSourceColumn:
    def test_rows_carry_source_label_at_index_2(self):
        repo = ExecutionServerRepository()
        repo.upsert("node-1", name="Node 1", total_quota=4, status="idle",
                    connected=True, source="execution_server")
        repo.upsert("human-1", name="human-1", total_quota=8, status="idle",
                    connected=True, source="human_agent")

        rows, summary = _load_servers()
        by_id = {r[0]: r for r in rows}
        assert by_id["node-1"][2] == "执行服务器"
        assert by_id["human-1"][2] == "人工 Agent"
        # summary surfaces an offline count now
        assert "离线" in summary


class TestServerDetailSource:
    def test_detail_includes_source_and_label(self):
        ExecutionServerRepository().upsert(
            "human-1", name="human-1", total_quota=8, status="idle",
            connected=True, source="human_agent")

        detail = _server_detail("human-1")
        assert detail["source"] == "human_agent"
        assert detail["source_label"] == "人工 Agent"


class TestRemoveServer:
    def test_remove_offline_succeeds_and_deletes_row(self):
        repo = ExecutionServerRepository()
        repo.upsert("node-2", name="Node 2", total_quota=2,
                    status="offline", connected=False)

        ok, msg = _remove_server("node-2")

        assert ok is True
        assert "已移除" in msg
        assert repo.find_by_server_id("node-2") is None

    def test_remove_connected_is_refused(self):
        """Connected servers can't be removed - their row would reappear on
        the next heartbeat while the live WS connection keeps receiving tasks."""
        repo = ExecutionServerRepository()
        repo.upsert("node-1", name="Node 1", total_quota=4,
                    status="idle", connected=True)

        ok, msg = _remove_server("node-1")

        assert ok is False
        assert "在线" in msg
        # row untouched
        assert repo.find_by_server_id("node-1") is not None

    def test_remove_missing_returns_failure(self):
        ok, msg = _remove_server("ghost")
        assert ok is False
        assert "未找到" in msg

    def test_remove_empty_id_returns_failure(self):
        ok, _msg = _remove_server("")
        assert ok is False


class TestCleanupOffline:
    def test_cleanup_deletes_only_offline(self):
        repo = ExecutionServerRepository()
        repo.upsert("node-1", name="Node 1", total_quota=4,
                    status="idle", connected=True)
        repo.upsert("node-2", name="Node 2", total_quota=2,
                    status="offline", connected=False)
        repo.upsert("node-3", name="Node 3", total_quota=2,
                    status="offline", connected=False)

        n, msg = _cleanup_offline()

        assert n == 2
        assert "已清理" in msg
        remaining = {s.server_id for s in repo.list_all()}
        assert remaining == {"node-1"}

    def test_cleanup_with_no_offline_reports_nothing_to_clean(self):
        ExecutionServerRepository().upsert(
            "node-1", name="Node 1", total_quota=4,
            status="idle", connected=True)

        n, msg = _cleanup_offline()

        assert n == 0
        assert "没有可清理" in msg
