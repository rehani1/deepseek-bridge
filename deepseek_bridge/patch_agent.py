from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable

from .bridge import normalize_task
from .client import STATE_DIR

if TYPE_CHECKING:
    from .bridge import DeepSeekWebBridge


MAX_CONTEXT_CHARS = 100_000
MAX_FILE_CHARS = 24_000
MAX_PATCH_BYTES = 500_000
MAX_CHANGED_FILES = 30
MAX_DELETED_LINES = 5_000
MAX_TEST_OUTPUT_CHARS = 20_000
MAX_UNTRACKED_BYTES = 20_000_000
MAX_AUTONOMOUS_PLAN_CHARS = 24_000

SENSITIVE_NAMES = {
    ".env",
    ".npmrc",
    ".pypirc",
    "credentials",
    "credentials.json",
    "id_rsa",
    "id_ed25519",
    "secrets.json",
}
SENSITIVE_SUFFIXES = {".key", ".pem", ".p12", ".pfx", ".keystore"}
MANIFEST_NAMES = {
    "agents.md",
    "AGENTS.md",
    "README.md",
    "package.json",
    "pyproject.toml",
    "requirements.txt",
    "Cargo.toml",
    "go.mod",
    "pom.xml",
    "build.gradle",
    "Makefile",
}
STOP_WORDS = {
    "about",
    "after",
    "before",
    "build",
    "change",
    "code",
    "create",
    "file",
    "from",
    "have",
    "implementation",
    "into",
    "make",
    "project",
    "should",
    "that",
    "this",
    "using",
    "with",
}


class PatchAgentError(RuntimeError):
    pass


def _run(
    args: list[str],
    *,
    cwd: Path,
    input_bytes: bytes | None = None,
    timeout: int = 120,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(
            args,
            cwd=cwd,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise PatchAgentError(f"Command timed out after {timeout}s: {args[0]}") from exc
    if check and result.returncode:
        message = (result.stderr or result.stdout).decode(errors="replace")[-4_000:]
        raise PatchAgentError(f"Command failed ({result.returncode}): {' '.join(args)}\n{message}")
    return result


def is_sensitive_path(path: Path) -> bool:
    lowered = [part.lower() for part in path.parts]
    name = path.name.lower()
    return (
        name in SENSITIVE_NAMES
        or name.startswith(".env.")
        or path.suffix.lower() in SENSITIVE_SUFFIXES
        or ".git" in lowered
        or "node_modules" in lowered
        or "__pycache__" in lowered
    )


def _safe_relative(root: Path, value: str | Path) -> Path:
    candidate = (root / value).resolve() if not Path(value).is_absolute() else Path(value).resolve()
    try:
        relative = candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise PatchAgentError(f"Path escapes repository: {value}") from exc
    if is_sensitive_path(relative):
        raise PatchAgentError(f"Sensitive path is not allowed: {relative}")
    return relative


def list_repo_files(root: Path) -> list[Path]:
    raw = _run(
        ["git", "ls-files", "-co", "--exclude-standard", "-z"],
        cwd=root,
    ).stdout
    files: list[Path] = []
    for item in raw.split(b"\0"):
        if not item:
            continue
        relative = Path(item.decode(errors="surrogateescape"))
        path = root / relative
        if path.is_file() and not path.is_symlink() and not is_sensitive_path(relative):
            files.append(relative)
    return files


def resolve_requested_paths(root: Path, values: list[str] | None) -> list[Path]:
    if not values:
        return []
    resolved: list[Path] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise PatchAgentError("paths must contain non-empty strings")
        relative = _safe_relative(root, value)
        resolved.append(relative)
    return sorted(set(resolved), key=str)


def _read_text(path: Path) -> str | None:
    try:
        raw = path.read_bytes()
    except (OSError, PermissionError):
        return None
    if b"\0" in raw[:4_096]:
        return None
    return raw.decode("utf-8", errors="replace")


def select_context_files(
    root: Path,
    task: str,
    requested: list[Path],
) -> list[Path]:
    all_files = list_repo_files(root)
    if requested:
        selected: list[Path] = []
        for relative in requested:
            target = root / relative
            if target.is_dir():
                selected.extend(path for path in all_files if relative in path.parents)
            elif target.is_file():
                selected.append(relative)
        return sorted(set(selected), key=str)[:30]

    terms = [
        term.lower()
        for term in re.findall(r"[A-Za-z_][A-Za-z0-9_-]{2,}", task)
        if term.lower() not in STOP_WORDS
    ][:12]
    scored: dict[Path, int] = {}
    for relative in all_files:
        name = str(relative).lower()
        score = sum(8 for term in terms if term in name)
        if relative.name in MANIFEST_NAMES:
            score += 4
        if score:
            scored[relative] = score

    if terms and shutil.which("rg"):
        pattern = "|".join(re.escape(term) for term in terms[:8])
        result = _run(
            ["rg", "-l", "-i", "--glob", "!*.lock", pattern, "."],
            cwd=root,
            timeout=30,
            check=False,
        )
        for line in result.stdout.decode(errors="replace").splitlines():
            relative = Path(line.removeprefix("./"))
            if relative in all_files:
                scored[relative] = scored.get(relative, 0) + 5

    ranked = sorted(scored, key=lambda item: (-scored[item], len(str(item)), str(item)))
    if not ranked:
        ranked = sorted(
            (path for path in all_files if path.name in MANIFEST_NAMES),
            key=str,
        )
    return ranked[:16]


def build_context(root: Path, task: str, requested: list[Path]) -> tuple[str, list[str]]:
    all_files = list_repo_files(root)
    selected = select_context_files(root, task, requested)
    manifest = "\n".join(str(path) for path in all_files[:1_000])
    sections = [f"Repository files:\n{manifest[:25_000]}"]
    used: list[str] = []
    size = len(sections[0])

    for relative in selected:
        content = _read_text(root / relative)
        if content is None:
            continue
        content = content[:MAX_FILE_CHARS]
        section = f"\n\n--- FILE: {relative} ---\n{content}"
        if size + len(section) > MAX_CONTEXT_CHARS:
            break
        sections.append(section)
        used.append(str(relative))
        size += len(section)
    return "".join(sections), used


def extract_unified_diff(value: str) -> str:
    text = value.strip()
    fence = re.fullmatch(r"```(?:diff|patch)?\s*\n(.*?)\n?```", text, flags=re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    start = text.find("diff --git ")
    if start < 0:
        raise PatchAgentError("DeepSeek did not return a unified Git diff")
    text = text[start:]
    if text.endswith("```"):
        text = text[:-3].rstrip()
    if len(text.encode()) > MAX_PATCH_BYTES:
        raise PatchAgentError("Generated patch exceeds the 500 KB safety limit")
    return text + ("" if text.endswith("\n") else "\n")


def changed_paths(patch: str) -> list[Path]:
    paths: set[Path] = set()
    old_path: Path | None = None
    for line in patch.splitlines():
        if line.startswith("--- "):
            value = line[4:].split("\t", 1)[0]
            old_path = None if value == "/dev/null" else Path(value.removeprefix("a/"))
        elif line.startswith("+++ "):
            value = line[4:].split("\t", 1)[0]
            path = None if value == "/dev/null" else Path(value.removeprefix("b/"))
            selected = path or old_path
            if selected is not None:
                paths.add(selected)
    return sorted(paths, key=str)


def validate_patch(patch: str, allowed: list[Path]) -> list[Path]:
    if "GIT binary patch" in patch or "Binary files " in patch:
        raise PatchAgentError("Binary patches are not allowed")
    if "new file mode 120000" in patch or "new file mode 160000" in patch:
        raise PatchAgentError("Symlink and submodule patches are not allowed")
    if "rename from " in patch or "rename to " in patch:
        raise PatchAgentError("Rename-only patches are not allowed")

    paths = changed_paths(patch)
    if not paths:
        raise PatchAgentError("Generated patch contains no changed paths")
    if len(paths) > MAX_CHANGED_FILES:
        raise PatchAgentError(f"Patch changes more than {MAX_CHANGED_FILES} files")
    deleted_lines = sum(
        1
        for line in patch.splitlines()
        if line.startswith("-") and not line.startswith("---")
    )
    if deleted_lines > MAX_DELETED_LINES:
        raise PatchAgentError(f"Patch deletes more than {MAX_DELETED_LINES} lines")

    for path in paths:
        if path.is_absolute() or ".." in path.parts or is_sensitive_path(path):
            raise PatchAgentError(f"Unsafe patch path: {path}")
        if allowed and not any(path == base or base in path.parents for base in allowed):
            raise PatchAgentError(f"Patch changed a path outside the allowlist: {path}")
    return paths


def repository_fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    digest.update(_run(["git", "status", "--porcelain=v1", "-z"], cwd=root).stdout)
    digest.update(_run(["git", "diff", "--binary", "HEAD"], cwd=root).stdout)
    untracked = _run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=root,
    ).stdout
    for item in untracked.split(b"\0"):
        if not item:
            continue
        relative = Path(item.decode(errors="surrogateescape"))
        if is_sensitive_path(relative):
            continue
        path = root / relative
        digest.update(item)
        try:
            digest.update(path.read_bytes())
        except OSError:
            pass
    return digest.hexdigest()


@dataclass
class TestResult:
    status: str
    output: str
    returncode: int | None


class RepoSandbox:
    def __init__(self, project_root: Path) -> None:
        requested_root = project_root.expanduser().resolve()
        if not requested_root.is_dir():
            raise PatchAgentError(f"Project root does not exist: {requested_root}")
        top = _run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=requested_root,
        ).stdout.decode().strip()
        self.root = Path(top).resolve()
        unresolved = _run(
            ["git", "diff", "--name-only", "--diff-filter=U"],
            cwd=self.root,
        ).stdout.decode().strip()
        if unresolved:
            raise PatchAgentError("Repository has unresolved merge conflicts")

        cache = Path.home() / "Library" / "Caches" / "deepseek-bridge" / "worktrees"
        cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.temp_root = Path(tempfile.mkdtemp(prefix="patch-", dir=cache))
        self.path = self.temp_root / "repo"
        self.home = self.temp_root / "home"
        self.home.mkdir(mode=0o700)
        self.original_fingerprint = repository_fingerprint(self.root)

    def __enter__(self) -> "RepoSandbox":
        _run(
            ["git", "worktree", "add", "--detach", str(self.path), "HEAD"],
            cwd=self.root,
            timeout=180,
        )
        working_diff = _run(
            ["git", "diff", "--binary", "HEAD"],
            cwd=self.root,
        ).stdout
        if working_diff:
            _run(["git", "apply", "--binary", "-"], cwd=self.path, input_bytes=working_diff)

        copied = 0
        raw = _run(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
            cwd=self.root,
        ).stdout
        for item in raw.split(b"\0"):
            if not item:
                continue
            relative = Path(item.decode(errors="surrogateescape"))
            source = self.root / relative
            if is_sensitive_path(relative) or source.is_symlink() or not source.is_file():
                continue
            size = source.stat().st_size
            if copied + size > MAX_UNTRACKED_BYTES:
                continue
            target = self.path / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied += size

        _run(["git", "add", "-A"], cwd=self.path)
        dirty = _run(["git", "status", "--porcelain"], cwd=self.path).stdout
        if dirty:
            _run(
                [
                    "git",
                    "-c",
                    "user.name=DeepSeek Bridge",
                    "-c",
                    "user.email=bridge@localhost",
                    "commit",
                    "--no-gpg-sign",
                    "-m",
                    "DeepSeek bridge baseline",
                ],
                cwd=self.path,
            )
        return self

    def __exit__(self, *_: object) -> None:
        _run(
            ["git", "worktree", "remove", "--force", str(self.path)],
            cwd=self.root,
            check=False,
            timeout=120,
        )
        _run(["git", "worktree", "prune"], cwd=self.root, check=False)
        shutil.rmtree(self.temp_root, ignore_errors=True)

    def reset_to_patch(self, patch: str) -> None:
        _run(["git", "reset", "--hard", "HEAD"], cwd=self.path)
        _run(["git", "clean", "-fdx"], cwd=self.path)
        if patch:
            _run(["git", "apply", "--binary", "-"], cwd=self.path, input_bytes=patch.encode())

    def current_patch(self) -> str:
        return _run(["git", "diff", "--binary", "HEAD"], cwd=self.path).stdout.decode(
            errors="replace"
        )

    def apply_increment(self, patch: str) -> None:
        encoded = patch.encode()
        _run(
            ["git", "apply", "--recount", "--check", "-"],
            cwd=self.path,
            input_bytes=encoded,
        )
        _run(
            ["git", "apply", "--recount", "--binary", "-"],
            cwd=self.path,
            input_bytes=encoded,
        )

    def run_tests(self, command: str | None) -> TestResult:
        if not command:
            return TestResult("not_run", "No test command supplied", None)
        policy_parts = ["(version 1)", "(allow default)", "(deny network*)"]
        for protected in (".ssh", ".aws", ".config/gcloud", ".kube", ".gnupg"):
            path = Path.home() / protected
            policy_parts.append(f'(deny file-read* (subpath "{path}"))')
        policy_parts.append(f'(deny file-write* (subpath "{self.root}"))')
        policy = "".join(policy_parts)
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
            "HOME": str(self.home),
            "TMPDIR": str(self.temp_root),
            "LANG": os.environ.get("LANG", "en_US.UTF-8"),
            "CI": "1",
            "NO_PROXY": "*",
            "no_proxy": "*",
        }
        result = _run(
            ["/usr/bin/sandbox-exec", "-p", policy, "/bin/zsh", "-lc", command],
            cwd=self.path,
            timeout=300,
            check=False,
            env=env,
        )
        combined = (result.stdout + b"\n" + result.stderr).decode(errors="replace")
        combined = combined[-MAX_TEST_OUTPUT_CHARS:].strip()
        status = "passed" if result.returncode == 0 else "failed"
        return TestResult(status, combined, result.returncode)

    def apply_to_original(self, patch: str) -> None:
        if repository_fingerprint(self.root) != self.original_fingerprint:
            raise PatchAgentError("Repository changed while DeepSeek was working; patch was not applied")
        encoded = patch.encode()
        _run(
            ["git", "apply", "--recount", "--check", "-"],
            cwd=self.root,
            input_bytes=encoded,
        )
        _run(
            ["git", "apply", "--recount", "--binary", "-"],
            cwd=self.root,
            input_bytes=encoded,
        )


def _allowed_description(allowed: list[Path]) -> str:
    if not allowed:
        return "Any non-sensitive repository path (maximum 30 changed files)."
    return "\n".join(f"- {path}" for path in allowed)


def initial_patch_prompt(task: str, allowed: list[Path], context: str) -> str:
    return f"""Create a production-ready implementation as a unified Git diff.

Rules:
- Return only a raw unified diff beginning with `diff --git`, or one `diff` code block.
- Use repository-relative paths with `a/` and `b/` prefixes.
- Include complete diff hunks; do not use placeholders or ellipses.
- Do not modify credentials, environment files, binaries, symlinks, or submodules.
- Keep the change tightly scoped and preserve existing conventions.

Allowed paths:
{_allowed_description(allowed)}

Task:
{task}

Repository context:
{context}
"""


def autonomous_plan_prompt(task: str, context: str) -> str:
    return f"""Determine and plan the requested project phase independently.

Use the repository requirements, manifests, file inventory, and current implementation below as
the source of truth. Identify what is already implemented before choosing changes. Do not assume
that a checklist item is missing merely because the task is brief. Keep the plan coherent enough
for one patch job and explicitly include verification steps.

Requested phase or objective:
{task}

Repository snapshot:
{context}
"""


def repair_patch_prompt(
    task: str,
    allowed: list[Path],
    context: str,
    current_patch: str,
    test_command: str,
    test_output: str,
) -> str:
    return f"""Repair the current implementation based on the test failure.

Return only an incremental unified Git diff that applies on top of the current patch.
Follow the same path and safety constraints.

Allowed paths:
{_allowed_description(allowed)}

Original task:
{task}

Current patch:
{current_patch[:60_000]}

Test command:
{test_command}

Test output:
{test_output[-12_000:]}

Relevant repository context:
{context[:40_000]}
"""


def malformed_patch_prompt(original_request: str, previous: str, error: str) -> str:
    return f"""Correct the malformed unified Git diff below.

Return a complete replacement diff against the original repository state, not an incremental
patch. Return only the corrected raw diff beginning with `diff --git`. Ensure every hunk line has
the required leading space, plus, or minus and that all file and hunk headers are valid.

Original request:
{original_request[:120_000]}

Invalid output:
{previous[:40_000]}

Git validation error:
{error[-4_000:]}
"""


class PatchAgent:
    def __init__(self, bridge: "DeepSeekWebBridge") -> None:
        self.bridge = bridge

    async def run(
        self,
        task: str,
        project_root: str,
        paths: list[str] | None = None,
        test_command: str | None = None,
        apply_changes: bool = True,
        max_repairs: int = 2,
        conversation_url: str | None = None,
        model: str = "expert",
        autonomous: bool = False,
        project_plan: str | None = None,
        checkpoint_plan: Callable[[str, str | None], None] | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        clean_task = normalize_task(task)
        if not isinstance(project_root, str) or not project_root:
            raise PatchAgentError("project_root is required")
        max_repairs = max(0, min(int(max_repairs), 2))
        patch_id = uuid.uuid4().hex[:12]

        with RepoSandbox(Path(project_root)) as sandbox:
            if conversation_url and model == "expert":
                await self.bridge.resume_task(conversation_url)
            else:
                await self.bridge.begin_task()
            allowed = resolve_requested_paths(sandbox.root, paths)
            context, context_files = build_context(
                sandbox.path,
                clean_task,
                [] if autonomous else allowed,
            )
            if autonomous:
                plan = (project_plan or "").strip()
                if not plan:
                    plan = (
                        await self.bridge.plan(
                            autonomous_plan_prompt(clean_task, context),
                            model=model,
                        )
                    ).strip()
                    if not plan:
                        raise PatchAgentError("DeepSeek returned an empty autonomous project plan")
                    plan = plan[:MAX_AUTONOMOUS_PLAN_CHARS]
                    if checkpoint_plan is not None:
                        checkpoint_plan(plan, self.bridge.current_conversation_url())
                clean_task = (
                    f"{clean_task}\n\n"
                    "DeepSeek-derived execution plan (checkpointed by the bridge):\n"
                    f"{plan}"
                )
                context, context_files = build_context(sandbox.path, clean_task, [])
            request = initial_patch_prompt(clean_task, allowed, context)
            raw = await self.bridge.diff(request, model=model)
            for malformed_attempt in range(3):
                try:
                    increment = extract_unified_diff(raw)
                    validate_patch(increment, allowed)
                    sandbox.apply_increment(increment)
                    break
                except PatchAgentError as exc:
                    if malformed_attempt >= 2:
                        raise
                    sandbox.reset_to_patch("")
                    raw = await self.bridge.diff(
                        malformed_patch_prompt(request, raw, str(exc)),
                        model=model,
                    )

            attempts = 1
            test_result = TestResult("not_run", "No test command supplied", None)
            candidate = sandbox.current_patch()
            while True:
                validate_patch(candidate, allowed)
                test_result = sandbox.run_tests(test_command)
                sandbox.reset_to_patch(candidate)
                if test_result.status != "failed" or attempts > max_repairs:
                    break
                raw_repair = await self.bridge.diff(
                    repair_patch_prompt(
                        clean_task,
                        allowed,
                        context,
                        candidate,
                        test_command or "",
                        test_result.output,
                    ),
                    model=model,
                )
                repair = extract_unified_diff(raw_repair)
                validate_patch(repair, allowed)
                sandbox.apply_increment(repair)
                candidate = sandbox.current_patch()
                attempts += 1

            changed = validate_patch(candidate, allowed)
            patch_dir = STATE_DIR / "patches"
            patch_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            patch_file = patch_dir / f"{patch_id}.diff"
            patch_file.write_text(candidate)
            patch_file.chmod(0o600)

            if test_result.status == "failed":
                status = "tests_failed"
            elif apply_changes:
                sandbox.apply_to_original(candidate)
                status = "applied"
            else:
                status = "ready"

            summary = test_result.output[-1_500:] if test_result.status == "failed" else (
                test_result.output[-500:] if test_result.output else ""
            )
            return {
                "status": status,
                "patch_id": patch_id,
                "changed_files": [str(path) for path in changed],
                "tests": {
                    "status": test_result.status,
                    "command": test_command,
                    "summary": summary,
                },
                "attempts": attempts,
                "model_mode": model,
                "context_files": context_files,
                "patch_file": str(patch_file),
            }
