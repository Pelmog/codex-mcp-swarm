import subprocess
import sys

import codex_mcp_swarm as m


def test_pid_start_time_on_this_platform():
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    try:
        st = m._get_pid_start_time(p.pid)
        assert isinstance(st, float)
        assert m._is_alive(p.pid, expected_start_time=st)
        assert not m._is_alive(p.pid, expected_start_time=st - 3600)  # PID reuse detected
    finally:
        p.kill()
        p.wait()


def test_call_tool_returns_dict_not_send(monkeypatch):
    sent = []
    monkeypatch.setattr(m, "_send", sent.append)
    out = m._call_tool("codex_status", {"task_ids": ["deadbeef"]})
    assert "result" in out and out["result"]["content"][0]["type"] == "text"
    assert m._call_tool("codex_status", {}) == {"error": {"code": -32602, "message": "task_ids is required"}}
    assert m._call_tool("nope", {})["error"]["code"] == -32601
    assert sent == []


def test_handle_still_sends(monkeypatch):
    sent = []
    monkeypatch.setattr(m, "_send", sent.append)
    m._handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
               "params": {"name": "codex_status", "arguments": {"task_ids": ["deadbeef"]}}})
    assert sent[0]["id"] == 7 and "result" in sent[0]
