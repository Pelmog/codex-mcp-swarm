"""The real HTTP app under uvicorn, a stub `codex` on PATH, the SDK client on the wire."""
import asyncio
import json
import os
import socket
import threading
import time

import httpx2
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

STUB = os.path.join(os.path.dirname(__file__), "stub_codex")
os.environ["PATH"] = STUB + os.pathsep + os.environ["PATH"]

import codex_mcp_swarm as m  # noqa: E402
import codex_swarm_http as h  # noqa: E402

TOKEN = "test-token"
m.TASK_DIR.mkdir(parents=True, exist_ok=True)


@pytest.fixture(scope="module")
def url():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = uvicorn.Server(uvicorn.Config(h.build_app(TOKEN), host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    while not server.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True
    t.join(5)


async def _call(url, name, args=None, token=TOKEN):
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=30) as hc:
        async with streamable_http_client(url, http_client=hc) as (r, w, *_):
            async with ClientSession(r, w) as s:
                await s.initialize()
                if name == "__list__":
                    return await s.list_tools()
                return await s.call_tool(name, args or {})


def call(url, name, args=None):
    return asyncio.run(_call(url, name, args))


def text(res):
    return res.content[0].text


def task_id_of(res):
    return text(res).split("Codex task ")[1].split()[0]


def test_rejects_missing_or_wrong_token(url):
    assert httpx2.post(url, json={}).status_code == 401
    assert httpx2.post(url, json={}, headers={"Authorization": "Bearer nope"}).status_code == 401


def test_lists_six_tools_with_cwd_required(url):
    tools = {t.name: t for t in call(url, "__list__").tools}
    assert set(tools) == {"codex", "codex_async", "codex_reply", "codex_status", "codex_wait", "codex_cancel"}
    for name in ("codex", "codex_async", "codex_reply"):
        assert "cwd" in tools[name].input_schema["required"]
    assert {"sandbox", "model"} <= set(tools["codex_reply"].input_schema["properties"])


@pytest.mark.parametrize("cwd", [None, "relative/dir", "/definitely/not/here"])
def test_cwd_validation(url, cwd):
    args = {"prompt": "hi"} | ({"cwd": cwd} if cwd else {})
    res = call(url, "codex_async", args)
    assert res.is_error and "cwd" in text(res)


def test_sync_codex_completes_in_cwd(url, tmp_path):
    res = call(url, "codex", {"prompt": "hello", "cwd": str(tmp_path)})
    assert not res.is_error
    assert f"cwd={tmp_path}" in text(res) and "completed" in text(res)
    assert "Thread ID: stub-thread" in text(res)


def test_sync_codex_returns_task_id_after_cap(url, tmp_path):
    res = call(url, "codex", {"prompt": "sleep:6", "cwd": str(tmp_path)})
    assert "STILL RUNNING" in text(res) and "codex_wait" in text(res)
    tid = text(res).split("=== Task ")[1].split()[0]
    deadline = time.time() + 20
    out = ""
    while time.time() < deadline and "completed" not in out:
        out = text(call(url, "codex_wait", {"task_ids": [tid], "timeout": None}))
    assert "completed" in out


def test_wait_clamps_null_huge_and_junk_timeouts(url, tmp_path):
    tid = task_id_of(call(url, "codex_async", {"prompt": "sleep:30", "cwd": str(tmp_path)}))
    try:
        for t in (None, 100000, "x"):
            start = time.time()
            out = text(call(url, "codex_wait", {"task_ids": [tid], "timeout": t}))
            assert time.time() - start < 10 and "STILL RUNNING" in out
    finally:
        call(url, "codex_cancel", {"task_id": tid})


def test_parallel_waits_do_not_serialise(url, tmp_path):
    ids = [task_id_of(call(url, "codex_async", {"prompt": "sleep:30", "cwd": str(tmp_path)})) for _ in range(2)]

    async def both():
        return await asyncio.gather(*[_call(url, "codex_wait", {"task_ids": [i]}) for i in ids])

    try:
        start = time.time()
        asyncio.run(both())
        assert time.time() - start < 6  # two 3s capped waits overlapped
    finally:
        for i in ids:
            call(url, "codex_cancel", {"task_id": i})


def test_reply_resumes_in_given_cwd_with_sandbox(url, tmp_path):
    out = text(call(url, "codex_reply", {"threadId": "t-123", "prompt": "again",
                                         "cwd": str(tmp_path), "sandbox": "read-only"}))
    assert f"cwd={tmp_path}" in out and "resume=True" in out
    assert "sandbox_mode=read-only" in out and "t-123" in out


def test_legacy_meta_from_stdio_copy_resolves(url):
    # A 1.7.0 stdio copy writes no exit_code_lost / status_inferred / pid_start_time.
    tid = "abcd1234"
    (m.TASK_DIR / f"{tid}.meta").write_text(json.dumps(
        {"task_id": tid, "pid": 99999, "status": "running", "command": "codex exec",
         "started_at": time.time() - 5}))
    (m.TASK_DIR / f"{tid}.stdout").write_text(
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "legacy done"}})
        + "\n" + json.dumps({"type": "turn.completed"}) + "\n")
    (m.TASK_DIR / f"{tid}.stderr").write_text("")
    out = text(call(url, "codex_wait", {"task_ids": [tid]}))
    assert "completed" in out and "legacy done" in out


def test_disconnect_does_not_kill_task(url, tmp_path):
    tid = task_id_of(call(url, "codex_async", {"prompt": "sleep:4", "cwd": str(tmp_path)}))
    with pytest.raises(BaseException):
        asyncio.run(asyncio.wait_for(_call(url, "codex_wait", {"task_ids": [tid]}), 0.5))
    time.sleep(5)
    assert "completed" in text(call(url, "codex_wait", {"task_ids": [tid]}))


def test_cleanup_thread_runs(monkeypatch):
    calls = []
    monkeypatch.setattr(m, "_cleanup_old_tasks", lambda: calls.append(1))
    h._start_cleanup_thread(interval=0.1)
    time.sleep(0.35)
    assert len(calls) >= 2


def test_one_wait_on_many_running_ids_respects_cap(url, tmp_path):
    ids = [task_id_of(call(url, "codex_async", {"prompt": "sleep:30", "cwd": str(tmp_path)})) for _ in range(3)]
    try:
        start = time.time()
        out = text(call(url, "codex_wait", {"task_ids": ids}))
        assert time.time() - start < 6  # one deadline per call, not per task
        assert out.count("STILL RUNNING") == 3
    finally:
        for i in ids:
            call(url, "codex_cancel", {"task_id": i})
