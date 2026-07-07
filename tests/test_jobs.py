import asyncio
import tempfile
import time
import unittest
from pathlib import Path

from deepseek_bridge.jobs import JobManager, JobStore


class JobStoreTest(unittest.TestCase):
    def test_submit_claim_wait_complete_and_cancel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory) / "jobs.sqlite3")
            submitted = store.submit(
                task="Implement feature",
                project_root=directory,
                paths=["src"],
                test_command="true",
                apply_changes=True,
                max_repairs=2,
                keep_awake=False,
                max_wait_hours=8,
            )
            self.assertEqual(submitted["status"], "queued")

            claimed = store.claim_due()
            self.assertEqual(claimed["id"], submitted["id"])
            self.assertEqual(claimed["status"], "running")

            store.wait(
                submitted["id"],
                next_attempt_at=time.time(),
                error="busy",
                conversation_url="https://chat.deepseek.com/a/chat/s/test",
            )
            self.assertEqual(store.get(submitted["id"])["status"], "waiting")

            claimed = store.claim_due()
            store.complete(claimed["id"], {"status": "applied"})
            completed = store.get(submitted["id"])
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(completed["result"]["status"], "applied")

            second = store.submit(
                task="Second feature",
                project_root=directory,
                paths=None,
                test_command=None,
                apply_changes=False,
                max_repairs=0,
                keep_awake=False,
                max_wait_hours=1,
            )
            self.assertEqual(store.cancel(second["id"])["status"], "cancelled")

    def test_running_job_is_requeued_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "jobs.sqlite3"
            store = JobStore(database)
            submitted = store.submit(
                task="Restart-safe feature",
                project_root=directory,
                paths=None,
                test_command=None,
                apply_changes=False,
                max_repairs=0,
                keep_awake=False,
                max_wait_hours=1,
            )
            store.claim_due()
            restarted = JobStore(database)
            recovered = restarted.get(submitted["id"])
            self.assertEqual(recovered["status"], "waiting")
            self.assertIn("safely requeued", recovered["error"])


class JobManagerTest(unittest.IsolatedAsyncioTestCase):
    async def test_worker_completes_job_without_polling_provider(self) -> None:
        class FakeBridge:
            def current_conversation_url(self):
                return "https://chat.deepseek.com/a/chat/s/job"

            def cooldown_until(self, _):
                return 0

        class FakePatchAgent:
            async def run(self, **_):
                await asyncio.sleep(0.05)
                return {"status": "ready", "changed_files": ["src/app.py"]}

        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory) / "jobs.sqlite3")
            manager = JobManager(FakeBridge(), FakePatchAgent(), asyncio.Lock(), store=store)
            manager.start()
            try:
                submitted = manager.submit(
                    {
                        "task": "Implement feature",
                        "project_root": directory,
                        "paths": ["src"],
                        "apply_changes": False,
                        "keep_awake": False,
                        "max_wait_hours": 1,
                    }
                )
                deadline = asyncio.get_running_loop().time() + 2
                while asyncio.get_running_loop().time() < deadline:
                    status = manager.status(submitted["job_id"])
                    if status["status"] == "completed":
                        break
                    await asyncio.sleep(0.02)
                self.assertEqual(status["status"], "completed")
                self.assertEqual(status["result"]["status"], "ready")
            finally:
                await manager.stop()

    async def test_running_job_can_be_cancelled(self) -> None:
        class FakeBridge:
            def current_conversation_url(self):
                return "https://chat.deepseek.com/a/chat/s/job"

            def cooldown_until(self, _):
                return 0

        class SlowPatchAgent:
            async def run(self, **_):
                await asyncio.Event().wait()

        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory) / "jobs.sqlite3")
            manager = JobManager(FakeBridge(), SlowPatchAgent(), asyncio.Lock(), store=store)
            manager.start()
            try:
                submitted = manager.submit(
                    {
                        "task": "Long feature",
                        "project_root": directory,
                        "keep_awake": False,
                        "max_wait_hours": 1,
                    }
                )
                deadline = asyncio.get_running_loop().time() + 2
                while asyncio.get_running_loop().time() < deadline:
                    status = manager.status(submitted["job_id"])
                    if status["status"] == "running":
                        break
                    await asyncio.sleep(0.02)
                self.assertEqual(status["status"], "running")
                cancelled = manager.cancel(submitted["job_id"])
                self.assertEqual(cancelled["status"], "cancelled")
                await asyncio.sleep(0.05)
                self.assertEqual(manager.status(submitted["job_id"])["status"], "cancelled")
            finally:
                await manager.stop()

    async def test_busy_job_waits_without_retrying_immediately(self) -> None:
        from deepseek_bridge.bridge import DeepSeekBusyError

        class FakeBridge:
            def current_conversation_url(self):
                return "https://chat.deepseek.com/a/chat/s/job"

            def cooldown_until(self, _):
                return time.time() + 600

        class BusyPatchAgent:
            async def run(self, **_):
                raise DeepSeekBusyError("Expert busy")

        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory) / "jobs.sqlite3")
            manager = JobManager(FakeBridge(), BusyPatchAgent(), asyncio.Lock(), store=store)
            manager.start()
            try:
                submitted = manager.submit(
                    {
                        "task": "Wait for Expert",
                        "project_root": directory,
                        "keep_awake": False,
                        "max_wait_hours": 1,
                    }
                )
                deadline = asyncio.get_running_loop().time() + 2
                while asyncio.get_running_loop().time() < deadline:
                    status = manager.status(submitted["job_id"])
                    if status["status"] == "waiting":
                        break
                    await asyncio.sleep(0.02)
                self.assertEqual(status["status"], "waiting")
                self.assertGreaterEqual(status["next_attempt_in_seconds"], 590)
                self.assertEqual(status["attempt_count"], 1)
            finally:
                await manager.stop()

    async def test_expert_cooldown_uses_instant_for_job(self) -> None:
        called_models = []

        class FakeBridge:
            def current_conversation_url(self):
                return None

            def cooldown_until(self, model):
                return time.time() + 600 if model == "expert" else 0

        class FakePatchAgent:
            async def run(self, **kwargs):
                called_models.append(kwargs["model"])
                return {"status": "ready", "model_mode": kwargs["model"]}

        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory) / "jobs.sqlite3")
            manager = JobManager(FakeBridge(), FakePatchAgent(), asyncio.Lock(), store=store)
            manager.start()
            try:
                submitted = manager.submit(
                    {
                        "task": "Fallback task",
                        "project_root": directory,
                        "keep_awake": False,
                        "max_wait_hours": 1,
                    }
                )
                deadline = asyncio.get_running_loop().time() + 2
                while asyncio.get_running_loop().time() < deadline:
                    status = manager.status(submitted["job_id"])
                    if status["status"] == "completed":
                        break
                    await asyncio.sleep(0.02)
                self.assertEqual(status["status"], "completed")
                self.assertEqual(called_models, ["instant"])
                self.assertEqual(status["result"]["model_mode"], "instant")
            finally:
                await manager.stop()


if __name__ == "__main__":
    unittest.main()
