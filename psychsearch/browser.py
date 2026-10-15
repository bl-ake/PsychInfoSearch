"""A separate, visible Firefox that the tool drives over WebDriver BiDi.

Firefox is started as an ordinary app with its own profile and a remote-control port that only
this machine can reach. You log in by hand in that window; the tool then loads pages and reads them.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import websocket  # websocket-client

PORT = int(os.environ.get("PSYCHSEARCH_BROWSER_PORT", "9333"))
if sys.platform == "darwin":
    PROFILE = Path.home() / "Library" / "Application Support" / "psychsearch" / "firefox-profile"
else:
    PROFILE = Path.home() / ".local" / "share" / "psychsearch" / "firefox-profile"


class BrowserError(Exception):
    pass


def is_running(port: int = PORT) -> bool:
    with socket.socket() as probe:
        probe.settimeout(1)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def launch(url: str, port: int = PORT) -> None:
    """Open the automation Firefox on `url`. On macOS it must go through `open`: a Firefox started
    as a child of the terminal cannot reach its profile or create a usable window."""
    PROFILE.mkdir(parents=True, exist_ok=True)
    args = ["-no-remote", "-profile", str(PROFILE), "--remote-debugging-port", str(port), "-new-window", url]
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", "-n", "-a", "Firefox", "--args", *args], check=True)
        else:
            subprocess.Popen([shutil.which("firefox") or "firefox", *args])
    except (OSError, subprocess.CalledProcessError) as exc:
        raise BrowserError(f"could not start Firefox: {exc}") from None


class BiDi:
    """Just enough of WebDriver BiDi: list tabs, navigate, run JavaScript in a page."""

    def __init__(self, port: int = PORT, timeout: float = 60):
        try:
            self.ws = websocket.create_connection(f"ws://127.0.0.1:{port}/session", timeout=timeout, suppress_origin=True)
        except (OSError, websocket.WebSocketException) as exc:
            raise BrowserError(f"could not connect to the automation Firefox on port {port}: {exc}") from None
        self.next_id = 0
        self.send("session.new", {"capabilities": {}})

    def send(self, method: str, params: dict | None = None) -> dict:
        self.next_id += 1
        try:
            self.ws.send(json.dumps({"id": self.next_id, "method": method, "params": params or {}}))
            while True:
                message = json.loads(self.ws.recv())
                if message.get("id") == self.next_id:
                    break
        except (OSError, websocket.WebSocketException) as exc:
            raise BrowserError(f"lost contact with the automation Firefox during {method}: {exc}") from None
        if message.get("type") == "error":
            raise BrowserError(f"{method}: {message.get('error')}: {message.get('message')}")
        return message.get("result") or {}

    def tabs(self) -> list[dict]:
        contexts = self.send("browsingContext.getTree", {"maxDepth": 0})["contexts"]
        return [{"context": c["context"], "url": c["url"]} for c in contexts]

    def navigate(self, context: str, url: str) -> None:
        # "interactive": the new document is parsed. Waiting for "complete" also waits for every image and
        # tracking script, and one slow one can stall it long after the results are on screen.
        self.send("browsingContext.navigate", {"context": context, "url": url, "wait": "interactive"})

    def bring_to_front(self, context: str | None = None) -> None:
        if context is None:
            tabs = self.tabs()
            if not tabs:
                return
            context = tabs[-1]["context"]
        self.send("browsingContext.activate", {"context": context})

    def evaluate(self, context: str, function_body: str):
        """Run a JavaScript function body in the page and return its (JSON-serialisable) result."""
        expression = f"(async () => JSON.stringify(await (async () => {{ {function_body} }})()))()"
        result = self.send(
            "script.evaluate",
            {"expression": expression, "target": {"context": context}, "awaitPromise": True, "resultOwnership": "none"},
        )
        if result.get("type") != "success":
            raise BrowserError("page script failed: " + json.dumps(result.get("exceptionDetails", result))[:400])
        value = result["result"].get("value")
        return json.loads(value) if isinstance(value, str) else None

    def close(self) -> None:
        try:
            self.send("session.end")
        except BrowserError:
            pass
        try:
            self.ws.close()
        except OSError:
            pass
