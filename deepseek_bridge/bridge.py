from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time
from pathlib import Path

from playwright.async_api import (
    BrowserContext,
    Locator,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)


CHAT_URL = "https://chat.deepseek.com/"
DEFAULT_PROFILE_DIR = (
    Path.home() / "Library" / "Application Support" / "deepseek-bridge" / "profile"
)
DEFAULT_RATE_STATE_PATH = (
    Path.home() / "Library" / "Application Support" / "deepseek-bridge" / "rate-state.json"
)
DEFAULT_LAST_RESPONSE_PATH = (
    Path.home() / "Library" / "Application Support" / "deepseek-bridge" / "last-response.txt"
)
MAX_TASK_CHARS = 50_000
MAX_RESPONSE_CHARS = 120_000
MAX_BRIDGE_PROMPT_CHARS = 180_000

COMPOSER_SELECTORS = (
    'textarea[placeholder*="message" i]',
    'textarea[placeholder*="DeepSeek" i]',
    "textarea",
    'div[contenteditable="true"]',
)
RESPONSE_SELECTORS = (
    ".ds-markdown",
    'div[class*="assistant"] .markdown',
    'div[class*="assistant"]',
    "div.markdown",
)
STOP_SELECTORS = (
    'button:has-text("Stop")',
    '[aria-label*="Stop" i]',
    '[title*="Stop" i]',
)
DEEPTHINK_SELECTORS = (
    'button:has-text("DeepThink")',
    '[role="button"]:has-text("DeepThink")',
)
UI_CONTROL_LABELS = {"copy", "download"}
MAX_CONVERSATION_TURNS = 20
SUBMISSION_INTERVAL_SECONDS = {"expert": 60, "instant": 15}
HOURLY_REQUEST_LIMITS = {"expert": 8, "instant": 20}
BUSY_RESPONSE_MARKERS = (
    "server is busy",
    "try again later",
    "use instant mode",
    "high traffic",
)


class DeepSeekBridgeError(RuntimeError):
    """Base error returned as an MCP tool failure."""


class AuthenticationRequired(DeepSeekBridgeError):
    """Raised when the persisted browser profile is not authenticated."""


class DeepSeekBusyError(DeepSeekBridgeError):
    """Raised without retrying when a DeepSeek mode reports capacity pressure."""


def profile_dir() -> Path:
    configured = os.environ.get("DEEPSEEK_PROFILE_DIR")
    return Path(configured).expanduser() if configured else DEFAULT_PROFILE_DIR


def normalize_task(task: str) -> str:
    value = task.strip()
    if not value:
        raise ValueError("task must not be empty")
    if len(value) > MAX_TASK_CHARS:
        raise ValueError(f"task exceeds the {MAX_TASK_CHARS:,}-character limit")
    return value


def strip_single_code_fence(value: str) -> str:
    """Remove one fence only when it wraps the complete response."""
    text = value.strip()
    match = re.fullmatch(r"```[^\n]*\n(.*?)\n?```", text, flags=re.DOTALL)
    return match.group(1).strip() if match else text


def sanitize_rendered_text(value: str) -> str:
    """Remove code-block controls included by DeepSeek's rendered inner text."""
    lines = value.strip().splitlines()
    cleaned: list[str] = []
    for index, line in enumerate(lines):
        normalized = line.strip().lower()
        if normalized in UI_CONTROL_LABELS:
            continue
        next_labels = {item.strip().lower() for item in lines[index + 1 : index + 3]}
        is_language_label = (
            len(normalized) <= 32
            and bool(re.fullmatch(r"[a-z0-9_+#. -]+", normalized))
            and bool(next_labels & UI_CONTROL_LABELS)
        )
        if is_language_label:
            continue
        cleaned.append(line)
    return "\n".join(cleaned).strip()


def choose_response_text(rendered_text: str, code_blocks: list[str]) -> str:
    """Prefer the complete rendered answer over partial code-node matches.

    DeepSeek uses ``<code>`` for inline identifiers as well as fenced blocks. Treating any code
    node as the response can collapse a large multi-file answer to a few inline symbols.
    """
    complete = sanitize_rendered_text(rendered_text)
    if complete:
        return complete
    return "\n\n".join(block.strip() for block in code_blocks if block.strip()).strip()


def is_busy_response(value: str) -> bool:
    lowered = value.lower()
    return any(marker in lowered for marker in BUSY_RESPONSE_MARKERS)


class DeepSeekWebBridge:
    def __init__(self) -> None:
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._lock = asyncio.Lock()
        self._conversation_turns = 0
        self._selected_model: str | None = None
        self._deepthink_enabled = False
        self._toggle_states: dict[str, bool] = {}
        self._last_submission: dict[str, float] = {}
        self._rate_state = self._load_rate_state()
        self.request_count = 0

    @staticmethod
    def _load_rate_state() -> dict[str, dict[str, object]]:
        try:
            value = json.loads(DEFAULT_RATE_STATE_PATH.read_text())
            if not isinstance(value, dict):
                return {}
            return {key: item for key, item in value.items() if isinstance(item, dict)}
        except (OSError, ValueError, TypeError):
            return {}

    def _save_rate_state(self) -> None:
        DEFAULT_RATE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = DEFAULT_RATE_STATE_PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._rate_state, separators=(",", ":")))
        temporary.chmod(0o600)
        temporary.replace(DEFAULT_RATE_STATE_PATH)

    @staticmethod
    def _load_last_response() -> str:
        try:
            return DEFAULT_LAST_RESPONSE_PATH.read_text().strip()
        except OSError:
            return ""

    @staticmethod
    def _save_last_response(value: str) -> None:
        DEFAULT_LAST_RESPONSE_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = DEFAULT_LAST_RESPONSE_PATH.with_suffix(".tmp")
        temporary.write_text(value)
        temporary.chmod(0o600)
        temporary.replace(DEFAULT_LAST_RESPONSE_PATH)

    def _check_cooldown(self, model: str) -> None:
        state = self._rate_state.setdefault(model, {})
        until = float(state.get("until", 0))
        remaining = max(0, int(until - time.time()))
        if remaining:
            minutes = max(1, (remaining + 59) // 60)
            raise DeepSeekBusyError(
                f"DeepSeek {model.title()} mode is in local cooldown for about {minutes} "
                "minute(s) after a busy response. No request was sent."
            )

        now = time.time()
        submissions = [
            float(value)
            for value in state.get("submissions", [])
            if isinstance(value, (int, float)) and float(value) > now - 3_600
        ]
        state["submissions"] = submissions
        limit = HOURLY_REQUEST_LIMITS[model]
        if len(submissions) >= limit:
            until = submissions[0] + 3_600
            state["until"] = until
            self._save_rate_state()
            minutes = max(1, int((until - now + 59) // 60))
            raise DeepSeekBusyError(
                f"Local {model.title()} request budget reached ({limit}/hour). "
                f"No request was sent; retry in about {minutes} minute(s)."
            )

    def _mark_submission(self, model: str) -> None:
        state = self._rate_state.setdefault(model, {})
        now = time.time()
        submissions = [
            float(value)
            for value in state.get("submissions", [])
            if isinstance(value, (int, float)) and float(value) > now - 3_600
        ]
        submissions.append(now)
        state["submissions"] = submissions
        self._save_rate_state()

    def _mark_busy(self, model: str) -> None:
        previous = self._rate_state.setdefault(model, {})
        failures = min(int(previous.get("failures", 0)) + 1, 4)
        duration = min(3_600, 600 * (2 ** (failures - 1)))
        previous["failures"] = failures
        previous["until"] = time.time() + duration
        self._save_rate_state()

    def _clear_busy(self, model: str) -> None:
        state = self._rate_state.get(model)
        if state is None:
            return
        state.pop("failures", None)
        state.pop("until", None)
        if not state.get("submissions"):
            self._rate_state.pop(model, None)
        self._save_rate_state()

    def cooldown_until(self, model: str) -> float:
        state = self._rate_state.get(model, {})
        until = float(state.get("until", 0))
        now = time.time()
        submissions = sorted(
            float(value)
            for value in state.get("submissions", [])
            if isinstance(value, (int, float)) and float(value) > now - 3_600
        )
        limit = HOURLY_REQUEST_LIMITS[model]
        if len(submissions) >= limit:
            until = max(until, submissions[-limit] + 3_600)
        return until

    def current_conversation_url(self) -> str | None:
        if self._page is None or self._page.is_closed():
            return None
        url = self._page.url
        return url if url.startswith(CHAT_URL) else None

    async def start(self) -> Page:
        if self._page is not None and not self._page.is_closed():
            return self._page

        data_dir = profile_dir()
        data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        data_dir.chmod(0o700)

        self._playwright = await async_playwright().start()
        try:
            self._context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(data_dir),
                headless=False,
                viewport={"width": 1280, "height": 900},
                args=["--window-position=50,50", "--window-size=1280,900"],
            )
        except Exception:
            await self._playwright.stop()
            self._playwright = None
            raise

        self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        await self._activate_browser(self._page)
        return self._page

    async def _activate_browser(self, page: Page) -> None:
        if self._context is not None:
            try:
                session = await self._context.new_cdp_session(page)
                window = await session.send("Browser.getWindowForTarget")
                await session.send(
                    "Browser.setWindowBounds",
                    {
                        "windowId": window["windowId"],
                        "bounds": {
                            "left": 50,
                            "top": 50,
                            "width": 1280,
                            "height": 900,
                            "windowState": "normal",
                        },
                    },
                )
                await session.detach()
            except Exception:
                pass
        executable = Path(self._playwright.chromium.executable_path) if self._playwright else None
        if executable is not None and len(executable.parents) >= 3:
            app_path = executable.parents[2]
            if app_path.suffix == ".app":
                subprocess.run(
                    ["/usr/bin/open", "-a", str(app_path)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                subprocess.run(
                    [
                        "/usr/bin/osascript",
                        "-e",
                        'tell application "Google Chrome for Testing"',
                        "-e",
                        "if (count of windows) > 0 then",
                        "-e",
                        "set frontWindow to first window",
                        "-e",
                        "set bounds of frontWindow to {50, 50, 1330, 950}",
                        "-e",
                        "set index of frontWindow to 1",
                        "-e",
                        "end if",
                        "-e",
                        "activate",
                        "-e",
                        "end tell",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                await asyncio.sleep(0.5)
        try:
            await page.bring_to_front()
        except Exception:
            pass

    async def show_browser(self) -> str:
        async with self._lock:
            page = await self.start()
            await self._ensure_chat(page)
            await self._activate_browser(page)
            return f"DeepSeek Chromium surfaced at {page.url}"

    async def last_response(self) -> str:
        """Return the latest complete rendered answer without submitting another request."""
        async with self._lock:
            page = await self.start()
            await self._ensure_chat(page)
            deadline = asyncio.get_running_loop().time() + 180
            while await self._generation_is_active(page):
                if asyncio.get_running_loop().time() >= deadline:
                    raise DeepSeekBridgeError(
                        "The latest DeepSeek response is still generating after 180 seconds"
                    )
                await asyncio.sleep(1)
            response, _ = await self._settle_responses(page, prefer_code=True)
            if response:
                complete = strip_single_code_fence(response)[:MAX_RESPONSE_CHARS]
                self._save_last_response(complete)
                return complete
            cached = self._load_last_response()
            if cached:
                return cached[:MAX_RESPONSE_CHARS]
            raise DeepSeekBridgeError(
                "No completed DeepSeek response is visible or present in the local recovery cache"
            )

    async def close(self) -> None:
        if self._context is not None:
            await self._context.close()
        if self._playwright is not None:
            await self._playwright.stop()
        self._page = None
        self._context = None
        self._playwright = None

    async def status(self) -> str:
        async with self._lock:
            page = await self.start()
            await self._ensure_chat(page)
            composer = await self._find_composer(page, timeout_ms=5_000)
            state = "authenticated" if composer is not None else "authentication required"
            mode = await self._current_model(page)
            return (
                f"DeepSeek bridge: {state}; model: {mode}; "
                f"conversation turns: {self._conversation_turns}; "
                f"requests served: {self.request_count}; URL: {page.url}"
            )

    async def begin_task(self) -> None:
        """Start one conversation for a multi-request patch/test/repair task."""
        async with self._lock:
            page = await self.start()
            await self._open_fresh_chat(page)
            await self._activate_browser(page)
            self._conversation_turns = 0

    async def resume_task(self, url: str | None) -> None:
        if not url or not url.startswith(CHAT_URL):
            await self.begin_task()
            return
        async with self._lock:
            page = await self.start()
            if page.url != url:
                await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                await page.wait_for_timeout(1_500)
            await self._activate_browser(page)
            composer = await self._find_composer(page, timeout_ms=10_000)
            if composer is None:
                await self._open_fresh_chat(page)
                self._conversation_turns = 0
                return
            # Mode controls disappear after the first message; these values were verified
            # before the job recorded this Expert conversation URL.
            self._selected_model = "expert"
            self._deepthink_enabled = True
            self._toggle_states["Search"] = False
            self._conversation_turns = max(1, await self._response_count(page))

    async def generate(self, task: str) -> str:
        clean_task = normalize_task(task)
        prompt = (
            "You are DeepSeek Expert, a senior software engineer operating in DeepThink mode. "
            "Reason carefully and return only the complete working implementation requested "
            "below. Do not add commentary or a preamble. For a single file, return raw code or "
            "one code fence. For multiple files, label each relative path and include its "
            "complete content.\n\nTask:\n"
            f"{clean_task}"
        )
        try:
            return await self._query(prompt, prefer_code=True, model="expert", search=False)
        except DeepSeekBusyError:
            return await self._query(prompt, prefer_code=True, model="instant", search=False)

    async def expert(self, query: str) -> str:
        clean_query = normalize_task(query)
        prompt = (
            "You are DeepSeek Expert operating in DeepThink mode. Analyze the request carefully "
            "and provide a precise, technically rigorous final answer. For coding requests, "
            "produce complete production-ready code and clearly identify multiple files. Do not "
            "reveal private chain-of-thought; provide only the final answer and concise rationale "
            "when useful.\n\nQuery:\n"
            f"{clean_query}"
        )
        try:
            return await self._query(prompt, prefer_code=False, model="expert", search=False)
        except DeepSeekBusyError:
            return await self._query(prompt, prefer_code=False, model="instant", search=False)

    async def search(self, query: str) -> str:
        clean_query = normalize_task(query)
        prompt = (
            "Research the query using web search. Return a concise, factual answer with source "
            "links and distinguish confirmed facts from inference.\n\nQuery:\n"
            f"{clean_query}"
        )
        return await self._query(prompt, prefer_code=False, model="instant", search=True)

    async def diff(self, request: str, *, model: str = "expert") -> str:
        clean_request = request.strip()
        if not clean_request:
            raise ValueError("patch request must not be empty")
        if len(clean_request) > MAX_BRIDGE_PROMPT_CHARS:
            raise ValueError(
                f"patch request exceeds the {MAX_BRIDGE_PROMPT_CHARS:,}-character limit"
            )
        role = "DeepSeek Expert" if model == "expert" else "a senior software engineer"
        prompt = (
            f"You are {role} operating in DeepThink mode. "
            "Produce a precise, directly applicable Git patch. Follow every output and safety "
            "constraint in the request. Return only the requested unified diff; do not add a "
            "preamble, explanation, or postscript.\n\n"
            f"{clean_request}"
        )
        return await self._query(prompt, prefer_code=True, model=model, search=False)

    async def _query(
        self,
        prompt: str,
        prefer_code: bool,
        *,
        model: str,
        search: bool,
    ) -> str:
        async with self._lock:
            self._check_cooldown(model)
            page = await self.start()
            await self._ensure_chat(page)
            if self._selected_model is not None and self._selected_model != model:
                await self._open_fresh_chat(page)
                self._conversation_turns = 0
            if self._conversation_turns >= MAX_CONVERSATION_TURNS:
                await self._open_fresh_chat(page)
                self._conversation_turns = 0
            composer = await self._find_composer(page, timeout_ms=15_000)
            if composer is None:
                raise AuthenticationRequired(
                    "DeepSeek login is required. Run ~/deepseek-bridge/authenticate.sh "
                    "in a terminal, complete login, then retry."
                )
            await self._ensure_model(page, model)
            await self._ensure_deepthink(page)
            await self._ensure_toggle(page, "Search", enabled=search)
            before, before_count = await self._settle_responses(
                page,
                prefer_code=prefer_code,
            )
            spacing = SUBMISSION_INTERVAL_SECONDS[model] - (
                time.monotonic() - self._last_submission.get(model, 0.0)
            )
            if spacing > 0:
                await asyncio.sleep(spacing)
            await composer.fill(prompt)
            await composer.press("Enter")
            self._last_submission[model] = time.monotonic()
            self._mark_submission(model)
            response = await self._wait_for_response(
                page,
                previous=before,
                previous_count=before_count,
                prefer_code=prefer_code,
            )
            if is_busy_response(response):
                self._mark_busy(model)
                raise DeepSeekBusyError(
                    f"DeepSeek {model.title()} mode reported that the server is busy. "
                    "A local cooldown was started; the bridge will not retry or downgrade modes."
                )
            self._clear_busy(model)
            self.request_count += 1
            self._conversation_turns += 1
            complete = strip_single_code_fence(response)[:MAX_RESPONSE_CHARS]
            self._save_last_response(complete)
            return complete

    async def _ensure_chat(self, page: Page) -> None:
        if page.url.startswith(CHAT_URL):
            composer = await self._find_composer(page, timeout_ms=2_000)
            if composer is not None:
                return
        await self._open_fresh_chat(page)
        self._conversation_turns = 0

    async def _open_fresh_chat(self, page: Page) -> None:
        try:
            await page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_timeout(1_500)
            self._selected_model = None
            self._deepthink_enabled = False
            self._toggle_states.clear()
        except PlaywrightTimeoutError as exc:
            raise DeepSeekBridgeError("DeepSeek did not load within 60 seconds") from exc

    async def _current_model(self, page: Page) -> str:
        for model, model_type in (("expert", "expert"), ("instant", "default")):
            option = page.locator(
                f'[data-model-type="{model_type}"][role="radio"][aria-checked="true"]'
            )
            if await option.count():
                self._selected_model = model
                return model
        return self._selected_model or "unknown"

    async def _ensure_model(self, page: Page, model: str) -> None:
        model_types = {"expert": "expert", "instant": "default"}
        if model not in model_types:
            raise ValueError(f"Unsupported DeepSeek model mode: {model}")
        option = page.locator(
            f'[data-model-type="{model_types[model]}"][role="radio"]'
        ).first
        if not await option.count():
            if self._selected_model == model:
                return
            raise DeepSeekBridgeError(f"The DeepSeek {model.title()} mode control was not found")
        if (await option.get_attribute("aria-checked")) == "true":
            self._selected_model = model
            return
        await option.click()
        deadline = asyncio.get_running_loop().time() + 5
        while asyncio.get_running_loop().time() < deadline:
            if (await option.get_attribute("aria-checked")) == "true":
                self._selected_model = model
                return
            await asyncio.sleep(0.1)
        raise DeepSeekBridgeError(f"DeepSeek did not switch to {model.title()} mode")

    @staticmethod
    async def _find_composer(page: Page, timeout_ms: int) -> Locator | None:
        deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
        while asyncio.get_running_loop().time() < deadline:
            for selector in COMPOSER_SELECTORS:
                locator = page.locator(selector).last
                try:
                    if await locator.count() and await locator.is_visible():
                        return locator
                except Exception:
                    continue
            await asyncio.sleep(0.25)
        return None

    async def _ensure_deepthink(self, page: Page) -> None:
        candidates: list[Locator] = [page.locator(selector).last for selector in DEEPTHINK_SELECTORS]
        candidates.append(page.get_by_text(re.compile(r"^DeepThink", re.IGNORECASE)).last)

        toggle: Locator | None = None
        for candidate in candidates:
            try:
                if await candidate.count() and await candidate.is_visible():
                    toggle = candidate
                    break
            except Exception:
                continue
        if toggle is None:
            if self._deepthink_enabled:
                return
            raise DeepSeekBridgeError(
                "The DeepThink control was not found. DeepSeek may have changed its interface."
            )

        state = await toggle.evaluate(
            """element => {
                const nodes = [element, element.parentElement, element.parentElement?.parentElement];
                for (const node of nodes) {
                    if (!node) continue;
                    for (const name of ['aria-pressed', 'aria-checked', 'data-active',
                                        'data-selected', 'data-state']) {
                        const value = node.getAttribute(name);
                        if (value !== null) return value.toLowerCase();
                    }
                    const classes = typeof node.className === 'string' ? node.className : '';
                    if (/(^|\\s)(active|selected|checked)(\\s|$)/i.test(classes)) return 'true';
                }
                return 'unknown';
            }"""
        )
        if state in {"true", "on", "active", "selected", "checked"}:
            self._deepthink_enabled = True
            return
        await toggle.click()
        await page.wait_for_timeout(300)
        self._deepthink_enabled = True

    async def _ensure_toggle(self, page: Page, label: str, *, enabled: bool) -> None:
        text = page.get_by_text(re.compile(rf"^{re.escape(label)}$", re.IGNORECASE)).last
        if not await text.count():
            if self._toggle_states.get(label) == enabled:
                return
            if not enabled:
                self._toggle_states[label] = False
                return
            raise DeepSeekBridgeError(f"The DeepSeek {label} control was not found")
        control = text.locator("xpath=parent::*")
        if not await control.count():
            raise DeepSeekBridgeError(f"The DeepSeek {label} control is invalid")
        state = (await control.get_attribute("aria-pressed")) == "true"
        if state == enabled:
            self._toggle_states[label] = enabled
            return
        await control.click()
        deadline = asyncio.get_running_loop().time() + 5
        while asyncio.get_running_loop().time() < deadline:
            state = (await control.get_attribute("aria-pressed")) == "true"
            if state == enabled:
                self._toggle_states[label] = enabled
                return
            await asyncio.sleep(0.1)
        raise DeepSeekBridgeError(f"DeepSeek did not set {label} to {enabled}")

    @staticmethod
    async def _response_count(page: Page) -> int:
        for selector in RESPONSE_SELECTORS:
            try:
                count = await page.locator(selector).count()
                if count:
                    return count
            except Exception:
                continue
        return 0

    async def _settle_responses(self, page: Page, prefer_code: bool) -> tuple[str, int]:
        last_text = ""
        last_count = -1
        stable = 0
        deadline = asyncio.get_running_loop().time() + 5
        while asyncio.get_running_loop().time() < deadline:
            text = await self._latest_response_text(page, prefer_code=prefer_code)
            count = await self._response_count(page)
            if text == last_text and count == last_count:
                stable += 1
                if stable >= 3:
                    return text, count
            else:
                last_text, last_count, stable = text, count, 0
            await asyncio.sleep(0.25)
        return last_text, max(last_count, 0)

    @staticmethod
    async def _latest_response_text(page: Page, prefer_code: bool = False) -> str:
        for selector in RESPONSE_SELECTORS:
            locator = page.locator(selector)
            try:
                count = await locator.count()
                if count:
                    response = locator.nth(count - 1)
                    rendered = await response.inner_text()
                    blocks: list[str] = []
                    if prefer_code:
                        # Bare ``code`` also matches inline identifiers. Block selectors are only
                        # a fallback for a response whose rendered text is unexpectedly empty.
                        code = response.locator("pre code, .md-code-block code")
                        code_count = await code.count()
                        if code_count:
                            blocks = [
                                (await code.nth(index).text_content() or "").strip()
                                for index in range(code_count)
                            ]
                    text = choose_response_text(rendered, blocks)
                    if text:
                        return text
            except Exception:
                continue
        return ""

    @staticmethod
    async def _generation_is_active(page: Page) -> bool:
        for selector in STOP_SELECTORS:
            locator = page.locator(selector).last
            try:
                if await locator.count() and await locator.is_visible():
                    return True
            except Exception:
                continue
        return False

    async def _wait_for_response(
        self,
        page: Page,
        previous: str,
        previous_count: int,
        prefer_code: bool,
    ) -> str:
        deadline = asyncio.get_running_loop().time() + 180
        last = ""
        stable_polls = 0
        saw_new_response = False
        saw_generation = False

        while asyncio.get_running_loop().time() < deadline:
            current = await self._latest_response_text(page, prefer_code=prefer_code)
            count = await self._response_count(page)
            active = await self._generation_is_active(page)
            saw_generation = saw_generation or active
            is_new = count > previous_count or (saw_generation and current != previous)
            if current and current != previous and is_new:
                saw_new_response = True
                if current == last:
                    stable_polls += 1
                else:
                    last = current
                    stable_polls = 0

                if stable_polls >= 2 and not active:
                    return current
            await asyncio.sleep(1)

        if saw_new_response and last:
            return last
        raise DeepSeekBridgeError(
            "No DeepSeek response was detected within 180 seconds. The site layout, login "
            "state, or rate limit may require attention in the opened browser."
        )
