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

import base64
import hmac
import json
import logging
import os
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import alerts as alertmod
from . import review, sizing
from .dashboard_page import FAVICON, PAGE

log = logging.getLogger(__name__)

ALIVE_WITHIN_S = 30.0
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
PASSWORD_ENV = "DASHBOARD_PASSWORD"


def password_from_env() -> str | None:
    return os.environ.get(PASSWORD_ENV) or None


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
        decisions_file: Path | None = None,
    ) -> None:
        files = state_file if isinstance(state_file, list) else [state_file]
        self.state_files = [Path(f) for f in files]
        self.stop_file = Path(stop_file)
        self.pause_file = Path(pause_file) if pause_file else self.stop_file.with_name("PAUSE")
        self.alerts_file = Path(alerts_file) if alerts_file else None
        self.decisions_file = Path(decisions_file) if decisions_file else None
        self._decisions_cache: dict[str, tuple[float, int, list[dict[str, Any]]]] = {}

    @staticmethod
    def _read(path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except ValueError:
            return None  # mid-write; the next poll will get it

    def resolve(self, name: str | None = None) -> Path:
        """The state file to show: the one asked for by basename, else the first
        candidate with a live heartbeat (the live loop comes first), else the
        most recently written."""
        if name:
            for f in self.state_files:
                if f.name == name:
                    return f
        now = time.time()
        for f in self.state_files:
            st = self._read(f) or {}
            last = st.get("last_tick_ts")
            if last is not None and now - float(last) <= ALIVE_WITHIN_S:
                return f
        existing = [f for f in self.state_files if f.exists()]
        if not existing:
            return self.state_files[0]
        return max(existing, key=lambda f: f.stat().st_mtime)

    @property
    def state_file(self) -> Path:
        return self.resolve()

    def companions(self, state_file: Path) -> tuple[Path | None, Path | None]:
        """(decisions, alerts) for a state file: the paper loop keeps its own."""
        if state_file.name.startswith("paper_"):
            return (
                state_file.with_name("paper_decisions.jsonl"),
                state_file.with_name("paper_alerts.jsonl"),
            )
        return self.decisions_file, self.alerts_file

    def files(self, now: float) -> list[dict[str, Any]]:
        out = []
        for f in self.state_files:
            st = self._read(f)
            if st is None and not f.exists():
                continue
            last = (st or {}).get("last_tick_ts")
            out.append(
                {
                    "name": f.name,
                    "alive": last is not None and now - float(last) <= ALIVE_WITHIN_S,
                    "env": ((st or {}).get("config") or {}).get("env"),
                }
            )
        return out

    def snapshot(self, now: float | None = None, name: str | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        state_file = self.resolve(name)
        state = self._read(state_file)
        _decisions_file, alerts_file = self.companions(state_file)
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
        rows = alertmod.tail(alerts_file, ALERTS_SHOWN)
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
            "alerts_file": str(alerts_file) if alerts_file else None,
            "files": self.files(now),
            "alerts": rows,
            "heartbeat": heartbeat,
            "alive": alive and not st.get("halted"),
        }

    # ------------------------------------------------------------ analysis

    def _decisions(self, path: Path | None) -> list[dict[str, Any]]:
        """Decision rows, re-read only when the file changes."""
        if path is None or not path.exists():
            return []
        st = path.stat()
        cached = self._decisions_cache.get(str(path))
        if cached and cached[:2] == (st.st_mtime, st.st_size):
            return cached[2]
        rows = review.load_decisions(path)
        self._decisions_cache[str(path)] = (st.st_mtime, st.st_size, rows)
        return rows

    def analysis(self, name: str | None = None) -> dict[str, Any]:
        """The review's cuts as JSON for the Analysis tab, plus every result row
        with its entry inputs for the Trades tab."""
        state_file = self.resolve(name)
        decisions_file, _alerts = self.companions(state_file)
        history = review.load_history(state_file)
        rows = review.attribute(history, self._decisions(decisions_file))
        for i, r in enumerate(rows):
            r["id"] = i
        rec = sizing.TrackRecord()
        for r in rows:
            rec.add(r.get("p_side"), r["net"])
        gross, fees = review.fee_share(rows)

        def cuts(key: str, edges=None):
            return [{"bucket": label, **stats} for label, stats in review.cut(rows, key, edges)]

        tiers = [
            {
                "tier": label,
                "n": t.n,
                "win_rate": t.win_rate,
                "net": t.net,
                "scaling": t.n >= sizing.MIN_TIER_RESULTS and t.net > 0,
            }
            for label, t in sorted(rec.tiers.items())
        ]
        return {
            "rows": rows,
            "gross": gross,
            "fees": fees,
            "cuts": {
                "how": cuts("how"),
                "side": cuts("side"),
                "series": cuts("series"),
                "confidence": cuts("p_side", review.CONFIDENCE),
                "ttc": cuts("secs_to_close", review.TTC),
                "distance": cuts("strike_bps", review.DISTANCE),
            },
            "tiers": tiers,
            "min_tier_results": sizing.MIN_TIER_RESULTS,
            "suggestions": review.suggest(rows) if rows else [],
        }

    def decisions_for(
        self, ticker: str, limit: int = 60, name: str | None = None
    ) -> list[dict[str, Any]]:
        decisions_file, _alerts = self.companions(self.resolve(name))
        rows = [d for d in self._decisions(decisions_file) if d.get("ticker") == ticker]
        return rows[-limit:]

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


def _query(path: str, key: str) -> str | None:
    values = parse_qs(urlparse(path).query).get(key) or []
    return values[0] or None if values else None


def make_handler(dash: Dashboard, password: str | None = None) -> type[BaseHTTPRequestHandler]:
    """``password`` (any username) is required on every request when set; it is
    what makes the page safe to reach from a phone over a private network."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # quieter than the default
            log.debug(fmt, *args)

        def _authorised(self) -> bool:
            if not password:
                return True
            header = self.headers.get("Authorization", "")
            if not header.startswith("Basic "):
                return False
            try:
                raw = base64.b64decode(header[6:].strip()).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                return False
            _user, _, given = raw.partition(":")
            return hmac.compare_digest(given, password)

        def _challenge(self) -> None:
            body = b"password required"
            self.send_response(HTTPStatus.UNAUTHORIZED)
            self.send_header("WWW-Authenticate", 'Basic realm="Lewis Wealth Global"')
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send(self, status: HTTPStatus, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - http.server API
            if not self._authorised():
                self._challenge()
                return
            if self.path in ("/", "/index.html"):
                self._send(HTTPStatus.OK, PAGE.encode(), "text/html; charset=utf-8")
            elif self.path in ("/favicon.svg", "/favicon.ico"):
                self._send(HTTPStatus.OK, FAVICON.encode(), "image/svg+xml")
            elif self.path.startswith("/api/state"):
                name = _query(self.path, "file")
                body = json.dumps(dash.snapshot(name=name)).encode()
                self._send(HTTPStatus.OK, body, "application/json")
            elif self.path.startswith("/api/analysis"):
                name = _query(self.path, "file")
                body = json.dumps(dash.analysis(name=name), default=str).encode()
                self._send(HTTPStatus.OK, body, "application/json")
            elif self.path.startswith("/api/decisions"):
                ticker = _query(self.path, "ticker") or ""
                name = _query(self.path, "file")
                body = json.dumps(dash.decisions_for(ticker, name=name), default=str).encode()
                self._send(HTTPStatus.OK, body, "application/json")
            else:
                self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")

        def do_POST(self) -> None:  # noqa: N802 - http.server API
            if not self._authorised():
                self._challenge()
                return
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
    decisions_file: Path | None = None,
    password: str | None = None,
) -> ThreadingHTTPServer:
    """Bind and return the server; call ``serve_forever`` on it.

    Binding to anything but the local machine requires a password: the page
    can stop and pause a live loop, so it must never be open to whoever finds
    the port.
    """
    if host not in LOCAL_HOSTS and not password:
        raise ValueError(
            f"binding to {host} exposes the dashboard beyond this machine; pass --password "
            "(or set DASHBOARD_PASSWORD) so a phone can reach it but a stranger cannot"
        )
    dash = Dashboard(
        state_file,
        stop_file,
        pause_file=pause_file,
        alerts_file=alerts_file,
        decisions_file=decisions_file,
    )
    try:
        return _Server((host, port), make_handler(dash, password))
    except OSError as exc:
        raise OSError(
            f"port {port} is already in use, probably by an earlier dashboard window; "
            f"close it or pass --port with a different number ({exc})"
        ) from exc
