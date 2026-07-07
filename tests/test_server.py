import hashlib
import tempfile
import unittest
from pathlib import Path

from deepseek_bridge.server import snapshot_project_plan


class ProjectPlanSnapshotTest(unittest.TestCase):
    def test_plan_is_embedded_with_source_and_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path = root / "docs" / "plans" / "phase-3.md"
            plan_path.parent.mkdir(parents=True)
            plan = "# Phase 3\n\n- Add integration tests\n- Harden account locking"
            plan_path.write_text(plan)

            task = snapshot_project_plan(
                str(root),
                "Implement Phase 3.",
                "docs/plans/phase-3.md",
            )

            self.assertIn("Implement Phase 3.", task)
            self.assertIn("source: docs/plans/phase-3.md", task)
            self.assertIn(hashlib.sha256(plan.encode()).hexdigest(), task)
            self.assertIn(plan, task)

            plan_path.write_text("changed after submission")
            self.assertIn(plan, task)
            self.assertNotIn("changed after submission", task)

    def test_task_without_plan_is_unchanged(self) -> None:
        self.assertEqual(snapshot_project_plan("/tmp", "  Implement feature.  ", None),
                         "Implement feature.")

    def test_plan_must_stay_inside_repository(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                snapshot_project_plan(directory, "Implement.", "../outside.md")

    def test_plan_must_be_markdown_or_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan = Path(directory) / "plan.json"
            plan.write_text("{}")
            with self.assertRaises(ValueError):
                snapshot_project_plan(directory, "Implement.", "plan.json")


if __name__ == "__main__":
    unittest.main()
