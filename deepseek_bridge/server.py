from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .client import call_daemon


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stderr,
)


def surface_chromium_app() -> None:
    cache = Path.home() / "Library" / "Caches" / "ms-playwright"
    candidates = sorted(
        cache.glob("chromium-*/chrome-mac-arm64/Google Chrome for Testing.app")
    )
    if not candidates:
        return
    subprocess.run(
        ["/usr/bin/open", "-a", str(candidates[-1])],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )

mcp = FastMCP(
    "deepseek-web-bridge",
    instructions=(
        "Use deepseek_patch for substantial repository implementation: it edits an isolated Git "
        "worktree asynchronously and immediately returns a durable job ID. Do not poll repeatedly; "
        "use deepseek_job_status when the user later asks. The daemon waits through Expert capacity "
        "limits, tests and repairs the change, and stores a concise result. "
        "Use deepseek_expert for delegated analysis, deepseek_generate for code returned in chat, "
        "and deepseek_search for current web research. Expert/generate/patch use actual Expert "
        "mode when available and use one controlled Instant fallback; search uses Instant mode. "
        "Review untrusted generated changes before execution."
    ),
    log_level="WARNING",
)


@mcp.tool()
async def deepseek_patch(
    task: str,
    paths: list[str] | None = None,
    test_command: str | None = None,
    apply_changes: bool = True,
    max_repairs: int = 2,
    keep_awake: bool = True,
    max_wait_hours: int = 12,
) -> str:
    """Queue a durable background implementation job using Expert mode and DeepThink.

    Returns immediately with a job ID. The persistent daemon serializes Expert requests, reuses
    one conversation for the task, waits through capacity cooldowns, validates paths, optionally
    runs network-blocked tests, and repairs failures up to twice.

    Args:
        task: A self-contained implementation objective and acceptance criteria.
        paths: Optional repository-relative files or directories that may be changed.
        test_command: Optional test/lint command to run in the isolated worktree.
        apply_changes: Apply a validated patch to the working tree; false saves a dry-run patch.
        max_repairs: Number of DeepSeek test-repair attempts, from 0 to 2.
        keep_awake: Prevent idle system sleep while this job is queued or running.
        max_wait_hours: Durable job deadline, from 1 to 24 hours.
    """
    project_root = os.environ.get("CLAUDE_PROJECT_DIR")
    if not project_root:
        raise RuntimeError("CLAUDE_PROJECT_DIR is unavailable; open Claude Code in a Git project")
    await call_daemon("show_browser", timeout=60)
    surface_chromium_app()
    await call_daemon("show_browser", timeout=60)
    result = await call_daemon(
        "job_submit",
        {
            "task": task,
            "project_root": project_root,
            "paths": paths,
            "test_command": test_command,
            "apply_changes": apply_changes,
            "max_repairs": max_repairs,
            "keep_awake": keep_awake,
            "max_wait_hours": max_wait_hours,
        },
        timeout=30,
    )
    return json.dumps(result, separators=(",", ":"))


@mcp.tool()
async def deepseek_job_status(job_id: str) -> str:
    """Get one queued DeepSeek job's current state and final result, if available."""
    result = await call_daemon("job_status", {"job_id": job_id}, timeout=30)
    return json.dumps(result, separators=(",", ":"))


@mcp.tool()
async def deepseek_jobs(limit: int = 10) -> str:
    """List recent DeepSeek background jobs without contacting DeepSeek."""
    result = await call_daemon("job_list", {"limit": limit}, timeout=30)
    return json.dumps(result, separators=(",", ":"))


@mcp.tool()
async def deepseek_cancel_job(job_id: str) -> str:
    """Cancel a queued, waiting, or running DeepSeek background job."""
    result = await call_daemon("job_cancel", {"job_id": job_id}, timeout=30)
    return json.dumps(result, separators=(",", ":"))


@mcp.tool()
async def deepseek_generate(task: str) -> str:
    """Generate code in chat using the DeepSeek Expert prompt with DeepThink enabled."""
    return str(await call_daemon("generate", {"task": task}, timeout=300))


@mcp.tool()
async def deepseek_expert(query: str) -> str:
    """Answer a technical query using the DeepSeek Expert prompt with DeepThink enabled."""
    return str(await call_daemon("expert", {"query": query}, timeout=300))


@mcp.tool()
async def deepseek_search(query: str) -> str:
    """Search the web using DeepSeek Instant mode and return a sourced answer."""
    return str(await call_daemon("search", {"query": query}, timeout=300))


@mcp.tool()
async def deepseek_status() -> str:
    """Report daemon, browser-session, and DeepSeek authentication status."""
    return str(await call_daemon("status", timeout=60))


@mcp.tool()
async def deepseek_show_browser() -> str:
    """Surface the bridge-owned Chromium window without submitting a DeepSeek query."""
    result = str(await call_daemon("show_browser", timeout=60))
    surface_chromium_app()
    await call_daemon("show_browser", timeout=60)
    return result


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
