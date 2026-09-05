"""Local dashboard for the demo loop.

A single-page site served from the standard library, bound to localhost. It
polls the loop's JSON state file every two seconds, so it works whether or
not the loop is running, and it can stop the loop by creating the same stop
file the loop watches, pause and resume it the same way, and shows the
event feed both loops append to. No third-party dependencies, nothing
leaves the machine.

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

from . import alerts as alertmod
from .dashboard_page import PAGE

log = logging.getLogger(__name__)

ALIVE_WITHIN_S = 30.0
STALE_AFTER_S = 90.0  # a running loop that has not ticked for this long is presumed dead
ALERTS_SHOWN = 40


class Dashboard:
    """State that the HTTP handler reads; one per server.

    ``state_files`` may list several candidates (the live and the demo loop
    write different files); each poll shows the most recently modified one.
    """

    def __init__(
        self,
        state_file: Path | list[Path],
        stop_file: Path,
        pause_file: Path | None = None,
        alerts_file: Path | None = None,
    ) -> None:
        files = state_file if isinstance(state_file, list) else [state_file]
        self.state_files = [Path(f) for f in files]
        self.stop_file = Path(stop_file)
        self.pause_file = Path(pause_file) if pause_file else self.stop_file.with_name("PAUSE")
        self.alerts_file = Path(alerts_file) if alerts_file else None

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
        st = state or {}
        last = st.get("last_tick_ts")
        alive = bool(state) and last is not None and now - float(last) <= ALIVE_WITHIN_S
        # heartbeat: a loop that ended says so in ``stopped``; one that simply
        # went quiet (crash, closed laptop, lost network) is "stale"
        if not state:
            heartbeat = "none"
        elif st.get("stopped"):
            heartbeat = "stopped"
        elif alive:
            heartbeat = (
                "halted" if st.get("halted") else ("paused" if st.get("paused") else "alive")
            )
        elif last is not None and now - float(last) > STALE_AFTER_S:
            heartbeat = "stale"
        else:
            heartbeat = "quiet"
        rows = alertmod.tail(self.alerts_file, ALERTS_SHOWN)
        if heartbeat == "stale":
            rows.append(
                {
                    "ts": now,
                    "level": "warn",
                    "source": "dashboard",
                    "text": f"no heartbeat for {int(now - float(last))}s: the loop is not "
                    "running. Restart it, or check the window it ran in",
                }
            )
        return {
            "now": now,
            "state": state,
            "state_file": str(state_file),
            "stop_file": str(self.stop_file),
            "stop_file_present": self.stop_file.exists(),
            "pause_file": str(self.pause_file),
            "pause_file_present": self.pause_file.exists(),
            "alerts_file": str(self.alerts_file) if self.alerts_file else None,
            "alerts": rows,
            "heartbeat": heartbeat,
            "alive": alive and not st.get("halted"),
        }

    def stop(self) -> None:
        self.stop_file.parent.mkdir(parents=True, exist_ok=True)
        self.stop_file.write_text(
            f"stopped from dashboard at {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        )

    def clear_stop(self) -> None:
        if self.stop_file.exists():
            self.stop_file.unlink()

    def pause(self) -> None:
        self.pause_file.parent.mkdir(parents=True, exist_ok=True)
        self.pause_file.write_text(
            f"paused from dashboard at {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        )

    def resume(self) -> None:
        if self.pause_file.exists():
            self.pause_file.unlink()


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
            elif self.path == "/api/pause":
                dash.pause()
            elif self.path == "/api/resume":
                dash.resume()
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
    state_file: Path | list[Path],
    stop_file: Path,
    host: str = "127.0.0.1",
    port: int = 8765,
    pause_file: Path | None = None,
    alerts_file: Path | None = None,
) -> ThreadingHTTPServer:
    """Bind and return the server; call ``serve_forever`` on it."""
    dash = Dashboard(state_file, stop_file, pause_file=pause_file, alerts_file=alerts_file)
    try:
        return _Server((host, port), make_handler(dash))
    except OSError as exc:
        raise OSError(
            f"port {port} is already in use, probably by an earlier dashboard window; "
            f"close it or pass --port with a different number ({exc})"
        ) from exc
