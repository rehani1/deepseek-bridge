from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from .bridge import DeepSeekWebBridge
from .client import MAX_MESSAGE_BYTES, SOCKET_PATH, STATE_DIR
from .jobs import JobManager
from .patch_agent import PatchAgent


LOG_PATH = STATE_DIR / "bridge.log"


def configure_logging() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    handler = RotatingFileHandler(LOG_PATH, maxBytes=2_000_000, backupCount=3)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])


class BridgeDaemon:
    def __init__(self) -> None:
        self.bridge = DeepSeekWebBridge()
        self.patch_agent = PatchAgent(self.bridge)
        self.server: asyncio.AbstractServer | None = None
        self.operation_lock = asyncio.Lock()
        self.jobs = JobManager(self.bridge, self.patch_agent, self.operation_lock)

    async def dispatch(self, method: str, params: dict[str, Any]) -> Any:
        if method == "job_submit":
            return self.jobs.submit(params)
        if method == "job_status":
            return self.jobs.status(str(params.get("job_id", "")))
        if method == "job_list":
            project_root = params.get("project_root")
            return self.jobs.list(
                int(params.get("limit", 10)),
                str(project_root) if project_root else None,
            )
        if method == "job_cancel":
            return self.jobs.cancel(str(params.get("job_id", "")))
        async with self.operation_lock:
            if method == "show_browser":
                return await self.bridge.show_browser()
            if method == "last_response":
                return await self.bridge.last_response()
            if method == "status":
                return await self.bridge.status()
            if method == "generate":
                if self.jobs.has_pending_jobs():
                    raise RuntimeError(
                        "An overnight Expert job is queued or running; direct Expert generation "
                        "is paused to preserve its request budget and conversation."
                    )
                return await self.bridge.generate(str(params.get("task", "")))
            if method == "expert":
                if self.jobs.has_pending_jobs():
                    raise RuntimeError(
                        "An overnight Expert job is queued or running; direct Expert queries are "
                        "paused. Use deepseek_job_status instead."
                    )
                return await self.bridge.expert(str(params.get("query", "")))
            if method == "search":
                return await self.bridge.search(str(params.get("query", "")))
        raise ValueError(f"Unknown daemon method: {method}")

    async def handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        request_id: str | None = None
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=30)
            if not raw or len(raw) >= MAX_MESSAGE_BYTES:
                raise ValueError("Invalid or oversized daemon request")
            request = json.loads(raw)
            request_id = request.get("id")
            method = request.get("method")
            params = request.get("params", {})
            if not isinstance(request_id, str) or not isinstance(method, str):
                raise ValueError("Daemon request requires string id and method")
            if not isinstance(params, dict):
                raise ValueError("Daemon params must be an object")
            result = await self.dispatch(method, params)
            response = {"id": request_id, "result": result}
        except Exception as exc:
            logging.exception("Bridge request failed")
            response = {
                "id": request_id,
                "error": {"type": type(exc).__name__, "message": str(exc)},
            }

        encoded = json.dumps(response, separators=(",", ":")).encode() + b"\n"
        writer.write(encoded)
        try:
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def run(self) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        STATE_DIR.chmod(0o700)
        if SOCKET_PATH.exists():
            SOCKET_PATH.unlink()
        self.server = await asyncio.start_unix_server(
            self.handle_client,
            path=str(SOCKET_PATH),
            limit=MAX_MESSAGE_BYTES,
        )
        SOCKET_PATH.chmod(0o600)
        self.jobs.start()
        logging.info("DeepSeek bridge daemon ready on %s", SOCKET_PATH)

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, stop.set)
        await stop.wait()

        self.server.close()
        await self.server.wait_closed()
        await self.jobs.stop()
        await self.bridge.close()
        if SOCKET_PATH.exists():
            SOCKET_PATH.unlink()


def main() -> None:
    configure_logging()
    asyncio.run(BridgeDaemon().run())


if __name__ == "__main__":
    main()
