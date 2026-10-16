"""
Workspace: one place to drop telemetry of any supported format and work on it.

A workspace is a project folder (see store.py) with two more things in it:

  dbc/     the CAN databases dropped into the workspace; every CAN log
           imported afterwards is decoded with them
  inbox/   files on their way in (and .ldx lap-marker files waiting for their .ld)

`Workspace` does the work and has no web code in it. `serve` puts a small page
in front of it on this machine only (127.0.0.1): drop files in, see what each
one was read as and every note from its import, open its report.

  veltrix workspace my_project
"""

from __future__ import annotations

import json
import re
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import numpy as np

from ..importers import read_dbc, supported_extensions, supported_formats
from ..store import ProjectStore

_PAGE = Path(__file__).with_name("page.html")
MAX_UPLOAD = 4 * 1024 ** 3


def _clean(value):
    """A value as JSON can carry it (NaN and numpy scalars are not JSON)."""
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        return float(value) if np.isfinite(value) else None
    return value


class Workspace:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.dbc_dir, self.inbox = self.path / "dbc", self.path / "inbox"
        for folder in (self.path, self.dbc_dir, self.inbox):
            folder.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()          # one import at a time; SQLite is opened per call
        self._do(lambda store: None)

    def _do(self, action):
        store = ProjectStore(self.path)
        try:
            return action(store)
        finally:
            store.close()

    # ── read ──────────────────────────────────────────────────────────────────

    def dbc_files(self) -> list:
        return sorted(self.dbc_dir.glob("*.dbc"))

    def sessions(self) -> list:
        def read(store):
            rows = []
            for row in store.sessions().to_dict("records"):
                notes = store.notes(row["id"])
                row["warnings"] = sum(level == "warning" for level, _ in notes)
                rows.append({k: _clean(v) for k, v in row.items()})
            return rows
        return self._do(read)

    def session(self, session_id: int) -> dict:
        def read(store):
            row = store.db.execute("SELECT name, source_file, format, mapping, units, file_info, rate_hz, "
                                   "lap_length_m, lap_source FROM session WHERE id = ?", (session_id,)).fetchone()
            if row is None:
                raise KeyError(f"No session {session_id} in this workspace.")
            laps = store.laps(session_id)
            units = json.loads(row["units"])
            return {"id": session_id, "name": row["name"], "source_file": row["source_file"],
                    "format": row["format"], "rate_hz": row["rate_hz"], "lap_length_m": _clean(row["lap_length_m"]),
                    "lap_source": row["lap_source"], "info": json.loads(row["file_info"]),
                    "channels": [{"channel": c, "column": col, "unit": units.get(c, "")}
                                 for c, col in json.loads(row["mapping"]).items()],
                    "notes": [{"level": level, "message": m} for level, m in store.notes(session_id)],
                    "dbc": [p.name for p in store.dbc_files(session_id)],
                    "engineering": store.engineering(session_id),
                    "laps": [{"lap": _clean(r.lap), "time": _clean(r.lap_time_s), "status": r.status,
                              "stint": int(r.stint)} for r in laps.itertuples()]}
        return self._do(read)

    def state(self) -> dict:
        databases = []
        for p in self.dbc_files():
            try:
                db = read_dbc(p)
                databases.append({"name": p.name, "messages": len(db.messages), "signals": db.signal_count})
            except ValueError as e:
                databases.append({"name": p.name, "error": str(e)})
        waiting = sorted(p.name for p in self.inbox.iterdir() if p.suffix.lower() == ".ldx")
        return {"folder": str(self.path.resolve()), "sessions": self.sessions(), "dbc": databases,
                "waiting": waiting, "formats": supported_formats(), "extensions": supported_extensions()}

    # ── write ─────────────────────────────────────────────────────────────────

    def add_file(self, name: str, data: bytes) -> dict:
        """Take in one file. A .dbc joins the workspace's databases, an .ldx
        waits for its .ld, anything else is imported as a session. Returns
        what happened: {"result": "session" | "dbc" | "kept" | "error", ...}."""
        name = Path(name.replace("\\", "/")).name
        if not name or name.startswith("."):
            return {"result": "error", "file": name, "message": "The file has no usable name."}
        suffix = Path(name).suffix.lower()
        with self._lock:
            if suffix == ".dbc":
                target = self.dbc_dir / name
                target.write_bytes(data)
                try:
                    db = read_dbc(target)
                except ValueError as e:
                    target.unlink()
                    return {"result": "error", "file": name, "message": str(e)}
                return {"result": "dbc", "file": name, "message": f"CAN database with {len(db.messages)} messages and "
                        f"{db.signal_count} signals. CAN logs added from now on are decoded with it."}
            target = self.inbox / name
            target.write_bytes(data)
            if suffix == ".ldx":
                ld = next((p for p in self.inbox.iterdir() if p.suffix.lower() == ".ld" and p.stem == target.stem), None)
                if ld is None:
                    return {"result": "kept", "file": name, "message": "Lap markers of a MoTeC log. Kept; add the "
                            f"{target.stem}.ld file and they are used with it."}
                target = ld
            return self._import(target)

    def _import(self, path: Path) -> dict:
        ldx = path.with_suffix(".ldx")
        try:
            sid = self._do(lambda store: store.import_file(path, dbc=self.dbc_files() or None))
        except (ValueError, KeyError, OSError) as e:
            message = str(e)
            if "DBC" in message and not self.dbc_files():
                message += " In the workspace: add the .dbc file first, then add this log again."
            path.unlink(missing_ok=True)
            return {"result": "error", "file": path.name, "message": message}
        path.unlink(missing_ok=True)
        if path.suffix.lower() == ".ld":
            ldx.unlink(missing_ok=True)
        summary = next(s for s in self.sessions() if s["id"] == sid)
        return {"result": "session", "file": path.name, "session": summary,
                "message": f"Read as {summary['format']}: {summary['laps']} lap(s), {summary['warnings']} warning(s)."}

    def report(self, session_id: int) -> Path:
        """The session's report page, built the first time it is asked for."""
        from ..report import write_report
        def build(store):
            target = store._folder(session_id) / "report.html"
            if not target.exists():
                write_report(store.reload_session(session_id), target)
            return target
        with self._lock:
            return self._do(build)

    def delete_session(self, session_id: int) -> None:
        with self._lock:
            self._do(lambda store: store.delete_session(session_id))

    def delete_dbc(self, name: str) -> None:
        target = self.dbc_dir / Path(name).name
        if target.suffix.lower() == ".dbc" and target.exists():
            target.unlink()


# ── web ───────────────────────────────────────────────────────────────────────

def _handler(ws: Workspace):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Veltrix"

        def log_message(self, *args):
            pass

        def _send(self, body, kind="application/json", status=200):
            if not isinstance(body, bytes):
                body = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", kind + ("; charset=utf-8" if kind.startswith("text") or "json" in kind else ""))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _local(self) -> bool:
            """Only this machine's own page may talk to the server: the Host
            must be the loopback address the server listens on."""
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            return host in ("127.0.0.1", "localhost")

        def do_GET(self):
            if not self._local():
                return self._send({"error": "forbidden"}, status=403)
            path = urlparse(self.path).path
            try:
                if path == "/":
                    return self._send(_PAGE.read_bytes(), "text/html")
                if path == "/api/state":
                    return self._send(ws.state())
                m = re.fullmatch(r"/api/sessions/(\d+)", path)
                if m:
                    return self._send(ws.session(int(m.group(1))))
                m = re.fullmatch(r"/report/(\d+)", path)
                if m:
                    return self._send(ws.report(int(m.group(1))).read_bytes(), "text/html")
            except (KeyError, FileNotFoundError, ValueError) as e:
                return self._send({"error": str(e)}, status=404)
            self._send({"error": "not found"}, status=404)

        def do_POST(self):
            # the custom header cannot be sent by another site's page without a
            # cross-origin permission this server never gives
            url = urlparse(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            refused = None
            if not self._local() or self.headers.get("X-Veltrix") != "1":
                refused = ({"error": "forbidden"}, 403)
            elif length > MAX_UPLOAD:
                refused = ({"error": "file too large"}, 413)
            if refused:
                left = length                      # take the upload off the wire without keeping it
                while left > 0:
                    chunk = self.rfile.read(min(left, 1 << 20))
                    if not chunk:
                        break
                    left -= len(chunk)
                return self._send(refused[0], status=refused[1])
            body = self.rfile.read(length)
            if url.path == "/api/files":
                name = unquote(parse_qs(url.query).get("name", [""])[0])
                return self._send(ws.add_file(name, body))
            m = re.fullmatch(r"/api/sessions/(\d+)/delete", url.path)
            if m:
                try:
                    ws.delete_session(int(m.group(1)))
                except KeyError as e:
                    return self._send({"error": str(e)}, status=404)
                return self._send({"ok": True})
            m = re.fullmatch(r"/api/dbc/([^/]+)/delete", url.path)
            if m:
                ws.delete_dbc(unquote(m.group(1)))
                return self._send({"ok": True})
            self._send({"error": "not found"}, status=404)

    return Handler


def make_server(folder: str | Path, port: int = 8765) -> ThreadingHTTPServer:
    """The workspace server, bound to this machine only. Port 0 picks a free port."""
    return ThreadingHTTPServer(("127.0.0.1", port), _handler(Workspace(folder)))


def serve(folder: str | Path, port: int = 8765, open_browser: bool = True) -> None:
    try:
        server = make_server(folder, port)
    except OSError:
        server = make_server(folder, 0)          # the port is taken: use any free one
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"Veltrix workspace: {Path(folder).resolve()}")
    print(f"Open {url}   (Ctrl-C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.server_close()


__all__ = ["Workspace", "make_server", "serve"]
