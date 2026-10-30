"""Unit tests for ``workers.mcp_status`` — per-PID status files.

The prefork pool keeps the MCP sessions in each child process while the
``@inspect_command`` handlers run in the worker's main process; the bridge
between the two is one small JSON file per child PID. These tests cover the
pure file logic with an explicit ``directory`` (a tmp dir, never the real
temp dir).
"""

from __future__ import annotations

import json
from functools import partial
from pathlib import Path

import pytest

from iso_insight.schemas.mcp import ToolStatus
from iso_insight.workers import mcp_status, tasks


class TestPublishStatus:
    def test_writes_readable_json(self, tmp_path: Path) -> None:
        mcp_status.publish_status(123, ["confluence", "jira"], ["rag: timeout"], tmp_path)
        payload = json.loads((tmp_path / "mcp-123.json").read_text(encoding="utf-8"))
        assert payload == {
            "pid": 123,
            "connected": ["confluence", "jira"],
            "startup_errors": ["rag: timeout"],
        }

    def test_sorts_connected_servers(self, tmp_path: Path) -> None:
        mcp_status.publish_status(1, ["jira", "confluence"], [], tmp_path)
        payload = json.loads((tmp_path / "mcp-1.json").read_text(encoding="utf-8"))
        assert payload["connected"] == ["confluence", "jira"]

    def test_overwrites_the_previous_status(self, tmp_path: Path) -> None:
        mcp_status.publish_status(7, [], ["jira: timeout startu (5s)"], tmp_path)
        mcp_status.publish_status(7, ["jira"], [], tmp_path)
        payload = json.loads((tmp_path / "mcp-7.json").read_text(encoding="utf-8"))
        assert payload["connected"] == ["jira"]
        assert payload["startup_errors"] == []

    def test_is_best_effort_and_never_raises(self, tmp_path: Path) -> None:
        blocker = tmp_path / "a-file"
        blocker.write_text("x", encoding="utf-8")
        # The "directory" cannot exist (its parent is a file) — publish must
        # swallow the error instead of breaking the worker.
        mcp_status.publish_status(1, ["jira"], [], blocker / "sub")


class TestRemoveStatus:
    def test_unlinks_the_file(self, tmp_path: Path) -> None:
        mcp_status.publish_status(5, ["jira"], [], tmp_path)
        mcp_status.remove_status(5, tmp_path)
        assert not (tmp_path / "mcp-5.json").exists()

    def test_missing_file_is_ok(self, tmp_path: Path) -> None:
        mcp_status.remove_status(5, tmp_path)  # must not raise


class TestCollectStatus:
    def test_merges_and_deduplicates(self, tmp_path: Path) -> None:
        mcp_status.publish_status(11, ["jira"], ["a: err"], tmp_path)
        mcp_status.publish_status(22, ["jira", "confluence"], [], tmp_path)
        assert mcp_status.collect_status([11, 22], tmp_path) == {
            "connected": ["confluence", "jira"],
            "startup_errors": ["a: err"],
        }

    def test_missing_file_reports_uninitialized(self, tmp_path: Path) -> None:
        assert mcp_status.collect_status([99], tmp_path) == {
            "connected": [],
            "startup_errors": ["pid 99: worker nie zainicjalizowany"],
        }

    def test_corrupt_file_reports_uninitialized(self, tmp_path: Path) -> None:
        (tmp_path / "mcp-7.json").write_text("{not json", encoding="utf-8")
        assert mcp_status.collect_status([7], tmp_path) == {
            "connected": [],
            "startup_errors": ["pid 7: worker nie zainicjalizowany"],
        }

    def test_malformed_payload_is_ignored(self, tmp_path: Path) -> None:
        (tmp_path / "mcp-7.json").write_text(
            json.dumps({"connected": "jira", "startup_errors": "err"}), encoding="utf-8"
        )
        assert mcp_status.collect_status([7], tmp_path) == {
            "connected": [],
            "startup_errors": [],
        }

    def test_empty_pids(self, tmp_path: Path) -> None:
        assert mcp_status.collect_status([], tmp_path) == {"connected": [], "startup_errors": []}


class TestPruneStale:
    def test_removes_only_files_of_dead_pids(self, tmp_path: Path) -> None:
        mcp_status.publish_status(11, [], [], tmp_path)
        mcp_status.publish_status(22, [], [], tmp_path)
        mcp_status.prune_stale([22], tmp_path)
        assert not (tmp_path / "mcp-11.json").exists()
        assert (tmp_path / "mcp-22.json").exists()

    def test_missing_directory_is_ok(self, tmp_path: Path) -> None:
        mcp_status.prune_stale([], tmp_path / "does-not-exist")  # must not raise


class TestFilePids:
    def test_lists_pids_from_file_names(self, tmp_path: Path) -> None:
        mcp_status.publish_status(11, [], [], tmp_path)
        mcp_status.publish_status(22, [], [], tmp_path)
        assert mcp_status.file_pids(tmp_path) == [11, 22]

    def test_ignores_unrelated_files(self, tmp_path: Path) -> None:
        (tmp_path / "other.json").write_text("{}", encoding="utf-8")
        (tmp_path / "mcp-x.json").write_text("{}", encoding="utf-8")
        assert mcp_status.file_pids(tmp_path) == []


class _FakeTools:
    def __init__(self, status: ToolStatus) -> None:
        self._status = status

    def status(self) -> ToolStatus:
        return self._status


class _FakeCtx:
    """Just enough of a PipelineContext for the handler's fast path."""

    def __init__(self, status: ToolStatus) -> None:
        self.tools = _FakeTools(status)


class TestMcpStatusHandler:
    """The ``mcp_status`` inspect handler in ``workers.tasks`` (delivery glue).

    Celery calls it with the pidbox ``state`` object as the first argument;
    under prefork the handler runs in the main process and must aggregate the
    children's per-PID files, while single-process pools (solo/threads) keep
    ``_ctx`` in the same process and answer directly.
    """

    def test_direct_answer_when_ctx_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            tasks, "_ctx", _FakeCtx(ToolStatus(connected=["jira"], startup_errors=[]))
        )
        assert tasks.mcp_status(None) == {"connected": ["jira"], "startup_errors": []}

    def test_prefork_aggregates_child_files(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        mcp_status.publish_status(11, ["jira"], [], tmp_path)
        monkeypatch.setattr(tasks, "_ctx", None)
        monkeypatch.setattr(tasks, "_live_child_pids", lambda: [11])
        monkeypatch.setattr(
            tasks, "prune_stale", partial(mcp_status.prune_stale, directory=tmp_path)
        )
        monkeypatch.setattr(
            tasks, "collect_status", partial(mcp_status.collect_status, directory=tmp_path)
        )
        assert tasks.mcp_status(None) == {"connected": ["jira"], "startup_errors": []}

    def test_prefork_reports_uninitialized_child(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A live child (pid 42) that never ran a task has no status file.
        monkeypatch.setattr(tasks, "_ctx", None)
        monkeypatch.setattr(tasks, "_live_child_pids", lambda: [42])
        monkeypatch.setattr(
            tasks, "prune_stale", partial(mcp_status.prune_stale, directory=tmp_path)
        )
        monkeypatch.setattr(
            tasks, "collect_status", partial(mcp_status.collect_status, directory=tmp_path)
        )
        assert tasks.mcp_status(None) == {
            "connected": [],
            "startup_errors": ["pid 42: worker nie zainicjalizowany"],
        }

    def test_prefork_falls_back_to_file_pids_when_introspection_empty(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # If pool introspection yields nothing (pool not started yet), the
        # existing files still define which PIDs to report/prune.
        mcp_status.publish_status(7, ["confluence"], [], tmp_path)
        monkeypatch.setattr(tasks, "_ctx", None)
        monkeypatch.setattr(tasks, "_live_child_pids", lambda: [])
        monkeypatch.setattr(tasks, "file_pids", partial(mcp_status.file_pids, directory=tmp_path))
        monkeypatch.setattr(
            tasks, "prune_stale", partial(mcp_status.prune_stale, directory=tmp_path)
        )
        monkeypatch.setattr(
            tasks, "collect_status", partial(mcp_status.collect_status, directory=tmp_path)
        )
        assert tasks.mcp_status(None) == {"connected": ["confluence"], "startup_errors": []}
