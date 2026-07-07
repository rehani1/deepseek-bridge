from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .bridge import AuthenticationRequired, DeepSeekBusyError, DeepSeekWebBridge, normalize_task
from .client import STATE_DIR
from .patch_agent import PatchAgent


DATABASE_PATH = STATE_DIR / "jobs.sqlite3"
TERMINAL_STATES = {"completed", "failed", "cancelled", "blocked_auth"}
PENDING_STATES = {"queued", "waiting", "running"}


class JobStore:
    def __init__(self, path: Path = DATABASE_PATH) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    next_attempt_at REAL NOT NULL,
                    deadline_at REAL NOT NULL,
                    task TEXT NOT NULL,
                    project_root TEXT NOT NULL,
                    paths_json TEXT NOT NULL,
                    test_command TEXT,
                    apply_changes INTEGER NOT NULL,
                    max_repairs INTEGER NOT NULL,
                    keep_awake INTEGER NOT NULL,
                    autonomous INTEGER NOT NULL DEFAULT 0,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    conversation_url TEXT,
                    plan_text TEXT,
                    result_json TEXT,
                    error TEXT
                )
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
            }
            if "autonomous" not in columns:
                connection.execute(
                    "ALTER TABLE jobs ADD COLUMN autonomous INTEGER NOT NULL DEFAULT 0"
                )
            if "plan_text" not in columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN plan_text TEXT")
            connection.execute(
                "UPDATE jobs SET status='waiting', next_attempt_at=?, "
                "error='Daemon restarted during this attempt; job safely requeued' "
                "WHERE status='running'",
                (time.time() + 60,),
            )
        self.path.chmod(0o600)

    def submit(
        self,
        *,
        task: str,
        project_root: str,
        paths: list[str] | None,
        test_command: str | None,
        apply_changes: bool,
        max_repairs: int,
        keep_awake: bool,
        autonomous: bool = False,
        max_wait_hours: int,
    ) -> dict[str, Any]:
        now = time.time()
        job_id = uuid.uuid4().hex[:12]
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    id, status, created_at, updated_at, next_attempt_at, deadline_at,
                    task, project_root, paths_json, test_command, apply_changes,
                    max_repairs, keep_awake, autonomous
                ) VALUES (?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    now,
                    now,
                    now,
                    now + max_wait_hours * 3_600,
                    task,
                    project_root,
                    json.dumps(paths or []),
                    test_command,
                    int(apply_changes),
                    max_repairs,
                    int(keep_awake),
                    int(autonomous),
                ),
            )
        return self.get(job_id)

    def find_pending_duplicate(
        self,
        *,
        task: str,
        project_root: str,
        paths: list[str] | None,
        test_command: str | None,
        apply_changes: bool,
        autonomous: bool,
    ) -> dict[str, Any] | None:
        paths_json = json.dumps(paths or [])
        placeholders = ",".join("?" for _ in PENDING_STATES)
        with self.connection() as connection:
            row = connection.execute(
                f"""
                SELECT * FROM jobs
                WHERE task=? AND project_root=? AND paths_json=?
                    AND COALESCE(test_command, '')=COALESCE(?, '')
                    AND apply_changes=? AND autonomous=? AND status IN ({placeholders})
                ORDER BY created_at DESC LIMIT 1
                """,
                (
                    task,
                    project_root,
                    paths_json,
                    test_command,
                    int(apply_changes),
                    int(autonomous),
                    *tuple(PENDING_STATES),
                ),
            ).fetchone()
        return self._decode(row) if row is not None else None

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["paths"] = json.loads(value.pop("paths_json"))
        value["apply_changes"] = bool(value["apply_changes"])
        value["keep_awake"] = bool(value["keep_awake"])
        value["autonomous"] = bool(value["autonomous"])
        result_json = value.pop("result_json")
        value["result"] = json.loads(result_json) if result_json else None
        return value

    def get(self, job_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown DeepSeek job: {job_id}")
        return self._decode(row)

    def list(self, limit: int = 10, project_root: str | None = None) -> list[dict[str, Any]]:
        with self.connection() as connection:
            bounded_limit = max(1, min(limit, 50))
            if project_root:
                rows = connection.execute(
                    "SELECT * FROM jobs WHERE project_root=? ORDER BY created_at DESC LIMIT ?",
                    (str(Path(project_root).expanduser().resolve()), bounded_limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?",
                    (bounded_limit,),
                ).fetchall()
        return [self._decode(row) for row in rows]

    def claim_due(self) -> dict[str, Any] | None:
        now = time.time()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM jobs
                WHERE status IN ('queued', 'waiting') AND next_attempt_at <= ?
                ORDER BY created_at ASC LIMIT 1
                """,
                (now,),
            ).fetchone()
            if row is None:
                return None
            if row["deadline_at"] <= now:
                connection.execute(
                    "UPDATE jobs SET status='failed', updated_at=?, error=? WHERE id=?",
                    (now, "Overnight job deadline expired", row["id"]),
                )
                return None
            connection.execute(
                "UPDATE jobs SET status='running', updated_at=?, attempt_count=attempt_count+1 "
                "WHERE id=?",
                (now, row["id"]),
            )
        return self.get(row["id"])

    def complete(self, job_id: str, result: dict[str, Any]) -> None:
        now = time.time()
        with self.connection() as connection:
            connection.execute(
                "UPDATE jobs SET status='completed', updated_at=?, result_json=?, error=NULL "
                "WHERE id=?",
                (now, json.dumps(result, separators=(",", ":")), job_id),
            )

    def checkpoint_plan(
        self,
        job_id: str,
        plan: str,
        conversation_url: str | None,
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                "UPDATE jobs SET plan_text=?, conversation_url=COALESCE(?, conversation_url), "
                "updated_at=? WHERE id=?",
                (plan, conversation_url, time.time(), job_id),
            )

    def wait(
        self,
        job_id: str,
        *,
        next_attempt_at: float,
        error: str,
        conversation_url: str | None,
    ) -> None:
        now = time.time()
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE jobs SET status='waiting', updated_at=?, next_attempt_at=?,
                    error=?, conversation_url=COALESCE(?, conversation_url)
                WHERE id=?
                """,
                (now, next_attempt_at, error[-2_000:], conversation_url, job_id),
            )

    def fail(self, job_id: str, error: str, *, status: str = "failed") -> None:
        if status not in {"failed", "blocked_auth"}:
            raise ValueError(f"Invalid failure status: {status}")
        with self.connection() as connection:
            connection.execute(
                "UPDATE jobs SET status=?, updated_at=?, error=? WHERE id=?",
                (status, time.time(), error[-4_000:], job_id),
            )

    def cancel(self, job_id: str) -> dict[str, Any]:
        job = self.get(job_id)
        if job["status"] in TERMINAL_STATES:
            return job
        with self.connection() as connection:
            connection.execute(
                "UPDATE jobs SET status='cancelled', updated_at=?, error='Cancelled by user' "
                "WHERE id=?",
                (time.time(), job_id),
            )
        return self.get(job_id)

    def has_keep_awake_jobs(self) -> bool:
        placeholders = ",".join("?" for _ in PENDING_STATES)
        with self.connection() as connection:
            row = connection.execute(
                f"SELECT COUNT(*) AS count FROM jobs WHERE keep_awake=1 "
                f"AND status IN ({placeholders})",
                tuple(PENDING_STATES),
            ).fetchone()
        return bool(row["count"])

    def has_pending_jobs(self) -> bool:
        placeholders = ",".join("?" for _ in PENDING_STATES)
        with self.connection() as connection:
            row = connection.execute(
                f"SELECT COUNT(*) AS count FROM jobs WHERE status IN ({placeholders})",
                tuple(PENDING_STATES),
            ).fetchone()
        return bool(row["count"])

    def next_due_at(self) -> float | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT MIN(next_attempt_at) AS value FROM jobs "
                "WHERE status IN ('queued', 'waiting')"
            ).fetchone()
        return float(row["value"]) if row["value"] is not None else None


def public_job(job: dict[str, Any]) -> dict[str, Any]:
    now = time.time()
    return {
        "job_id": job["id"],
        "status": job["status"],
        "task": job["task"][:300],
        "project_root": job["project_root"],
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
        "next_attempt_in_seconds": max(0, int(job["next_attempt_at"] - now))
        if job["status"] == "waiting"
        else None,
        "deadline_at": job["deadline_at"],
        "attempt_count": job["attempt_count"],
        "keep_awake": job["keep_awake"],
        "autonomous": job["autonomous"],
        "plan_checkpointed": bool(job.get("plan_text")),
        "error": job["error"],
        "result": job["result"],
    }


class JobManager:
    def __init__(
        self,
        bridge: DeepSeekWebBridge,
        patch_agent: PatchAgent,
        operation_lock: asyncio.Lock,
        store: JobStore | None = None,
    ) -> None:
        self.bridge = bridge
        self.patch_agent = patch_agent
        self.operation_lock = operation_lock
        self.store = store or JobStore()
        self.wake = asyncio.Event()
        self.worker_task: asyncio.Task[None] | None = None
        self.current_operation: asyncio.Task[None] | None = None
        self.current_job_id: str | None = None
        self.stopping = False
        self.caffeinate: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        if self.worker_task is None:
            self.worker_task = asyncio.create_task(self._worker(), name="deepseek-job-worker")

    async def stop(self) -> None:
        self.stopping = True
        self.wake.set()
        if self.current_operation is not None:
            self.current_operation.cancel()
        if self.worker_task is not None:
            self.worker_task.cancel()
            try:
                await self.worker_task
            except asyncio.CancelledError:
                pass
        self._set_caffeinate(False)

    def submit(self, params: dict[str, Any]) -> dict[str, Any]:
        task = normalize_task(str(params.get("task", "")))
        requested_root = str(params.get("project_root", ""))
        root = Path(requested_root).expanduser().resolve()
        if not requested_root or not root.is_dir():
            raise ValueError(f"Project root does not exist: {requested_root}")
        project_root = str(root)
        paths = params.get("paths")
        if paths is not None and (
            not isinstance(paths, list) or not all(isinstance(path, str) for path in paths)
        ):
            raise ValueError("paths must be a list of repository-relative strings")
        if paths is not None:
            paths = sorted(dict.fromkeys(path.strip() for path in paths if path.strip()))
        max_wait_hours = max(1, min(int(params.get("max_wait_hours", 12)), 24))
        max_repairs = max(0, min(int(params.get("max_repairs", 2)), 2))
        apply_changes = bool(params.get("apply_changes", True))
        autonomous = bool(params.get("autonomous", False))
        test_command = params.get("test_command")
        if test_command is not None and not isinstance(test_command, str):
            raise ValueError("test_command must be a string")
        test_command = test_command.strip() or None if test_command is not None else None
        duplicate = self.store.find_pending_duplicate(
            task=task,
            project_root=project_root,
            paths=paths,
            test_command=test_command,
            apply_changes=apply_changes,
            autonomous=autonomous,
        )
        if duplicate is not None:
            result = public_job(duplicate)
            result["deduplicated"] = True
            return result
        job = self.store.submit(
            task=task,
            project_root=project_root,
            paths=paths,
            test_command=test_command,
            apply_changes=apply_changes,
            max_repairs=max_repairs,
            keep_awake=bool(params.get("keep_awake", True)),
            autonomous=autonomous,
            max_wait_hours=max_wait_hours,
        )
        self.wake.set()
        self._refresh_caffeinate()
        result = public_job(job)
        result["deduplicated"] = False
        return result

    def status(self, job_id: str) -> dict[str, Any]:
        return public_job(self.store.get(job_id))

    def list(self, limit: int, project_root: str | None = None) -> list[dict[str, Any]]:
        return [public_job(job) for job in self.store.list(limit, project_root)]

    def has_pending_jobs(self) -> bool:
        return self.store.has_pending_jobs()

    def cancel(self, job_id: str) -> dict[str, Any]:
        job = self.store.cancel(job_id)
        if self.current_job_id == job_id and self.current_operation is not None:
            self.current_operation.cancel()
        self.wake.set()
        self._refresh_caffeinate()
        return public_job(job)

    def _set_caffeinate(self, enabled: bool) -> None:
        running = self.caffeinate is not None and self.caffeinate.poll() is None
        if enabled and not running:
            self.caffeinate = subprocess.Popen(
                ["/usr/bin/caffeinate", "-im"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        elif not enabled and running:
            self.caffeinate.terminate()
            self.caffeinate = None

    def _refresh_caffeinate(self) -> None:
        self._set_caffeinate(self.store.has_keep_awake_jobs())

    async def _worker(self) -> None:
        while not self.stopping:
            self._refresh_caffeinate()
            job = self.store.claim_due()
            if job is not None:
                self.current_job_id = job["id"]
                self.current_operation = asyncio.create_task(self._process(job))
                try:
                    await self.current_operation
                except asyncio.CancelledError:
                    if self.stopping:
                        raise
                finally:
                    self.current_operation = None
                    self.current_job_id = None
                    self._refresh_caffeinate()
                continue

            due = self.store.next_due_at()
            timeout = 60.0 if due is None else max(1.0, min(60.0, due - time.time()))
            self.wake.clear()
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass

    async def _process(self, job: dict[str, Any]) -> None:
        model = "expert"
        try:
            now = time.time()
            previous_error = (job.get("error") or "").lower()
            if (
                self.bridge.cooldown_until("expert") > now
                or "instant fallback scheduled" in previous_error
            ):
                model = "instant"

            available_at = self.bridge.cooldown_until(model)
            if available_at > now:
                self.store.wait(
                    job["id"],
                    next_attempt_at=available_at,
                    error=f"Waiting for the persisted {model.title()} cooldown or hourly budget",
                    conversation_url=job["conversation_url"],
                )
                return
            async with self.operation_lock:
                result = await self.patch_agent.run(
                    task=job["task"],
                    project_root=job["project_root"],
                    paths=job["paths"],
                    test_command=job["test_command"],
                    apply_changes=job["apply_changes"],
                    max_repairs=job["max_repairs"],
                    conversation_url=job["conversation_url"] if model == "expert" else None,
                    model=model,
                    autonomous=job["autonomous"],
                    project_plan=job["plan_text"],
                    checkpoint_plan=lambda plan, url: self.store.checkpoint_plan(
                        job["id"], plan, url
                    ),
                )
            self.store.complete(job["id"], result)
        except asyncio.CancelledError:
            current = self.store.get(job["id"])
            if current["status"] != "cancelled":
                self.store.wait(
                    job["id"],
                    next_attempt_at=time.time() + 60,
                    error="Daemon stopped; job safely requeued",
                    conversation_url=self.bridge.current_conversation_url(),
                )
            raise
        except DeepSeekBusyError as exc:
            if model == "expert":
                next_attempt = time.time() + 60
                error = f"{exc} Instant fallback scheduled for the next attempt."
                conversation_url = None
            else:
                next_attempt = max(time.time() + 60, self.bridge.cooldown_until("instant"))
                error = str(exc)
                conversation_url = self.bridge.current_conversation_url()
            self.store.wait(
                job["id"],
                next_attempt_at=next_attempt,
                error=error,
                conversation_url=conversation_url,
            )
        except AuthenticationRequired as exc:
            self.store.fail(job["id"], str(exc), status="blocked_auth")
        except Exception as exc:
            current = self.store.get(job["id"])
            if current["attempt_count"] < 3 and time.time() + 1_800 < current["deadline_at"]:
                self.store.wait(
                    job["id"],
                    next_attempt_at=time.time() + 1_800 * current["attempt_count"],
                    error=f"{type(exc).__name__}: {exc}",
                    conversation_url=self.bridge.current_conversation_url(),
                )
            else:
                self.store.fail(job["id"], f"{type(exc).__name__}: {exc}")
