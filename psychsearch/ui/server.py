"""The local web server behind the page, and the psychsearch-ui command."""

from __future__ import annotations

import argparse
import json
import re
import secrets
import signal
import sys
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .. import home
from ..backends import BACKENDS, SearchOptions
from ..backends.base import MAX_SAMPLE
from ..cli import CACHE_DIR, _load_env, _load_known
from ..combos import load_blocks
from ..http import Http
from ..query import FIELDS, QueryError
from .log import SAVED_FILE, Log, load_saved, read_log, session_summary
from .session import Session
from .strategy import UIError, state_from_blocks

PAGE = Path(__file__).with_name("page.html")
PLOTLY_JS = "/plotly.min.js"  # served from the installed plotly, so diagrams work offline



_plotly_cache: list[bytes] = []


def _plotly_js() -> bytes:
    if not _plotly_cache:
        from plotly.offline import get_plotlyjs
        _plotly_cache.append(get_plotlyjs().encode())
    return _plotly_cache[0]


# ------------------------------------------------------------------- http


class Handler(BaseHTTPRequestHandler):
    server: Server

    def log_message(self, format, *args):  # the session log records what matters
        pass

    def _send(self, status: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, value, status: int = 200) -> None:
        self._send(status, json.dumps(value, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _allowed(self, api: bool) -> bool:
        # Only this machine's page: a fixed Host stops DNS rebinding, and the token (a custom header,
        # so other sites cannot send it without a CORS preflight this server never answers) stops CSRF.
        if self.headers.get("Host") not in self.server.hosts:
            self._send(403, b"forbidden", "text/plain")
            return False
        if api and not secrets.compare_digest(self.headers.get("X-Token", ""), self.server.token):
            self._send(403, b"forbidden", "text/plain")
            return False
        return True

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        if url.path == "/":
            if self._allowed(api=False):
                page = PAGE.read_text(encoding="utf-8").replace("__TOKEN__", self.server.token)
                self._send(200, page.encode(), "text/html; charset=utf-8")
            return
        if url.path == PLOTLY_JS:
            if self._allowed(api=False):
                self._send(200, _plotly_js(), "text/javascript; charset=utf-8")
            return
        if match := re.fullmatch(r"/hasse/(\d+)", url.path):
            self._hasse(int(match.group(1)), parse_qs(url.query))
            return
        if not self._allowed(api=True):
            return
        session, query = self.server.session, parse_qs(url.query)
        try:
            if url.path == "/api/state":
                self._json(session.view())
            elif match := re.fullmatch(r"/api/entry/(\d+)", url.path):
                self._json(session.log.get(int(match.group(1))))
            elif url.path == "/api/terms-file":
                self._json(session.save_preview())
            elif url.path == "/api/sessions":
                files = sorted(session.log.path.parent.glob("*.jsonl"), reverse=True)
                self._json([{"file": f.name, "current": f == session.log.path, **session_summary(f)} for f in files])
            elif url.path == "/api/log":
                name = query.get("session", [""])[0]
                if not name or name == session.log.path.name:
                    self._json(session.log.entries)
                else:
                    path = session.log.path.parent / Path(name).name
                    if path.suffix != ".jsonl" or not path.is_file():
                        raise UIError(f"no log {name}")
                    self._json(read_log(path))
            else:
                self._send(404, b"not found", "text/plain")
        except UIError as exc:
            self._json({"error": str(exc)}, 400)

    def _hasse(self, n: int, query: dict) -> None:
        # Opened in its own tab, which cannot send the X-Token header, so the token comes in the URL.
        if not self._allowed(api=False):
            return
        if not secrets.compare_digest(query.get("token", [""])[0], self.server.token):
            self._send(403, b"forbidden", "text/plain")
            return
        session = self.server.session
        backend = query.get("backend", [""])[0] or session.backends[0].name
        theme = query.get("theme", ["light"])[0]
        job = session.job
        if job.get("running") and job.get("kind") == "hasse" and job.get("n") == n:
            self._send(200, session.hasse_wait_html(n, theme).encode(), "text/html; charset=utf-8")
            return
        try:
            page = session.hasse_html(n, backend, theme, PLOTLY_JS)
        except (UIError, QueryError) as exc:
            self._send(400, f"Cannot draw a Hasse diagram of #{n}: {exc}".encode(), "text/plain; charset=utf-8")
            return
        self._send(200, page.encode(), "text/html; charset=utf-8")

    def do_POST(self) -> None:
        if not self._allowed(api=True):
            return
        session = self.server.session
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            path = urlsplit(self.path).path
            if path == "/api/action":
                session.act(body)
            elif path == "/api/run":
                session.start("run")
            elif path == "/api/analyze":
                session.start("analyze", body.get("block"))
            elif path == "/api/hasse":
                session.start("hasse", n=body.get("n"))
            elif path == "/api/load":
                session.load(str(body.get("session", "")))
            else:
                self._send(404, b"not found", "text/plain")
                return
            self._json(session.view())
        except UIError as exc:
            self._json({"error": str(exc)}, 400)
        except (ValueError, json.JSONDecodeError) as exc:
            self._json({"error": f"bad request: {exc}"}, 400)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int, session: Session):
        super().__init__(("127.0.0.1", port), Handler)
        self.session = session
        self.token = secrets.token_urlsafe(24)
        self.hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}


# ------------------------------------------------------------------- main


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="psychsearch-ui",
        description="A local page for switching search strings on and off, adding and removing them, and seeing "
        "what each change does to the results. Every action and result is logged.",
    )
    parser.add_argument("terms", nargs="?", type=Path, help="terms file to start from ('?' candidates start switched off)")
    parser.add_argument("-b", "--backends", default="ebscohost", help="comma-separated; the first one is shown first (default: %(default)s)")
    parser.add_argument("--field", choices=FIELDS, default="tiab", help="where to search (default: %(default)s)")
    parser.add_argument("--sample", type=int, default=10, metavar="N", help=f"records per query, 0-{MAX_SAMPLE} (default: %(default)s)")
    parser.add_argument("--sort", choices=("relevance", "date"), default="relevance", help="which records the sample takes (default: %(default)s)")
    parser.add_argument("--years", metavar="FROM-TO", help="restrict publication years, e.g. 2010-2025")
    parser.add_argument("--known", type=Path, metavar="FILE", help="DOIs/PMIDs of known relevant papers")
    parser.add_argument("--out", type=Path, default=Path("results"), help="the log goes in OUT/ui/ (default: %(default)s)")
    parser.add_argument("--port", type=int, default=8765, help="local port (default: %(default)s; the next free one is used if taken)")
    parser.add_argument("--no-browser", action="store_true", help="do not open the page in your browser")
    parser.add_argument("--no-cache", action="store_true", help="ignore and do not write the 7-day response cache")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    args.terms = home.find(args.terms) if args.terms else None
    args.known = home.find(args.known) if args.known else None
    folder = home.enter()
    _load_env(Path(".env"))
    names = [n.strip() for n in args.backends.split(",") if n.strip()]
    if unknown := [n for n in names if n not in BACKENDS]:
        parser.error(f"unknown backend(s) {', '.join(unknown)}; choose from {', '.join(BACKENDS)}")
    if not names:
        parser.error("choose at least one backend")
    if not 0 <= args.sample <= MAX_SAMPLE:
        parser.error(f"--sample must be between 0 and {MAX_SAMPLE}")
    years = None
    if args.years:
        if not (match := re.fullmatch(r"(\d{4})-(\d{4})", args.years)):
            parser.error("--years must look like 2010-2025")
        years = (int(match.group(1)), int(match.group(2)))
    try:
        state = state_from_blocks(load_blocks(args.terms)) if args.terms else []
        known_ids = _load_known(args.known) if args.known else []
    except (QueryError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    opts = SearchOptions(field=args.field, sample=args.sample, sort=args.sort, years=years)
    http = Http(None if args.no_cache else CACHE_DIR)
    backends = [BACKENDS[name](http) for name in names]
    pool = [BACKENDS[name](http) for name in ("ebscohost", "pubmed", "eric") if name not in names]
    started = datetime.now()
    settings = {
        "terms file": str(args.terms) if args.terms else "(none)",
        "backends": [b.label for b in backends],
        "search field": args.field, "years": args.years or "any",
        "sample": f"{args.sample} per query, {args.sort} order",
        "known items supplied": len(known_ids), "cache": "off" if args.no_cache else str(CACHE_DIR),
    }
    try:
        saved_path = args.out / SAVED_FILE
        load_saved(saved_path)  # checked before the log is started, so a bad file leaves no empty session behind
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    log = Log(args.out / "ui" / f"{started:%Y%m%d-%H%M%S}.jsonl")
    session = Session(state, backends, opts, known_ids, log, args.terms, settings, pool, saved_path)

    server = None
    for port in range(args.port, args.port + 10):
        try:
            server = Server(port, session)
            break
        except OSError:
            continue
    if server is None:
        print(f"error: ports {args.port}-{args.port + 9} are all in use; choose another with --port", file=sys.stderr)
        return 2
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"psychsearch-ui: {url}  (Ctrl+C to stop)\nFolder: {folder}\nLog: {log.path}", flush=True)
    if not args.no_browser:
        webbrowser.open(url)

    def stop(*_):
        # A plain `kill` ends the session as tidily as Ctrl+C. Later signals (uv forwards its own) are
        # ignored so they cannot cut the shutdown short.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        session.close()
    return 0
