"""Install, probe, switch Claude clients to, or roll back the shared codex-swarm HTTP service.

  install                          token + LaunchAgent + probe
  probe [--smoke]                  check the running service (smoke = real Codex round trip)
  switch-clients --scope local     canary: `codex` -> HTTP for sessions started in this repo
  switch-clients --scope user      every new Claude session (saves the stdio entry first)
  rollback --scope local|user [--stop]

Never prints the token. Uses the `claude` CLI rather than rewriting ~/.claude.json,
which ~100 live sessions write concurrently.
"""
import argparse
import json
import os
import plistlib
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
REPO = Path(__file__).resolve().parents[1]
STATE = HOME / ".local/share/mcp-fleet/codex-swarm"
TOKEN = STATE / "http-token"
BACKUP = STATE / "backups/claude-user-codex.json"
LABEL = "com.local.codex-swarm-http"
PLIST = HOME / "Library/LaunchAgents" / f"{LABEL}.plist"
DOMAIN = f"gui/{os.getuid()}"
BIN = HOME / ".local/bin/codex-mcp-swarm-http"
TOOL_PY = HOME / ".local/share/uv/tools/codex-mcp-swarm-http/bin/python"
URL = "http://127.0.0.1:8736/mcp"
# Verbatim from the user-scope stdio `codex` entry, so both transports run Codex identically.
SERVER_ARGS = [
    "-c", "approval_policy=never", "-c", "sandbox_mode=workspace-write",
    "-c", "model_reasoning_effort=medium", "--skip-git-repo-check",
] + [a for name in ("chrome-devtools", "google-workspace", "cclsp", "hetzner-firewall",
                    "remote-bash", "node_repl", "outlook", "elevenlabs")
     for a in ("-c", f"mcp_servers.{name}.enabled=false")]


def secure_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def service_path() -> str:
    # launchd does not expand ~, and /opt/homebrew/bin/codex is an ancient 0.27.0:
    # ~/.local/bin must come first, as an absolute path. node (for Codex's own MCP
    # servers) is taken from wherever this shell resolves it (nvm).
    dirs = [str(HOME / ".local/bin"), str(HOME / ".bun/bin")]
    node = shutil.which("node")
    if node:
        dirs.append(str(Path(node).parent))
    dirs += ["/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    return ":".join(dict.fromkeys(dirs))


def probe(smoke: bool = False) -> bool:
    r = subprocess.run([str(TOOL_PY), str(REPO / "scripts/probe.py")] + (["--smoke"] if smoke else []),
                       capture_output=True, text=True)
    print((r.stdout + r.stderr).strip())
    return r.returncode == 0


def install() -> None:
    assert BIN.exists(), f"{BIN} missing: uv tool install --editable {REPO}"
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    STATE.chmod(0o700)
    if not TOKEN.exists() or "--rotate-token" in sys.argv:
        secure_write(TOKEN, secrets.token_urlsafe(32).encode())
    plist = {
        "Label": LABEL,
        "ProgramArguments": [str(BIN), *SERVER_ARGS],
        "EnvironmentVariables": {"HOME": str(HOME), "PATH": service_path(), "LANG": "en_GB.UTF-8"},
        "WorkingDirectory": str(HOME),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 5,
        "StandardOutPath": str(STATE / "service.log"),
        "StandardErrorPath": str(STATE / "service.log"),
    }
    PLIST.write_bytes(plistlib.dumps(plist))
    subprocess.run(["plutil", "-lint", str(PLIST)], check=True, capture_output=True)
    subprocess.run(["launchctl", "bootout", f"{DOMAIN}/{LABEL}"], capture_output=True)
    for _ in range(40):  # bootout returns before the job is gone; bootstrap then fails with EIO (5)
        if subprocess.run(["launchctl", "print", f"{DOMAIN}/{LABEL}"], capture_output=True).returncode:
            break
        time.sleep(0.25)
    subprocess.run(["launchctl", "bootstrap", DOMAIN, str(PLIST)], check=True)
    for _ in range(30):
        time.sleep(0.5)
        if probe():
            return
    sys.exit(f"service probe failed; clients unchanged. Log: {STATE / 'service.log'}")


def claude(*args: str, cwd: Path = HOME) -> None:
    # Errors print the CLI's stderr only: argv can carry the token.
    r = subprocess.run(["claude", "mcp", *args], cwd=cwd, capture_output=True, text=True)
    if r.returncode:
        token = TOKEN.read_text().strip() if TOKEN.exists() else "\0"
        sys.exit(f"claude mcp {args[0]} failed: {(r.stderr or r.stdout).replace(token, '<token>').strip()}")


def add_http(scope: str, cwd: Path) -> None:
    # add-json, not `add -H`: -H is variadic and swallows the name and URL.
    token = TOKEN.read_text().strip()
    entry = {"type": "http", "url": URL, "headers": {"Authorization": f"Bearer {token}"}}
    claude("add-json", "-s", scope, "codex", json.dumps(entry), cwd=cwd)


def switch(scope: str) -> None:
    if not probe():
        sys.exit("probe failed; refusing to switch clients")
    if scope == "local":
        add_http("local", REPO)
        print(f"local-scope `codex` -> {URL} for new sessions in {REPO}")
        return
    entry = json.loads((HOME / ".claude.json").read_text())["mcpServers"]["codex"]
    assert entry.get("type", "stdio") == "stdio", "user-scope codex is not the stdio entry; already switched?"
    secure_write(BACKUP, json.dumps(entry, indent=2).encode())
    claude("remove", "-s", "user", "codex")
    add_http("user", HOME)
    print(f"user-scope `codex` -> {URL}; stdio entry saved to {BACKUP}. Existing sessions keep their stdio copies.")


def rollback(scope: str, stop: bool) -> None:
    if scope == "local":
        claude("remove", "-s", "local", "codex", cwd=REPO)
    else:
        saved = BACKUP.read_text()
        claude("remove", "-s", "user", "codex")
        claude("add-json", "-s", "user", "codex", saved)
    print(f"{scope}-scope `codex` restored")
    if stop:
        subprocess.run(["launchctl", "bootout", f"{DOMAIN}/{LABEL}"], check=False)
        print("service stopped")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["install", "probe", "switch-clients", "rollback"])
    p.add_argument("--scope", choices=["local", "user"])
    p.add_argument("--smoke", action="store_true")
    p.add_argument("--stop", action="store_true")
    p.add_argument("--rotate-token", action="store_true", help="install: replace the token (re-run switch-clients after)")
    a = p.parse_args()
    if a.action in ("switch-clients", "rollback") and not a.scope:
        p.error("--scope is required")
    if a.action == "install":
        install()
    elif a.action == "probe":
        sys.exit(0 if probe(a.smoke) else 1)
    elif a.action == "switch-clients":
        switch(a.scope)
    else:
        rollback(a.scope, a.stop)
