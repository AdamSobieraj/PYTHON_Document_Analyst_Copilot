"""Per-PID MCP status files: bridge the prefork children's tool status to the main process.

Celery's ``@inspect_command`` handlers run in the worker's *main* process,
but the MCP sessions (module globals in :mod:`iso_insight.workers.tasks`)
live in each prefork *child*. Each child therefore publishes its
``ToolStatus`` to a small JSON file named after its PID, and the main
process reads the files of its live children to answer ``mcp_status``.
Writes are atomic (tmp file + ``os.replace``) so a reader never sees a torn
file. Reporting is best-effort: any failure is logged and never raised —
health reporting must not break the worker.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

logger = logging.getLogger("iso_insight.worker.mcp_status")

STATUS_DIRNAME = "iso-insight-mcp-status"
_FILE_TEMPLATE = "mcp-{pid}.json"
_UNINITIALIZED = "worker nie zainicjalizowany"


def status_dir(directory: Path | None = None) -> Path:
    """Directory holding the per-PID status files (created on demand)."""
    base = directory if directory is not None else Path(tempfile.gettempdir()) / STATUS_DIRNAME
    base.mkdir(parents=True, exist_ok=True)
    return base


def status_file_for(pid: int, directory: Path | None = None) -> Path:
    """Path of the status file of one worker process."""
    return status_dir(directory) / _FILE_TEMPLATE.format(pid=pid)


def publish_status(
    pid: int,
    connected: Sequence[str],
    startup_errors: Sequence[str],
    directory: Path | None = None,
) -> None:
    """Atomically write one process's MCP status (best-effort — never raises)."""
    try:
        base = status_dir(directory)
        path = base / _FILE_TEMPLATE.format(pid=pid)
        payload: dict[str, Any] = {
            "pid": pid,
            "connected": sorted(connected),
            "startup_errors": list(startup_errors),
        }
        tmp = base / f".{path.name}.tmp"
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        logger.debug("MCP status publish failed", exc_info=True)


def remove_status(pid: int, directory: Path | None = None) -> None:
    """Delete one process's status file (best-effort — never raises)."""
    try:
        status_file_for(pid, directory).unlink(missing_ok=True)
    except OSError:
        logger.debug("MCP status remove failed", exc_info=True)


def file_pids(directory: Path | None = None) -> list[int]:
    """PIDs that currently have a status file on disk (sorted)."""
    base = status_dir(directory)
    pids: list[int] = []
    for path in base.glob(_FILE_TEMPLATE.format(pid="*")):
        try:
            pids.append(int(path.name[len("mcp-") : -len(".json")]))
        except ValueError:
            continue
    return sorted(pids)


def collect_status(pids: Sequence[int], directory: Path | None = None) -> dict[str, Any]:
    """Aggregate the status files of the given PIDs.

    Returns ``{"connected": [...], "startup_errors": [...]}``: connected
    server names are merged and deduplicated across processes, errors are
    concatenated. A PID without a readable file contributes one
    ``"pid N: worker nie zainicjalizowany"`` entry (child alive, no task
    run yet).
    """
    connected: set[str] = set()
    errors: list[str] = []
    for pid in pids:
        try:
            payload = json.loads(status_file_for(pid, directory).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            errors.append(f"pid {pid}: {_UNINITIALIZED}")
            continue
        if not isinstance(payload, dict):
            errors.append(f"pid {pid}: {_UNINITIALIZED}")
            continue
        raw_connected = payload.get("connected")
        if isinstance(raw_connected, list):
            connected.update(name for name in raw_connected if isinstance(name, str))
        raw_errors = payload.get("startup_errors")
        if isinstance(raw_errors, list):
            errors.extend(msg for msg in raw_errors if isinstance(msg, str))
    return {"connected": sorted(connected), "startup_errors": errors}


def prune_stale(live_pids: Sequence[int], directory: Path | None = None) -> None:
    """Delete status files of PIDs that are no longer live (best-effort)."""
    live = set(live_pids)
    for pid in file_pids(directory):
        if pid not in live:
            try:
                status_file_for(pid, directory).unlink()
            except OSError:
                logger.debug("MCP status prune failed", exc_info=True)
