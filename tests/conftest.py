import os
import sys
import tempfile

# TASK_DIR and friends are read when codex_mcp_swarm is imported, so set them first.
ROOT = tempfile.mkdtemp(prefix="codex_swarm_pytest_")
os.environ["CODEX_SWARM_TASK_DIR"] = os.path.join(ROOT, "tasks")
os.environ["CODEX_SWARM_WORKTREE_DIR"] = os.path.join(ROOT, "worktrees")
os.environ["CODEX_SWARM_LOG"] = os.path.join(ROOT, "server.log")
os.environ.setdefault("CODEX_SWARM_MIN_WAIT", "1")
os.environ.setdefault("CODEX_SWARM_HTTP_MAX_WAIT", "3")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
