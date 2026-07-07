from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Any


STATE_DIR = Path.home() / "Library" / "Application Support" / "deepseek-bridge"
SOCKET_PATH = STATE_DIR / "bridge.sock"
MAX_MESSAGE_BYTES = 1_000_000


class DaemonClientError(RuntimeError):
    pass


async def _kickstart_daemon() -> None:
    process = await asyncio.create_subprocess_exec(
        "/bin/launchctl",
        "kickstart",
        "-k",
        f"gui/{os.getuid()}/com.deepseek.bridge",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await process.wait()


async def call_daemon(
    method: str,
    params: dict[str, Any] | None = None,
    *,
    timeout: float = 300,
) -> Any:
    request = {
        "id": uuid.uuid4().hex,
        "method": method,
        "params": params or {},
    }

    last_error: Exception | None = None
    for attempt in range(2):
        try:
            reader, writer = await asyncio.open_unix_connection(
                str(SOCKET_PATH),
                limit=MAX_MESSAGE_BYTES,
            )
            break
        except (FileNotFoundError, ConnectionRefusedError, OSError) as exc:
            last_error = exc
            if attempt == 0:
                await _kickstart_daemon()
                await asyncio.sleep(1)
    else:
        raise DaemonClientError(
            "DeepSeek daemon is unavailable. Run: launchctl kickstart -k "
            f"gui/{os.getuid()}/com.deepseek.bridge"
        ) from last_error

    try:
        payload = json.dumps(request, separators=(",", ":")).encode() + b"\n"
        writer.write(payload)
        await writer.drain()
        raw = await asyncio.wait_for(reader.readline(), timeout=timeout)
        if not raw:
            raise DaemonClientError("DeepSeek daemon closed the connection without a response")
        response = json.loads(raw)
        if response.get("id") != request["id"]:
            raise DaemonClientError("DeepSeek daemon returned a mismatched response")
        if "error" in response:
            error = response["error"]
            raise DaemonClientError(f"{error.get('type', 'BridgeError')}: {error.get('message')}")
        return response.get("result")
    finally:
        writer.close()
        await writer.wait_closed()
