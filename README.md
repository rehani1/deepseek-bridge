# DeepSeek Web Bridge for Claude Code

The bridge integrates with Claude Code and its VS Code extension through a user-scoped MCP
server. A persistent macOS LaunchAgent owns the authenticated browser, so restarting VS Code or
the MCP process does not discard the browser session.

Claude Code's global `~/.claude/CLAUDE.md` can delegate substantial work to these tools by default
and explicitly avoid Plan Mode, subagents, polling, and sleep-based retry loops. Use a native,
low-cost Claude model such as Haiku at low effort as the coordinator. Do not point Claude Code's
Anthropic endpoint at DeepSeek: that prevents the coordinator from reliably authorizing and
invoking MCP tools. Expert remains preferred inside the bridge for substantial analysis and
implementation, with one controlled Instant fallback.

## Install on macOS

```sh
git clone https://github.com/rehani1/deepseek-bridge.git ~/deepseek-bridge
cd ~/deepseek-bridge
chmod +x install-macos.sh authenticate.sh run-*.sh
./install-macos.sh
./authenticate.sh
```

The installer creates a virtual environment, installs Playwright Chromium, installs a lightweight
GUI app under `~/Applications`, registers the persistent LaunchAgent, and registers the
`deepseek-free` user-scoped MCP server when the Claude CLI is available. The GUI wrapper is
required so macOS can display the bridge-owned Chromium window while the daemon continues running
overnight.

## MCP tools

- `deepseek_patch`: queues a durable background implementation and immediately returns a job ID.
  This is the preferred tool for substantial work because generated source does not enter
  Claude's context and Claude does not remain connected while Expert is unavailable.
- `deepseek_job_status`: checks one job without contacting DeepSeek.
- `deepseek_jobs`: lists recent jobs without contacting DeepSeek.
- `deepseek_cancel_job`: cancels a queued, waiting, or running job.
- `deepseek_expert`: answers a technical query with the expert prompt and DeepThink.
- `deepseek_generate`: returns code in chat with the expert prompt and DeepThink.
- `deepseek_search`: performs current web research using Instant mode and Search.
- `deepseek_status`: checks the daemon and authenticated web session.
- `deepseek_show_browser`: surfaces the bridge-owned Chromium window without sending a query.

The patch, expert, and generate tools select the site's actual `Expert` radio control, enable
DeepThink, and disable Search. The search tool selects `Instant` and enables Search. Normal queries
reuse the active conversation for up to 20 turns. A patch/test/repair operation uses one fresh
conversation for the entire operation rather than creating a chat for every internal request.

## Overnight jobs

Submit the work once:

```text
Use deepseek_patch to implement the downloader changes. Restrict changes to src and tests,
run python -m pytest, keep the Mac awake, and wait up to 12 hours.
```

Claude receives a job ID immediately and can end its turn. The LaunchAgent persists the queue in
`~/Library/Application Support/deepseek-bridge/jobs.sqlite3`, processes only one Expert job at a
time, and continues if VS Code closes. Ask Claude to call `deepseek_job_status` with the job ID the
next morning. MCP cannot proactively wake a finished Claude conversation.

Each job has a 1–24 hour deadline. With `keep_awake=true`, the daemon runs `caffeinate -im` only
while a queued, waiting, or running opted-in job exists; it stops the assertion when the queue is
done. Authentication expiry or CAPTCHA marks the job as blocked for manual attention.

## Recommended usage

```text
Use deepseek_patch to add URL validation to src/downloader.py.
Only change src and tests. Run: python -m pytest tests/test_downloader.py
```

The patch tool requires a Git repository. It:

1. Mirrors tracked and non-ignored working-tree changes into a temporary worktree.
2. Selects relevant repository files within a bounded context budget.
3. Requests and validates a unified diff from DeepSeek with DeepThink enabled.
4. Rejects sensitive paths, binaries, symlinks, submodules, path traversal, and oversized diffs.
5. Runs an optional command with network access denied and credential environment variables
   removed.
6. Applies successful patches only if the original working tree has not changed concurrently.

Every generated patch is retained with user-only permissions under
`~/Library/Application Support/deepseek-bridge/patches` for review and recovery.

## First-time or renewed authentication

Run:

```sh
~/deepseek-bridge/authenticate.sh
```

The script temporarily stops the daemon, opens the dedicated browser profile, waits for the chat
input, and restarts the daemon. The profile lives at
`~/Library/Application Support/deepseek-bridge/profile` with user-only permissions.

## Operations

```sh
# Check the LaunchAgent
launchctl print gui/$(id -u)/com.deepseek.bridge

# Restart it
launchctl kickstart -k gui/$(id -u)/com.deepseek.bridge

# Follow the rotating application log
tail -f ~/Library/Application\ Support/deepseek-bridge/bridge.log

# Run tests
cd ~/deepseek-bridge
PYTHONPATH=. .venv/bin/python -m unittest discover -s tests -v
```

In VS Code, use `/mcp` to reconnect `deepseek-free` after a tool-schema update.

Browser UI automation can still break when DeepSeek changes its site. The bridge does not bypass
login, CAPTCHAs, rate limits, or other access controls. Generated code remains untrusted and
should be reviewed before execution.

When Expert reports that it is busy, the bridge does not immediately retry. It records a
persistent exponential Expert cooldown (10, 20, 40, then 60 minutes) and schedules one controlled
Instant fallback for the job. A fallback requires a new chat because the model control disappears
after a conversation begins; all subsequent patch/test/repair turns remain in that fallback chat.
Expert requests are serialized, spaced by at least 60 seconds, and capped at eight per hour.
Instant requests are spaced by 15 seconds and capped at 20 requests per hour.
