# Shared stateless HTTP service (operator notes)

One `codex` MCP service for every Claude Code session on this Mac, in place of one stdio
`codex-mcp-swarm` process per session.

| What | Where |
|---|---|
| Endpoint | `http://127.0.0.1:8736/mcp`: stateless Streamable HTTP; bearer token required |
| Token | `~/.local/share/mcp-fleet/codex-swarm/http-token` (0600; never printed) |
| LaunchAgent | `com.local.codex-swarm-http`: `~/Library/LaunchAgents/com.local.codex-swarm-http.plist` |
| Log | `~/.local/share/mcp-fleet/codex-swarm/service.log`; core log `/tmp/codex_mcp_swarm.log` |
| Task state | `/tmp/codex_swarm_tasks` (shared with any stdio copies), worktrees `/tmp/codex-swarm-worktrees` |
| Binary | `~/.local/bin/codex-mcp-swarm-http` (`uv tool install --editable ~/repos/codex-mcp-swarm-v2`) |
| Rollback target | `~/.local/bin/codex-mcp-swarm`: the untouched 1.7.0 uv tool |

## Commands (`scripts/manage-local-service.py`)

```bash
python3 scripts/manage-local-service.py install [--rotate-token]   # token, plist, (re)load, probe
python3 scripts/manage-local-service.py probe [--smoke]            # --smoke = real Codex async/wait/reply
python3 scripts/manage-local-service.py switch-clients --scope local   # canary: new sessions in this repo
python3 scripts/manage-local-service.py switch-clients --scope user    # everyone (saves stdio entry)
python3 scripts/manage-local-service.py rollback --scope local|user [--stop]
```

After `--rotate-token`, re-run `switch-clients` for each switched scope, because the token is
baked into the Claude entry. After a code change, run `launchctl kickstart -k gui/$(id -u)/com.local.codex-swarm-http`,
then `probe`.

## How it differs from the stdio server

- `cwd` is **required** (an absolute, existing directory) on `codex`, `codex_async` and
  `codex_reply`.
- **No call outlives 240 s** (`CODEX_SWARM_HTTP_MAX_WAIT`), which keeps calls under Claude
  Code's ~5 min HTTP limit:
  - Sync `codex` and `codex_reply` return the result, or a task id when still running. Call
    `codex_wait` again with that id.
  - `codex_wait` timeouts are clamped to 240. Null, huge and invalid values all mean 240.
- `codex_reply` accepts `cwd`, `sandbox`, `model` and `config`, and runs as
  `codex_async(threadId=…)`. A read-only scout therefore stays read-only when resumed. The
  stdio 1.7.0 reply dropped all of these.
- The 24 h task and worktree sweep runs every 300 s, not only when a task starts.
- A client disconnect ends only the wait. The Codex task runs on until it finishes or
  `codex_cancel` stops it.

## Gotchas

- launchd does not expand `~`, and `/opt/homebrew/bin/codex` is an ancient 0.27.0. The plist
  PATH is absolute, with `~/.local/bin` first. Check it with
  `launchctl print gui/$(id -u)/com.local.codex-swarm-http | grep PATH`.
- Existing sessions keep their stdio copies until they restart. Only new sessions use HTTP.
- Run one process only (no reload, no workers). The core's SIGCHLD reaper records exit codes on
  the main thread.
