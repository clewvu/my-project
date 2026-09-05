"""Local dashboard for the demo loop.

A single-page site served from the standard library, bound to localhost. It
polls the loop's JSON state file every two seconds, so it works whether or
not the loop is running, and it can stop the loop by creating the same stop
file the loop watches. No third-party dependencies, nothing leaves the
machine.

    kalshi-bot demo-ui            # then open http://127.0.0.1:8765
"""

from __future__ import annotations

import json
import logging
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .dashboard_page import PAGE

log = logging.getLogger(__name__)

ALIVE_WITHIN_S = 30.0


class Dashboard:
    """State that the HTTP handler reads; one per server.

    ``state_files`` may list several candidates (the live and the demo loop
    write different files); each poll shows the most recently modified one.
    """

    def __init__(self, state_file: Path | list[Path], stop_file: Path) -> None:
        files = state_file if isinstance(state_file, list) else [state_file]
        self.state_files = [Path(f) for f in files]
        self.stop_file = Path(stop_file)

    @property
    def state_file(self) -> Path:
        existing = [f for f in self.state_files if f.exists()]
        if not existing:
            return self.state_files[0]
        return max(existing, key=lambda f: f.stat().st_mtime)

    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        state: dict[str, Any] | None = None
        state_file = self.state_file
        if state_file.exists():
            try:
                state = json.loads(state_file.read_text())
            except ValueError:
                state = None  # mid-write; the next poll will get it
        last = (state or {}).get("last_tick_ts")
        alive = bool(state) and last is not None and now - float(last) <= ALIVE_WITHIN_S
        return {
            "now": now,
            "state": state,
            "state_file": str(state_file),
            "stop_file": str(self.stop_file),
            "stop_file_present": self.stop_file.exists(),
            "alive": alive and not (state or {}).get("halted"),
        }

    def stop(self) -> None:
        self.stop_file.parent.mkdir(parents=True, exist_ok=True)
        self.stop_file.write_text(
            f"stopped from dashboard at {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        )

    def clear_stop(self) -> None:
        if self.stop_file.exists():
            self.stop_file.unlink()


def make_handler(dash: Dashboard) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # quieter than the default
            log.debug(fmt, *args)

        def _send(self, status: HTTPStatus, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - http.server API
            if self.path in ("/", "/index.html"):
                self._send(HTTPStatus.OK, PAGE.encode(), "text/html; charset=utf-8")
            elif self.path == "/api/state":
                body = json.dumps(dash.snapshot()).encode()
                self._send(HTTPStatus.OK, body, "application/json")
            else:
                self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")

        def do_POST(self) -> None:  # noqa: N802 - http.server API
            if self.path == "/api/stop":
                dash.stop()
            elif self.path == "/api/clear-stop":
                dash.clear_stop()
            else:
                self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
                return
            self._send(HTTPStatus.OK, json.dumps(dash.snapshot()).encode(), "application/json")

    return Handler


class _Server(ThreadingHTTPServer):
    # http.server sets allow_reuse_address, which on Windows lets a second
    # dashboard bind a port an older one still serves; the browser then keeps
    # talking to the stale process. Fail loudly instead.
    allow_reuse_address = False
    daemon_threads = True


def serve(
    state_file: Path | list[Path], stop_file: Path, host: str = "127.0.0.1", port: int = 8765
) -> ThreadingHTTPServer:
    """Bind and return the server; call ``serve_forever`` on it."""
    try:
        return _Server((host, port), make_handler(Dashboard(state_file, stop_file)))
    except OSError as exc:
        raise OSError(
            f"port {port} is already in use, probably by an earlier dashboard window; "
            f"close it or pass --port with a different number ({exc})"
        ) from exc
