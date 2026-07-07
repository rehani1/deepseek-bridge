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
- `deepseek_last_response`: recovers the latest completed browser answer without sending another
  query. Completed answers are cached with user-only permissions so recovery survives browser,
  daemon, and VS Code restarts. Large results will enter Claude's context when retrieved.

The patch, expert, and generate tools select the site's actual `Expert` radio control, enable
DeepThink, and disable Search. The search tool selects `Instant` and enables Search. Normal queries
reuse the active conversation for up to 20 turns. A patch/test/repair operation uses one fresh
conversation for the entire operation rather than creating a chat for every internal request.

For a named project phase, set `autonomous=true`. DeepSeek—not Claude—audits the repository
requirements, manifests, file inventory, and relevant implementation, derives the execution plan,
and checkpoints that plan and conversation URL in SQLite before generating a patch. Claude and VS
Code can close without losing the objective. Repeating an identical submission while it is pending
returns the existing job ID instead of creating a duplicate. `plan_path` remains available as an
optional alternative when a project already has a user-authored plan, but it is not required.

## Cost efficiency

DeepSeek Web Bridge is not just a DeepSeek wrapper. It is a cost-control layer for Claude Code.

The bridge saves money in two separate ways:

1. **Model-price savings** — heavy work can run through DeepSeek instead of a more expensive Claude
   model.
2. **Context savings** — large generated patches, repair loops, test logs, and intermediate
   reasoning stay outside Claude Code's paid context.

The second effect is usually the bigger win on large coding jobs.

Pricing checked July 7, 2026: [DeepSeek's official pricing page](https://api-docs.deepseek.com/quick_start/pricing/)
lists V4 Flash at `$0.14 / 1M` input tokens and `$0.28 / 1M` output tokens, and V4 Pro
at `$0.435 / 1M` input and `$0.87 / 1M` output. [Anthropic's official pricing page](https://platform.claude.com/docs/en/about-claude/pricing)
lists Claude Haiku 4.5 at `$1 / 1M` input and `$5 / 1M` output, and Claude Sonnet 4.5 / 4.6
at `$3 / 1M` input and `$15 / 1M` output.

### Raw model-cost comparison

For a representative coding-agent turn:

```text
50k input tokens + 15k output tokens
```

| Route                   | Input cost | Output cost | Total cost |
| ----------------------- | ---------: | ----------: | ---------: |
| Claude Sonnet 4.5 / 4.6 |  `$0.1500` |   `$0.2250` |  `$0.3750` |
| Claude Haiku 4.5        |  `$0.0500` |   `$0.0750` |  `$0.1250` |
| DeepSeek V4 Pro API     |  `$0.0218` |   `$0.0131` |  `$0.0348` |
| DeepSeek V4 Flash API   |  `$0.0070` |   `$0.0042` |  `$0.0112` |

So on API token price alone:

| Comparison                         | Approximate savings |
| ---------------------------------- | ------------------: |
| DeepSeek V4 Flash vs Claude Sonnet |    `~33.5x cheaper` |
| DeepSeek V4 Pro vs Claude Sonnet   |    `~10.8x cheaper` |
| DeepSeek V4 Flash vs Claude Haiku  |    `~11.2x cheaper` |
| DeepSeek V4 Pro vs Claude Haiku    |     `~3.6x cheaper` |

### What the bridge adds on top

The bridge does **not** make DeepSeek API tokens cheaper. It saves more money by moving expensive
work out of the paid Claude Code loop.

The key approximation is:

```text
Additional bridge efficiency ≈ 1 / remaining paid-token fraction
```

| Heavy work moved to bridge | Remaining paid token burden | Extra efficiency vs DeepSeek API-only |
| -------------------------: | --------------------------: | ------------------------------------: |
|                      `50%` |                       `50%` |                                  `2x` |
|                      `80%` |                       `20%` |                                  `5x` |
|                      `90%` |                       `10%` |                                 `10x` |
|                      `95%` |                        `5%` |                                 `20x` |

That means if you were already using **DeepSeek API inside Claude Code**, the bridge's incremental
benefit is usually:

```text
~5x to 20x cheaper on large implementation jobs
```

assuming it offloads `80–95%` of the actual coding, reasoning, patching, and repair tokens.

### Combined efficiency estimate

The combined stack is:

```text
Claude Code coordinator + DeepSeek Bridge heavy-work offload
```

Practical efficiency ranges:

| Baseline                               |                   Expected savings |
| -------------------------------------- | ---------------------------------: |
| Claude Code using Sonnet directly      |             `~25x to 100x cheaper` |
| Claude Code already using DeepSeek API |               `~5x to 20x cheaper` |
| Claude Code using Haiku only           | bridge helps mainly on larger jobs |
| Tiny one-file edits                    | bridge may not be worth the overhead |

These are directional estimates based on the stated token-offload assumptions, not guaranteed
billing outcomes. Actual savings depend on prompt caching, model routing, task shape, coordinator
overhead, retries, and the pricing or limits attached to the DeepSeek web account.

The bridge is most useful when Claude Code would otherwise repeatedly send:

- repository context
- generated source
- diffs
- failed patches
- test output
- repair attempts
- long reasoning traces

Those are the tokens the bridge keeps out of Claude's context.

### Recommended operating mode

Use Claude Code as the coordinator. Use DeepSeek Bridge for the expensive work.

```text
Claude Code / Haiku:
  - coordinate
  - inspect local files
  - choose MCP tools
  - review results
  - avoid long loops

DeepSeek Bridge:
  - implementation
  - patch generation
  - test/repair cycles
  - substantial analysis
  - web research
```

Recommended default:

```text
Claude Code model: low-cost native Claude model, such as Haiku
Heavy implementation: deepseek_patch
Technical analysis: deepseek_expert
Code returned in chat: deepseek_generate
Current web research: deepseek_search
```

Avoid:

```text
Plan Mode
subagent fan-out
polling loops
sleep-based retry loops
dumping generated source into Claude context unnecessarily
using DeepSeek as Claude Code's Anthropic endpoint while also relying on this MCP bridge
```

### Direct DeepSeek API vs bridge mode

DeepSeek supports [Anthropic-compatible API integration for Claude Code](https://api-docs.deepseek.com/guides/agent_integrations/claude_code).
That is a different architecture:

```text
Claude Code -> DeepSeek API
```

This bridge uses:

```text
Claude Code -> MCP -> persistent DeepSeek web session
```

Do not mix the two casually. Direct DeepSeek API mode can reduce raw model cost. Bridge mode
reduces raw model cost **and** prevents large implementation loops from entering Claude Code's paid
context.

Use direct API mode when you want DeepSeek to be the coding-agent backend. Use bridge mode when
you want Claude Code to remain the local coordinator while DeepSeek handles the expensive work
outside Claude's context.

## Overnight jobs

Submit the work once:

```text
Use deepseek_patch to implement the downloader changes. Restrict changes to src and tests,
run python -m pytest, keep the Mac awake, and wait up to 12 hours.
```

For a phase that DeepSeek should own independently:

```text
Use deepseek_patch with task="Restart Phase 3 and independently determine the required work",
autonomous=true, test_command="mvn test", keep_awake=true, max_wait_hours=12.
Return the job ID and do not poll.
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

### `deepseek-v4-* is temporarily unavailable` before an MCP call

If Claude Code reports that a DeepSeek model cannot classify or approve
`mcp__deepseek-free__deepseek_patch`, the call has not reached this bridge. Check both the normal
Claude settings and VS Code's user settings. Remove DeepSeek direct-API overrides such as
`ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_MODEL`, and the
`ANTHROPIC_DEFAULT_*_MODEL` variables from `claudeCode.environmentVariables`, then fully restart
the Claude Code session. Those extension-level variables override `~/.claude/settings.json`.

For unattended patch submission, explicitly allow only the patch tool in
`~/.claude/settings.json` rather than disabling the permission system globally:

```json
{
  "permissions": {
    "allow": ["mcp__deepseek-free__deepseek_patch"]
  }
}
```

Keep a native Claude model such as Haiku as the coordinator. Direct DeepSeek API mode and this MCP
bridge are separate architectures and should not be enabled in the same Claude Code process.

Browser UI automation can still break when DeepSeek changes its site. The bridge does not bypass
login, CAPTCHAs, rate limits, or other access controls. Generated code remains untrusted and
should be reviewed before execution.

When Expert reports that it is busy, the bridge does not immediately retry. It records a
persistent exponential Expert cooldown (10, 20, 40, then 60 minutes) and schedules one controlled
Instant fallback for the job. A fallback requires a new chat because the model control disappears
after a conversation begins; all subsequent patch/test/repair turns remain in that fallback chat.
Expert requests are serialized, spaced by at least 60 seconds, and capped at eight per hour.
Instant requests are spaced by 15 seconds and capped at 20 requests per hour.
