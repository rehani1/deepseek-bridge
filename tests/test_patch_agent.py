import asyncio
import subprocess
import tempfile
import unittest
from pathlib import Path

from deepseek_bridge.patch_agent import (
    PatchAgent,
    PatchAgentError,
    RepoSandbox,
    changed_paths,
    extract_unified_diff,
    validate_patch,
)


PATCH = """diff --git a/src/app.py b/src/app.py
index 2c99a52..1601d4b 100644
--- a/src/app.py
+++ b/src/app.py
@@ -1 +1 @@
-print("old")
+print("new")
"""


class PatchParsingTest(unittest.TestCase):
    def test_extracts_fenced_diff(self) -> None:
        self.assertEqual(extract_unified_diff(f"```diff\n{PATCH}```"), PATCH)

    def test_changed_paths(self) -> None:
        self.assertEqual(changed_paths(PATCH), [Path("src/app.py")])

    def test_allowlist_is_enforced(self) -> None:
        with self.assertRaises(PatchAgentError):
            validate_patch(PATCH, [Path("tests")])

    def test_sensitive_paths_are_rejected(self) -> None:
        secret_patch = PATCH.replace("src/app.py", ".env")
        with self.assertRaises(PatchAgentError):
            validate_patch(secret_patch, [])


class RepoSandboxTest(unittest.TestCase):
    def test_dirty_worktree_is_mirrored_without_changing_original(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            (root / "src").mkdir()
            app = root / "src" / "app.py"
            app.write_text('print("old")\n')
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@localhost",
                    "commit",
                    "-q",
                    "-m",
                    "initial",
                ],
                cwd=root,
                check=True,
            )
            app.write_text('print("working")\n')

            with RepoSandbox(root) as sandbox:
                self.assertEqual(
                    (sandbox.path / "src" / "app.py").read_text(),
                    'print("working")\n',
                )
                result = sandbox.run_tests("test -f src/app.py")
                self.assertEqual(result.status, "passed")

            self.assertEqual(app.read_text(), 'print("working")\n')

    def test_patch_agent_applies_validated_patch_with_fake_provider(self) -> None:
        class FakeBridge:
            async def begin_task(self) -> None:
                return None

            async def diff(self, _: str, *, model: str = "expert") -> str:
                return PATCH

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            (root / "src").mkdir()
            app = root / "src" / "app.py"
            app.write_text('print("old")\n')
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@localhost",
                    "commit",
                    "-q",
                    "-m",
                    "initial",
                ],
                cwd=root,
                check=True,
            )

            result = asyncio.run(
                PatchAgent(FakeBridge()).run(
                    task="Update the output",
                    project_root=str(root),
                    paths=["src"],
                    test_command="grep -q new src/app.py",
                    apply_changes=True,
                    max_repairs=0,
                )
            )
            self.assertEqual(result["status"], "applied")
            self.assertEqual(result["tests"]["status"], "passed")
            self.assertEqual(app.read_text(), 'print("new")\n')
            Path(result["patch_file"]).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
