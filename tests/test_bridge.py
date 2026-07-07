import unittest
import time
import asyncio

from deepseek_bridge.bridge import (
    is_busy_response,
    normalize_task,
    sanitize_rendered_text,
    strip_single_code_fence,
)


class BridgeHelpersTest(unittest.TestCase):
    def test_normalize_task(self) -> None:
        self.assertEqual(normalize_task("  write Python  "), "write Python")

    def test_empty_task_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            normalize_task(" \n ")

    def test_complete_code_fence_is_removed(self) -> None:
        self.assertEqual(strip_single_code_fence("```python\nprint('ok')\n```"), "print('ok')")

    def test_multiple_fences_are_preserved(self) -> None:
        value = "a.py\n```python\na = 1\n```\nb.py\n```python\nb = 2\n```"
        self.assertEqual(strip_single_code_fence(value), value)

    def test_code_block_controls_are_removed(self) -> None:
        value = "python\nCopy\nDownload\nimport os\nprint(os.getcwd())"
        self.assertEqual(sanitize_rendered_text(value), "import os\nprint(os.getcwd())")

    def test_normal_text_is_preserved(self) -> None:
        value = "Use Python for this answer.\n\nThe implementation follows."
        self.assertEqual(sanitize_rendered_text(value), value)

    def test_busy_response_is_detected(self) -> None:
        self.assertTrue(is_busy_response("Server is busy. Try again later, or use Instant Mode."))
        self.assertFalse(is_busy_response("Implementation completed successfully."))

    def test_hourly_expert_budget_blocks_without_request(self) -> None:
        from deepseek_bridge.bridge import DeepSeekBusyError, DeepSeekWebBridge

        bridge = DeepSeekWebBridge()
        bridge._rate_state = {"expert": {"submissions": [time.time()] * 8}}
        bridge._save_rate_state = lambda: None
        with self.assertRaises(DeepSeekBusyError):
            bridge._check_cooldown("expert")

    def test_expert_uses_one_instant_fallback(self) -> None:
        from deepseek_bridge.bridge import DeepSeekBusyError, DeepSeekWebBridge

        class FakeBridge(DeepSeekWebBridge):
            def __init__(self):
                super().__init__()
                self.calls = []

            async def _query(self, prompt, prefer_code, *, model, search):
                self.calls.append(model)
                if model == "expert":
                    raise DeepSeekBusyError("busy")
                return "fallback"

        bridge = FakeBridge()
        self.assertEqual(asyncio.run(bridge.expert("query")), "fallback")
        self.assertEqual(bridge.calls, ["expert", "instant"])


if __name__ == "__main__":
    unittest.main()
