"""stdout is the MCP protocol channel: nothing else may write to it."""
from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

import split_ticket_mcp.server as server

ROOT = Path(__file__).resolve().parent.parent


def test_importing_the_server_prints_nothing():
    """Review Focus #5."""
    done = subprocess.run([sys.executable, "-c", "import split_ticket_mcp.server"],
                          cwd=ROOT, capture_output=True, text=True, check=True)
    assert done.stdout == ""


def test_main_logs_to_stderr_and_runs_stdio(monkeypatch):
    ran = []
    monkeypatch.setattr(server.mcp, "run", lambda *a, **k: ran.append((a, k)))
    root = logging.getLogger()
    saved = root.handlers[:]
    try:
        server.main()
        streams = [getattr(h, "stream", None) for h in root.handlers]
        assert sys.stderr in streams and sys.stdout not in streams
    finally:
        root.handlers[:] = saved
    assert ran == [((), {})]


def test_the_readme_documents_the_setup():
    text = (ROOT / "README.md").read_text()
    assert 'pip install -e ".[mcp]"' in text
    assert "claude mcp add split-tickets" in text
    assert '"mcpServers"' in text
