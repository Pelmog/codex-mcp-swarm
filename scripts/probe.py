"""Probe the running codex-swarm HTTP service. Never prints the token.

  probe.py            auth + tools/list + codex_status (no Codex spawned)
  probe.py --smoke    also a real Codex round trip: codex_async -> codex_wait -> codex_reply
"""
import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

URL = f"http://127.0.0.1:{os.environ.get('CODEX_SWARM_HTTP_PORT', '8736')}/mcp"
TOKEN_FILE = Path(os.environ.get("CODEX_SWARM_HTTP_TOKEN_FILE",
                                 str(Path.home() / ".local/share/mcp-fleet/codex-swarm/http-token")))
TOOLS = {"codex", "codex_async", "codex_reply", "codex_status", "codex_wait", "codex_cancel"}
SMOKE = {"model": "gpt-5.6-luna", "config": {"model_reasoning_effort": "low"}, "sandbox": "read-only"}


def text(res):
    return res.content[0].text


async def until_done(s, task_id):
    for _ in range(10):  # 10 capped waits is ~40 min; smoke takes well under a minute
        out = text(await s.call_tool("codex_wait", {"task_ids": [task_id]}))
        if "STILL RUNNING" not in out:
            return out
    raise AssertionError("smoke task never finished")


async def probe(smoke: bool) -> None:
    async with httpx2.AsyncClient(timeout=10) as anon:
        r = await anon.post(URL, json={})
        assert r.status_code == 401, f"unauthenticated request got {r.status_code}, want 401"
    token = TOKEN_FILE.read_text().strip()
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=300) as hc:
        async with streamable_http_client(URL, http_client=hc) as (r, w, *_):
            async with ClientSession(r, w) as s:
                await s.initialize()
                names = {t.name for t in (await s.list_tools()).tools}
                assert names == TOOLS, f"tools: {sorted(names)}"
                out = text(await s.call_tool("codex_status", {"task_ids": ["00000000"]}))
                assert "NOT FOUND" in out.upper(), out
                if not smoke:
                    return
                repo = tempfile.mkdtemp(prefix="codex-swarm-smoke-")
                subprocess.run(["git", "init", "-q", repo], check=True)
                res = await s.call_tool("codex_async", {"prompt": "Reply with exactly PONG", "cwd": repo, **SMOKE})
                task_id = text(res).split("Codex task ")[1].split()[0]
                out = await until_done(s, task_id)
                assert "completed" in out and "PONG" in out, out
                thread = out.split("Thread ID: ")[1].split()[0]
                out = text(await s.call_tool("codex_reply", {"threadId": thread, "prompt": "Reply with exactly PONG2",
                                                             "cwd": repo, **SMOKE}))
                if "STILL RUNNING" in out:
                    out = await until_done(s, out.split("=== Task ")[1].split()[0])
                assert "PONG2" in out, out
                print(f"smoke OK (task {task_id}, thread {thread})")


if __name__ == "__main__":
    try:
        asyncio.run(probe("--smoke" in sys.argv))
    except Exception as exc:
        print(f"probe FAILED: {type(exc).__name__}: {exc}")
        sys.exit(1)
    print("probe OK")
