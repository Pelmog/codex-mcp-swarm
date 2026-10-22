"""codex-mcp-swarm as one shared, stateless Streamable HTTP MCP service.

A thin transport over codex_mcp_swarm: same six tools, same task files in
TASK_DIR, same server-level -c defaults. Differences from the stdio server,
all forced by sharing one long-lived process across many clients:

- every request needs `Authorization: Bearer <token>`;
- codex / codex_async / codex_reply require an absolute, existing `cwd`
  (the service's own cwd means nothing to the caller);
- no call outlives MAX_WAIT seconds, so none hits the client's ~5 min HTTP
  limit: sync `codex` and `codex_reply` run as async tasks and come back with
  a task_id if still running, and codex_wait's timeout is capped.
"""
import copy
import hmac
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

import anyio
import mcp_types as types
import uvicorn
from mcp.server.lowlevel import Server

import codex_mcp_swarm as core

MAX_WAIT = int(os.environ.get("CODEX_SWARM_HTTP_MAX_WAIT", "240"))
TOKEN_FILE = Path(os.environ.get(
    "CODEX_SWARM_HTTP_TOKEN_FILE",
    str(Path.home() / ".local/share/mcp-fleet/codex-swarm/http-token"),
))
THREADS = 256  # concurrent blocking tool calls (each codex_wait holds one thread)

_CAP_NOTE = (
    f" SHARED HTTP SERVICE: every call returns within ~{MAX_WAIT}s. If the task "
    "is still running you get its task_id -- call codex_wait again with it. "
    "cwd is required and must be an absolute, existing directory."
)


def _tools() -> list:
    tools = copy.deepcopy(core.TOOLS)
    for t in tools:
        schema = t["inputSchema"]
        if t["name"] in ("codex", "codex_async", "codex_reply"):
            schema["required"] = sorted(set(schema["required"]) | {"cwd"})
            t["description"] += _CAP_NOTE
        if t["name"] == "codex_reply":
            for k in ("cwd", "sandbox", "model", "config"):
                schema["properties"][k] = copy.deepcopy(core._CODEX_PROPERTIES[k])
        if t["name"] == "codex_wait":
            schema["properties"]["timeout"]["description"] = (
                f"Max seconds to wait; capped at {MAX_WAIT} on this service. "
                "The task keeps running if the wait times out."
            )
    return tools


def _err(message: str) -> Dict[str, Any]:
    return {"error": {"code": -32602, "message": message}}


def _bad_cwd(args: Dict[str, Any]) -> Optional[str]:
    cwd = args.get("cwd")
    if not isinstance(cwd, str) or not os.path.isabs(cwd):
        return "cwd is required and must be an absolute path"
    if not os.path.isdir(cwd):
        return f"cwd does not exist or is not a directory: {cwd}"
    return None


def _start_and_wait(params: Dict[str, Any]) -> Dict[str, Any]:
    task_id = core._start_async(params)["task_id"]
    return core._call_tool(
        "codex_wait", {"task_ids": [task_id], "timeout": MAX_WAIT}, uuid.uuid4().hex
    )


def call_tool(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Blocking dispatch with the shared-service rules; same contract as core._call_tool."""
    args = dict(args or {})
    if name in ("codex", "codex_async", "codex_reply"):
        bad = _bad_cwd(args)
        if bad:
            return _err(bad)
    if name == "codex":
        args.pop("timeout", None)
        return _start_and_wait(args)
    if name == "codex_reply":
        if not args.get("threadId") or not args.get("prompt"):
            return _err("threadId and prompt are required")
        # codex_async with threadId resumes with the caller's cwd/sandbox/model,
        # which the stdio codex_reply path cannot do.
        keep = ("threadId", "prompt", "cwd", "sandbox", "model", "config")
        return _start_and_wait({k: args[k] for k in keep if k in args})
    if name == "codex_wait":
        t = args.get("timeout")
        ok = isinstance(t, (int, float)) and not isinstance(t, bool) and t > 0
        args["timeout"] = min(t, MAX_WAIT) if ok else MAX_WAIT  # null would mean "forever"
    return core._call_tool(name, args, uuid.uuid4().hex)


def _to_result(out: Dict[str, Any]) -> types.CallToolResult:
    if "error" in out:
        text = out["error"]["message"]
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)], isError=True)
    content = [types.TextContent(type="text", text=c["text"]) for c in out["result"]["content"]]
    return types.CallToolResult(content=content)


async def _list_tools(ctx, params) -> types.ListToolsResult:
    return types.ListToolsResult(tools=[types.Tool(**t) for t in _tools()])


async def _call(ctx, params) -> types.CallToolResult:
    anyio.to_thread.current_default_thread_limiter().total_tokens = THREADS
    try:
        # abandon_on_cancel: a client disconnect ends only the wait; the Codex
        # task is detached and keeps running until codex_cancel.
        out = await anyio.to_thread.run_sync(
            call_tool, params.name, params.arguments or {}, abandon_on_cancel=True
        )
    except Exception as exc:  # spawn/worktree failures become tool errors, not HTTP 500s
        logging.exception("tool %s failed", params.name)
        out = {"error": {"code": -32603, "message": f"{params.name} failed: {exc}"}}
    return _to_result(out)


class _BearerAuth:
    """Reject any HTTP request without the exact static bearer token."""

    def __init__(self, app, token: str):
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            got = dict(scope["headers"]).get(b"authorization", b"")
            if not hmac.compare_digest(got, self.expected):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
                return
        await self.app(scope, receive, send)


def build_app(token: str):
    server = Server("codex", version=core.__version__,
                    on_list_tools=_list_tools, on_call_tool=_call)
    return _BearerAuth(server.streamable_http_app(stateless_http=True), token)


def _start_cleanup_thread(interval: float = 300) -> None:
    """Run the 24h task/worktree sweep on a timer, not only when a task starts."""
    def loop():
        while True:
            time.sleep(interval)
            try:
                core._cleanup_old_tasks()
            except Exception:
                logging.exception("cleanup sweep failed")
    threading.Thread(target=loop, daemon=True, name="codex-swarm-cleanup").start()


def main() -> None:
    core._parse_args()  # same -c / --skip-git-repo-check flags as the stdio server
    token = TOKEN_FILE.read_text().strip()
    if not token:
        raise SystemExit(f"empty token file: {TOKEN_FILE}")
    _start_cleanup_thread()
    # Single process on the main thread: core installs its SIGCHLD reaper at
    # import time, so no --reload / --workers.
    uvicorn.run(
        build_app(token),
        host=os.environ.get("CODEX_SWARM_HTTP_HOST", "127.0.0.1"),
        port=int(os.environ.get("CODEX_SWARM_HTTP_PORT", "8736")),
        log_level="warning",
    )


if __name__ == "__main__":
    main()
