from __future__ import annotations

import json
import hashlib
import logging
import os
import subprocess
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .client import call_daemon


MAX_PLAN_CHARS = 40_000
PLAN_SUFFIXES = {".md", ".txt"}


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stderr,
)


def snapshot_project_plan(project_root: str, task: str, plan_path: str | None) -> str:
    """Build a durable task whose plan content no longer depends on Claude's context."""
    objective = task.strip()
    if not plan_path:
        if not objective:
            raise ValueError("task must not be empty when plan_path is not provided")
        return objective

    root = Path(project_root).expanduser().resolve()
    relative = Path(plan_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("plan_path must be repository-relative and may not contain '..'")
    unresolved = root / relative
    if unresolved.is_symlink():
        raise ValueError("plan_path may not be a symlink")
    candidate = unresolved.resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("plan_path escapes the project repository") from exc
    if candidate.suffix.lower() not in PLAN_SUFFIXES:
        raise ValueError("plan_path must be a Markdown or text file")
    if not candidate.is_file():
        raise ValueError(f"plan_path does not exist: {plan_path}")
    try:
        plan = candidate.read_text(encoding="utf-8").strip()
    except UnicodeDecodeError as exc:
        raise ValueError("plan_path must contain UTF-8 text") from exc
    if not plan:
        raise ValueError("plan_path is empty")
    if len(plan) > MAX_PLAN_CHARS:
        raise ValueError(f"plan_path exceeds the {MAX_PLAN_CHARS:,}-character limit")
    digest = hashlib.sha256(plan.encode()).hexdigest()
    objective = objective or "Execute the durable project plan and satisfy all acceptance criteria."
    return (
        f"{objective}\n\n"
        f"Durable plan snapshot (source: {relative.as_posix()}, sha256: {digest}):\n"
        f"{plan}"
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
        "When the user names a project phase without supplying its full plan, set autonomous=true; "
        "DeepSeek will inspect the repository, derive and checkpoint its own plan, then implement it. "
        "use deepseek_job_status when the user later asks. The daemon waits through Expert capacity "
        "limits, tests and repairs the change, and stores a concise result. "
        "Use deepseek_expert for delegated analysis, deepseek_generate for code returned in chat, "
        "and deepseek_search for current web research. Expert/generate/patch use actual Expert "
        "mode when available and use one controlled Instant fallback; search uses Instant mode. "
        "Use deepseek_last_response to recover the latest completed browser answer without "
        "submitting another request. "
        "Review untrusted generated changes before execution."
    ),
    log_level="WARNING",
)


@mcp.tool()
async def deepseek_patch(
    task: str = "",
    plan_path: str | None = None,
    autonomous: bool = False,
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
        task: A concise objective, or a complete task when plan_path is omitted.
        plan_path: Optional repository-relative Markdown/text plan. Its content is snapshotted into
            the durable job before this tool returns, so later edits or chat loss cannot change it.
        autonomous: Have DeepSeek independently audit the repository, derive and checkpoint the
            execution plan, then implement it in the same conversation. Use for named phases when
            Claude should not create or carry the plan.
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
    durable_task = snapshot_project_plan(project_root, task, plan_path)
    await call_daemon("show_browser", timeout=60)
    surface_chromium_app()
    await call_daemon("show_browser", timeout=60)
    result = await call_daemon(
        "job_submit",
        {
            "task": durable_task,
            "autonomous": autonomous,
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
async def deepseek_jobs(limit: int = 10, current_project_only: bool = True) -> str:
    """List recent durable jobs, scoped to the open project by default, without contacting DeepSeek."""
    project_root = os.environ.get("CLAUDE_PROJECT_DIR") if current_project_only else None
    result = await call_daemon(
        "job_list",
        {"limit": limit, "project_root": project_root},
        timeout=30,
    )
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


@mcp.tool()
async def deepseek_last_response() -> str:
    """Retrieve the latest completed Chromium response without sending a DeepSeek query."""
    return str(await call_daemon("last_response", timeout=210))


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
