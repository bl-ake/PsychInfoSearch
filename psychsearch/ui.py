"""A local web page for changing a search strategy one string at a time and seeing what each change does.

    uv run psychsearch-ui examples/terms.txt -b ebscohost

Switch strings and blocks on and off, add and remove them, then run. Each run searches the full
strategy and each block, and, when a previous run exists, the records the change added
(``new NOT old``) and the ones it lost (``old NOT new``). Both are real searches with sample records.
A side that containment shows must be empty (adding an OR string cannot lose records) is reported
as 0 without searching. "Analyze" on a block shows what each of its strings contributes and what
each switched-off string would add (the same queries as ``--refine``). "Hasse diagram" on a run or
an analysis opens its queries, blocks, strings and every combination of blocks ordered by containment
(see ``hasse.py``) in a new tab, first searching any of those not yet searched, so every node has a count.

The star on a sampled record saves it as a relevant article, in ``results/relevant-articles.json``, which
every session shares. From then on each run and analysis also asks the database which of the saved
articles each query retrieves (by record id, so only in the database each was saved from), and shows
the proportion. Containment saves searches: an article a narrower query finds, a broader one finds too.

Every action and every result is appended to ``results/ui/<timestamp>.jsonl``. The page shows the
recent entries and, on request, the full log of this or an earlier session. Any earlier run can be
restored or used as the comparison point. An earlier session can be loaded to carry on where it left
off: its log is replayed (see ``replay``) and new entries are added to it.

The page is served on 127.0.0.1 only. Searches run one at a time through the same backends, cache
and limits as the command line. For ebscohost a session of this page counts as one run: one window,
searches 5 s apart, at most EBSCOHOST_MAX_SEARCHES in all, and a stop on anything that is not a
results page (restart the page to continue; finished searches are cached).
"""

from __future__ import annotations

import argparse
import copy
import html
import json
import math
import os
import re
import secrets
import signal
import sys
import threading
import traceback
import webbrowser
from datetime import datetime
from itertools import combinations
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import home
from .backends import BACKENDS, Backend, BackendError, Record, SearchOptions
from .backends.base import MAX_SAMPLE
from .cli import CACHE_DIR, _load_env, _load_known
from .combos import Block, Query, blocks_from_lines, load_blocks, refine, with_candidates
from .http import Http, HttpError
from .query import FIELDS, And, Node, Not, Or, QueryError, all_of, parse, to_generic
from .redundancy import covers, find_redundant
from .runner import KnownSet, Outcome, resolve_known, run, score_records

PAGE = Path(__file__).with_name("ui.html")
PLOTLY_JS = "/plotly.min.js"  # served from the installed plotly, so diagrams work offline
RECENT = 12  # log entries shown beside the results
LINE = 1000  # synthetic line numbers for find_redundant: block index * LINE + string index
# searches of a run or analysis that its Hasse diagram shows, and the diagram's name for each
HASSE_ROLES = {"full": "full strategy", "block": "blocks", "minus": "full minus a string", "adds": "added by a string"}
MAX_HASSE_COMBINATIONS = 60  # block combinations a diagram adds; with 7+ blocks the larger ones are left out
JOB_NAMES = {"run": "Run", "analyze": "Analyze", "hasse": "Searching for the Hasse diagram"}
SAVED_FILE = "relevant-articles.json"  # beside the ui/ log folder, shared by every session


class UIError(Exception):
    """A request the page cannot carry out; the message is shown to the user."""


# ------------------------------------------------------------------ state

# The working strategy is plain JSON so it can be logged, snapshotted and restored as is:
# [{"name": str, "on": bool, "terms": [{"text": str, "on": bool}]}]


def state_from_blocks(blocks: list[Block]) -> list[dict]:
    """The working state for a terms file: '?' candidates start switched off."""
    state = []
    for number, block in enumerate(blocks, 1):
        terms: list[dict] = []
        for _, node, candidate in block.entries:
            text = to_generic(node)
            if not any(t["text"] == text for t in terms):
                terms.append({"text": text, "on": not candidate})
        state.append({"name": block.name or f"block {number}", "on": True, "terms": terms})
    return state


def state_from_string(text: str) -> list[dict]:
    """The working state for a pasted search string such as ``(a OR b) AND (c OR d)``: each part
    joined by AND becomes a block, named block 1, block 2 and so on, and each part of it joined by OR
    becomes one of its strings, switched on."""
    no_not = UIError("NOT cannot be loaded, since a block's strings are ORed and blocks are ANDed; "
                     "take the NOT part out of the string and load it again")
    if not text.strip():
        raise UIError("paste a search string first")
    try:
        node = parse(text)
    except QueryError as exc:
        if re.search(r"\bNOT\b", re.sub(r'"[^"]*"', "", text)):  # "a AND NOT b" fails to parse
            raise no_not from None
        raise UIError(str(exc)) from None
    state = []
    for number, part in enumerate(node.parts if isinstance(node, And) else (node,), 1):
        if isinstance(part, Not):  # "a NOT b"
            raise no_not
        terms: list[dict] = []
        for term in part.parts if isinstance(part, Or) else (part,):
            text = _clean_term(to_generic(term))
            if not any(t["text"] == text for t in terms):
                terms.append({"text": text, "on": True})
        state.append({"name": f"block {number}", "on": True, "terms": terms})
    return state


def _blocks(state: list[dict], include_off: bool = False) -> list[Block]:
    """Blocks with at least one string switched on; switched-off strings become candidates.
    Blocks that are switched off are left out unless `include_off`."""
    blocks = []
    for bi, b in enumerate(state):
        entries = [(bi * LINE + ti, parse(t["text"]), not t["on"]) for ti, t in enumerate(b["terms"])]
        terms = list(dict.fromkeys(n for _, n, off in entries if not off))
        if terms and (b["on"] or include_off):
            candidates = list(dict.fromkeys(n for _, n, off in entries if off and n not in terms))
            blocks.append(Block(b["name"], terms, candidates, entries))
    return blocks


def full_node(state: list[dict]) -> Node | None:
    blocks = _blocks(state)
    return all_of(b.node for b in blocks) if blocks else None


def strategy_text(state: list[dict]) -> str:
    node = full_node(state)
    return to_generic(node) if node is not None else ""


def terms_file_text(state: list[dict], note: str) -> str:
    """The state as a terms file. Switched-off strings are '?' candidates; a block with nothing
    switched on is commented out, since a block needs at least one string."""
    lines = [f"# {note}", "# Strings marked '?' are switched off; commented-out blocks were switched off.", ""]
    for b in state:
        if b["on"] and any(t["on"] for t in b["terms"]):
            lines.append(f"[{b['name']}]")
            lines += [t["text"] if t["on"] else f"? {t['text']}" for t in b["terms"]]
        else:
            lines.append(f"# [{b['name']}] (switched off)")
            lines += [f"# {t['text']}" for t in b["terms"]]
        lines.append("")
    return "\n".join(lines)


def _clean_term(text: str) -> str:
    text = " ".join(text.split())
    if not text:
        raise UIError("type a search string first")
    if "#" in text:
        raise UIError("'#' starts a comment in the terms file, so it cannot be part of a string")
    if text.startswith("?") or re.fullmatch(r"\[.*\]", text):
        raise UIError("a string cannot start with '?' or be wrapped in [ ] (those mark candidates and block names in the terms file)")
    try:
        return to_generic(parse(text))
    except QueryError as exc:
        raise UIError(str(exc)) from None


def _clean_name(name: str, state: list[dict]) -> str:
    name = " ".join(name.split())
    if not name or re.search(r"[\[\]#]", name):
        raise UIError("a block name cannot be empty or contain [, ] or #")
    if any(b["name"].lower() == name.lower() for b in state):
        raise UIError(f"there is already a block [{name}]")
    return name


def _number(value: int | None) -> str:
    return "-" if value is None else f"{value:,}"


def _record(record: Record) -> dict:
    return {
        "id": record.id, "title": record.title, "authors": record.authors[:6], "more_authors": max(0, len(record.authors) - 6),
        "year": record.year, "journal": record.journal, "doi": record.doi, "url": record.url,
        "abstract": record.abstract, "subjects": record.subjects[:10], "pub_types": record.pub_types,
        "cited_by": record.cited_by, "blocks_matched": record.extra.get("blocks_matched"),
    }


# -------------------------------------------------------------------- log


class Log:
    """Append-only JSON lines; every entry is numbered like a database search history."""

    def __init__(self, path: Path, entries: list[dict] | None = None):
        self.path = path
        self.entries: list[dict] = entries or []
        self.lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    @classmethod
    def resume(cls, path: Path) -> "Log":
        """An earlier session's log, to add to; numbering carries on from its last entry."""
        data = path.read_bytes()
        if data and not data.endswith(b"\n"):  # a last line cut short by a crash: start a fresh line
            with path.open("ab") as out:
                out.write(b"\n")
        return cls(path, read_log(path))

    def add(self, kind: str, summary: str, **data) -> dict:
        with self.lock:
            n = self.entries[-1]["n"] + 1 if self.entries else 1
            entry = {"n": n, "time": datetime.now().isoformat(timespec="seconds"), "kind": kind, "summary": summary, **data}
            self.entries.append(entry)
            with self.path.open("a", encoding="utf-8") as out:
                out.write(json.dumps(entry, ensure_ascii=False) + "\n")
            return entry

    def get(self, n: int) -> dict:
        if 1 <= n <= len(self.entries) and self.entries[n - 1]["n"] == n:
            return self.entries[n - 1]
        for entry in self.entries:  # a resumed log that lost a line
            if entry["n"] == n:
                return entry
        raise UIError(f"no log entry #{n}")


def read_log(path: Path) -> list[dict]:
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            pass  # a line cut short by a crash
    return entries


def load_saved(path: Path) -> list[dict]:
    """The saved relevant articles, oldest first. A file that is not readable as one is an error rather
    than an empty list, so saving never overwrites it."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read the saved articles in {path}: {exc}") from None
    if not isinstance(data, list) or not all(isinstance(a, dict) and a.get("database") and a.get("id") for a in data):
        raise ValueError(f"{path} is not a list of saved articles")
    return data


def write_saved(path: Path, saved: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(saved, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    os.replace(temp, path)  # all or nothing, so a crash cannot leave half a file


def _options(o: SearchOptions) -> dict:
    return {"field": o.field, "sample": o.sample, "sort": o.sort, "years": list(o.years) if o.years else None}


def _logged_options(entry: dict) -> SearchOptions | None:
    """The search settings a "session start" or "resume" entry was made with. Older logs only have
    the settings as shown on the page, so those are read back from that text."""
    if o := entry.get("options"):
        return SearchOptions(field=o["field"], sample=o["sample"], sort=o["sort"], years=tuple(o["years"]) if o["years"] else None)
    s = entry.get("settings") or {}
    sample = re.fullmatch(r"(\d+) per query, (\w+) order", str(s.get("sample", "")))
    years = re.fullmatch(r"(\d{4})-(\d{4})", str(s.get("years", "")))
    if s.get("search field") not in FIELDS or not sample or not (years or s.get("years") == "any"):
        return None
    return SearchOptions(field=s["search field"], sample=int(sample.group(1)), sort=sample.group(2),
                         years=(int(years.group(1)), int(years.group(2))) if years else None)


def replay(entries: list[dict]) -> dict:
    """The page as a session's log left it: the working strategy, the runs it can compare with, the
    run it compares with, each block's latest analysis, and the databases and search settings.
    Edits are logged by block name and string text, both unique within a strategy, so they replay exactly."""
    labels = {cls.label: name for name, cls in BACKENDS.items()}
    state: list[dict] = []
    runs: dict[int, Node] = {}
    analysis: dict[str, dict] = {}
    pinned, databases, options, settings = None, [], None, {}
    for e in entries:
        kind = e.get("kind")
        block = next((b for b in state if b["name"] == e.get("block")), None)
        term = next((t for t in block["terms"] if t["text"] == e.get("term")), None) if block else None
        if kind in ("session start", "resume"):
            state = copy.deepcopy(e["state"])
            names = e.get("databases") or [labels[x] for x in e.get("settings", {}).get("backends", []) if x in labels]
            if names != databases:
                runs, analysis, pinned, databases = {}, {}, None, names
            options = _logged_options(e) or options
            settings = e.get("settings") or settings
        elif kind == "switch database" and [e["database"]] != databases:
            runs, analysis, pinned, databases = {}, {}, None, [e["database"]]
        elif kind == "add block":
            state.append({"name": e["block"], "on": True, "terms": []})
        elif kind == "remove block" and block:
            state.remove(block)
            analysis.pop(block["name"], None)
        elif kind == "toggle block" and block:
            block["on"] = e["on"]
        elif kind == "add term" and block and not term:
            block["terms"].append({"text": e["term"], "on": True})
        elif kind == "remove term" and term:
            block["terms"].remove(term)
        elif kind == "toggle term" and term:
            term["on"] = e["on"]
        elif kind == "restore":
            state = copy.deepcopy(e["state"])
        elif kind == "load string":
            state, analysis = copy.deepcopy(e["state"]), {}
        elif kind == "compare with":
            pinned = e["run"]
        elif kind == "run":
            try:
                if (node := full_node(e["state"])) is not None:
                    runs[e["n"]] = node
            except QueryError:
                pass
        elif kind == "analyze":
            analysis[e["block"]] = {"n": e["n"], "strategy": e["strategy"], "terms": e["effects"]}
    return {"state": state, "runs": runs, "pinned": pinned if pinned in runs else None, "analysis": analysis,
            "databases": databases, "options": options, "settings": settings}


_summaries: dict[Path, tuple[tuple, dict]] = {}


def session_summary(path: Path) -> dict:
    """What the session list shows of a log; worked out again only when the file changes."""
    stat = path.stat()
    key = (stat.st_mtime_ns, stat.st_size)
    if path not in _summaries or _summaries[path][0] != key:
        entries = read_log(path)
        _summaries[path] = (key, {
            "started": entries[0].get("time") if entries else None,
            "runs": sum(e.get("kind") == "run" for e in entries),
            "strategy": next((e["strategy"] for e in reversed(entries) if "strategy" in e), ""),
            "loadable": bool(entries) and entries[0].get("kind") == "session start",
        })
    return _summaries[path][1]


# ---------------------------------------------------------------- session


class Session:
    def __init__(self, state: list[dict], backends: list[Backend], opts: SearchOptions, known_ids: list[str],
                 log: Log, terms_path: Path | None, settings: dict, pool: list[Backend] | None = None,
                 saved_path: Path | None = None):
        self.state = state
        self.backends = backends
        # databases the page's dropdown can switch to (one at a time); the starting ones are always included
        found = {b.name: b for b in [*backends, *(pool or [])]}
        order = ["ebscohost", "pubmed", "eric"]
        self.pool = {n: found[n] for n in sorted(found, key=lambda n: order.index(n) if n in order else len(order))}
        self.opts = opts
        self.known_ids = known_ids
        self.known: dict[str, KnownSet] = {}
        self.ready = False  # backends checked (window open, logged in) and known items resolved
        self.log = log
        self.terms_path = terms_path
        self.lock = threading.RLock()
        self.memo: dict[Node, Outcome] = {}  # finished searches in this session, by query
        self.runs: dict[int, Node] = {}  # run log number -> its full strategy
        self.pinned: int | None = None  # run chosen as the comparison point; None means the latest run
        self.analysis: dict[str, dict] = {}  # block name -> latest analysis
        self.hasse_extra: dict[int, list] = {}  # run/analysis log number -> diagram nodes it did not search
        self.saved_path = saved_path or log.path.parent.parent / SAVED_FILE
        self.saved: list[dict] = load_saved(self.saved_path)
        # (backend, query) -> the saved ids checked against it and those it retrieves; kept while `memo` is
        self.saved_hits: dict[tuple[str, Node], dict] = {}
        self.job: dict = {"running": False}
        self.settings = settings
        log.add("session start", f"Session started with {len(state)} block(s), searching {', '.join(b.label for b in backends)}",
                settings=settings, state=copy.deepcopy(state), strategy=strategy_text(state),
                databases=[b.name for b in backends], options=_options(opts))

    # ---------------------------------------------------------- reading

    def latest_run(self) -> int | None:
        return max(self.runs) if self.runs else None

    def baseline(self) -> int | None:
        return self.pinned if self.pinned is not None else self.latest_run()

    def plan(self, state: list[dict], base_n: int | None) -> list[dict]:
        """What a run of `state` searches: the full strategy, each block, and the change from run `base_n`."""
        blocks = _blocks(state)
        if not blocks:
            return []
        full = all_of(b.node for b in blocks)
        items = [{"role": "full", "query": Query("FULL: " + " AND ".join(b.label for b in blocks), "full", full)}]
        if len(blocks) > 1:
            items += [{"role": "block", "block": b.name, "query": Query(b.label, "block", b.node)} for b in blocks]
        if base_n is not None and self.runs[base_n] != full:
            base = self.runs[base_n]
            items.append({"role": "added", "query": Query(f"added since #{base_n}", "added", all_of([full, Not(base)])),
                          "proved_empty": covers(base, full)})  # the new strategy is inside the old one
            items.append({"role": "lost", "query": Query(f"lost since #{base_n}", "lost", all_of([base, Not(full)])),
                          "proved_empty": covers(full, base)})
        return items

    def _cost(self, queries: list[Query]) -> int:
        return len({q.node for q in queries} - set(self.memo)) * len(self.backends)

    def _saved_ids(self, backend: str) -> dict[str, str]:
        """Saved articles from `backend`, as {record id: title}."""
        with self.lock:
            return {a["id"]: a.get("title", "") for a in self.saved if a["database"] == backend}

    def _saved_cost(self, queries: list[Query]) -> int:
        """At most how many searches checking the saved articles against `queries` takes. Fewer are
        usually needed: queries with no hits need none, and containment answers many."""
        total = 0
        for backend in self.backends:
            ids = set(self._saved_ids(backend.name))
            for node in {q.node for q in queries} if ids else ():
                hits = self.saved_hits.get((backend.name, node))
                todo = len(ids - hits["checked"]) if hits else len(ids)
                done = self.memo.get(node)
                if todo and not (done and done.by_backend[backend.name].result.count == 0):
                    total += math.ceil(todo / (backend.known_chunk or todo))
        return total

    def analysis_queries(self, state: list[dict], name: str) -> list[Query]:
        blocks = _blocks(state)
        if not any(b.name == name for b in blocks):
            raise UIError(f"[{name}] has no strings switched on, so there is nothing to analyze")
        return refine(blocks, name)

    def view(self) -> dict:
        """Everything the page needs, except run results (fetched by log number)."""
        with self.lock:
            state = copy.deepcopy(self.state)
            error, sent, pending, redundant = "", {}, {"searches": 0, "saved_checks": 0, "proved_empty": 0}, {}
            try:
                node = full_node(state)
                if node is not None:
                    sent = {b.name: b.render(node, self.opts) for b in self.backends}
                plan = self.plan(state, self.baseline())
                searched = [i["query"] for i in plan if not i.get("proved_empty")]
                pending = {"searches": self._cost(searched), "saved_checks": self._saved_cost(searched),
                           "proved_empty": sum(bool(i.get("proved_empty")) for i in plan)}
                for r in find_redundant(_blocks(state, include_off=True)):
                    redundant[(r.line // LINE, r.line % LINE)] = r.reason
            except QueryError as exc:
                error = str(exc)
            current = strategy_text(state)
            earlier = self._earlier()
            hasse_costs = {}  # searches each diagram button would run first
            for e in self.log.entries:
                if e["kind"] in ("run", "analyze"):
                    try:
                        hasse_costs[e["n"]] = len(self.hasse_missing(e["n"], earlier)) * len(self.backends)
                    except (UIError, QueryError):
                        pass
            blocks = []
            for bi, b in enumerate(state):
                analysis = self.analysis.get(b["name"])
                stale = bool(analysis) and analysis["strategy"] != current
                try:
                    queries = self.analysis_queries(state, b["name"])
                    cost = self._cost(queries) + self._saved_cost(queries)
                except (UIError, QueryError):
                    cost = None
                terms = []
                for ti, t in enumerate(b["terms"]):
                    effect = analysis["terms"].get(t["text"]) if analysis else None
                    terms.append({**t, "redundant": redundant.get((bi, ti)), "effect": effect})
                blocks.append({"name": b["name"], "on": b["on"], "terms": terms, "analysis_cost": cost,
                               "analysis": {"n": analysis["n"], "stale": stale} if analysis else None})
            return {
                "blocks": blocks,
                "strategy": current,
                "sent": sent,
                "error": error,
                "pending": pending,
                "backends": [{"name": b.name, "label": b.label,
                              "remaining": b.max_searches - b.searches if hasattr(b, "max_searches") else None}
                             for b in self.backends],
                "databases": [{"name": b.name, "label": b.label} for b in self.pool.values()],
                "autorun": not any(b.name == "ebscohost" for b in self.backends),
                "runs": [{"n": n, "summary": self.log.get(n)["summary"], "time": self.log.get(n)["time"]} for n in sorted(self.runs)],
                "latest": self.latest_run(),
                "latest_result": next((e["n"] for e in reversed(self.log.entries) if e["kind"] in ("run", "analyze")), None),
                "settings": [[k, v] for k, v in self.settings.items() if k not in ("backends", "terms file")]
                            + [["terms file", self.settings["terms file"]]],
                "baseline": self.baseline(),
                "hasse_costs": hasse_costs,
                "pinned": self.pinned is not None,
                "recent": [_brief(e) for e in self.log.entries[-RECENT:]][::-1],
                "log_count": len(self.log.entries),
                "log_file": str(self.log.path),
                "job": dict(self.job),
                "save_path": str(self._default_save_path()),
                "saved": [{k: a.get(k) for k in ("database", "id", "title", "year", "journal", "url", "saved")}
                          | {"authors": (a.get("authors") or [])[:3]} for a in reversed(self.saved)],
                "saved_file": str(self.saved_path),
            }

    def _default_save_path(self) -> Path:
        if self.terms_path is None:
            return Path.cwd() / "terms-ui.txt"  # the psychsearch folder
        return self.terms_path.with_name(f"{self.terms_path.stem}-ui{self.terms_path.suffix or '.txt'}")

    # ---------------------------------------------------------- actions

    def act(self, action: dict) -> None:
        kind = action.get("type", "")
        with self.lock:
            try:
                summary, data = self._apply(kind, action)
            except UIError as exc:
                if str(exc).startswith("EXISTS:"):
                    raise  # the page asks before overwriting; nothing has happened yet
                self.log.add("rejected", f"Rejected {kind or 'action'}: {exc}", action=action, error=str(exc))
                raise
            self.log.add(kind, summary, **data, strategy=strategy_text(self.state))

    def _switch_database(self, name: str) -> tuple[str, dict]:
        """Search one other database from now on. Results from the old one stay in the log but no longer count
        as runs to compare against."""
        if name not in self.pool:
            raise UIError(f"unknown database {name!r}")
        if self.job.get("running"):
            raise UIError("a search is running; wait for it to finish before switching database")
        backend = self.pool[name]
        if [b.name for b in self.backends] != [name]:
            self.backends = [backend]
            self.memo, self.runs, self.analysis, self.known, self.saved_hits = {}, {}, {}, {}, {}
            self.pinned, self.ready = None, False
        self.settings["backends"] = [backend.label]
        return f"Switched database to {backend.label}", {"database": name}

    def _block(self, action: dict) -> dict:
        index = action.get("block")
        if not isinstance(index, int) or not 0 <= index < len(self.state):
            raise UIError("that block no longer exists; reload the page")
        return self.state[index]

    def _term(self, block: dict, action: dict) -> dict:
        index = action.get("term")
        if not isinstance(index, int) or not 0 <= index < len(block["terms"]):
            raise UIError("that string no longer exists; reload the page")
        return block["terms"][index]

    def _apply(self, kind: str, action: dict) -> tuple[str, dict]:
        if kind == "switch database":
            return self._switch_database(str(action.get("name", "")))
        if kind == "add term":
            block = self._block(action)
            text = _clean_term(str(action.get("text", "")))
            if any(t["text"] == text for t in block["terms"]):
                raise UIError(f"{text} is already in [{block['name']}]")
            block["terms"].append({"text": text, "on": True})
            return f"+ {text} in [{block['name']}]", {"block": block["name"], "term": text}
        if kind == "remove term":
            block = self._block(action)
            term = self._term(block, action)
            block["terms"].remove(term)
            return f"- {term['text']} from [{block['name']}] (deleted)", {"block": block["name"], "term": term["text"], "was_on": term["on"]}
        if kind == "toggle term":
            block = self._block(action)
            term = self._term(block, action)
            term["on"] = not term["on"]
            sign, word = ("+", "switched on") if term["on"] else ("-", "switched off")
            return f"{sign} {term['text']} in [{block['name']}] ({word})", {"block": block["name"], "term": term["text"], "on": term["on"]}
        if kind == "toggle block":
            block = self._block(action)
            block["on"] = not block["on"]
            return f"Block [{block['name']}] switched {'on' if block['on'] else 'off'}", {"block": block["name"], "on": block["on"]}
        if kind == "add block":
            name = _clean_name(str(action.get("name", "")), self.state)
            self.state.append({"name": name, "on": True, "terms": []})
            return f"Added block [{name}]", {"block": name}
        if kind == "remove block":
            block = self._block(action)
            self.state.remove(block)
            self.analysis.pop(block["name"], None)
            return f"Deleted block [{block['name']}] ({len(block['terms'])} string(s))", {"block": block["name"], "removed": block}
        if kind == "restore":
            n = int(action.get("run", 0))
            if n not in self.runs:
                raise UIError(f"#{n} is not a run")
            self.state = copy.deepcopy(self.log.get(n)["state"])
            return f"Restored the strategy of run #{n}", {"run": n, "state": copy.deepcopy(self.state)}
        if kind == "load string":
            text = str(action.get("text", ""))
            self.state = state_from_string(text)
            self.analysis = {}  # "block 1" and so on are new blocks, whatever the old ones were called
            count = sum(len(b["terms"]) for b in self.state)
            return (f"Loaded a search string as {len(self.state)} block(s) with {count} string(s)",
                    {"text": text, "state": copy.deepcopy(self.state)})
        if kind == "compare with":
            n = action.get("run")
            if n is None:
                self.pinned = None
                return "Comparing with the latest run again", {"run": None}
            if int(n) not in self.runs:
                raise UIError(f"#{n} is not a run")
            self.pinned = int(n)
            return f"Comparing new runs with run #{n}", {"run": int(n)}
        if kind == "save":
            return self._save(str(action.get("path", "")).strip(), bool(action.get("overwrite")))
        if kind == "save article":
            return self._save_article(action)
        if kind == "unsave article":
            database, rid = str(action.get("database", "")), str(action.get("id", ""))
            article = next((a for a in self.saved if a["database"] == database and a["id"] == rid), None)
            if article is None:
                raise UIError("that article is not saved")
            self._write_saved([a for a in self.saved if a is not article])
            return (f"☆ Removed from relevant articles: {_short(article.get('title') or rid, 120)}",
                    {"database": database, "id": rid, "title": article.get("title", "")})
        raise UIError(f"unknown action {kind!r}")

    def _save_article(self, action: dict) -> tuple[str, dict]:
        """Save a record shown in log entry `n` as relevant. The record is taken from the log, not the
        page, so what is saved is what the database returned."""
        n, database, rid = action.get("n"), str(action.get("database", "")), str(action.get("id", ""))
        if not isinstance(n, int):
            raise UIError("say which result the article is from")
        entry = self.log.get(n)
        record = next((r for q in entry.get("queries") or entry.get("rows") or []
                       for r in (q["by"].get(database) or {}).get("records", []) if r["id"] == rid), None)
        if record is None:
            raise UIError(f"no record {rid} from {database} in #{n}")
        if any(a["database"] == database and a["id"] == rid for a in self.saved):
            raise UIError("that article is already saved")
        label = self.pool[database].label if database in self.pool else database
        article = {"database": database, **{k: v for k, v in record.items() if k != "blocks_matched"},
                   "saved": datetime.now().isoformat(timespec="seconds"), "session": self.log.path.name, "entry": n}
        self._write_saved([*self.saved, article])
        return (f"★ Saved as relevant ({label}): {_short(record.get('title') or rid, 120)}",
                {"database": database, "id": rid, "title": record.get("title", ""), "article": article})

    def _write_saved(self, saved: list[dict]) -> None:
        try:
            write_saved(self.saved_path, saved)
        except OSError as exc:
            raise UIError(f"could not write {self.saved_path}: {exc}") from None
        self.saved = saved

    def _save(self, path_text: str, overwrite: bool) -> tuple[str, dict]:
        if not path_text:
            raise UIError("give a file name to save to")
        path = Path(path_text).expanduser()
        if path.exists() and not overwrite:
            raise UIError(f"EXISTS:{path}")
        text = terms_file_text(self.state, f"Saved by psychsearch-ui on {datetime.now():%Y-%m-%d %H:%M}; log {self.log.path}")
        try:
            blocks_from_lines(text.splitlines())  # what is saved must load again
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        except (OSError, QueryError) as exc:
            raise UIError(f"could not save to {path}: {exc}") from None
        return f"Saved the strategy to {path}", {"path": str(path), "text": text}

    def load(self, name: str) -> None:
        """Carry on an earlier session: the page shows its strategy, runs, comparison point and analyses,
        searches its databases with its settings, and adds to its log, so its numbering carries on.
        This session's log stays as it is (or is removed if nothing happened in it yet)."""
        with self.lock:
            if self.job.get("running"):
                raise UIError("a search is running; wait for it to finish before loading another session")
            path = self.log.path.parent / Path(name).name
            if path == self.log.path:
                raise UIError("that is the session already open")
            if path.suffix != ".jsonl" or not path.is_file():
                raise UIError(f"no log {name}")
            log = Log.resume(path)
            if not log.entries or log.entries[0].get("kind") != "session start":
                raise UIError(f"{name} is not a psychsearch-ui session log")
            old = replay(log.entries)

            # its databases if this page can search them; otherwise the current ones, with a fresh comparison
            names = old["databases"]
            backends = [self.pool[n] for n in names] if names and all(n in self.pool for n in names) else self.backends
            note = ""
            if backends is self.backends and names != [b.name for b in backends]:
                old["runs"], old["analysis"], old["pinned"] = {}, {}, None
                missing = ", ".join(BACKENDS[n].label if n in BACKENDS else n for n in names if n not in self.pool)
                note = f" ({missing} cannot be searched from this page, so it searches {', '.join(b.label for b in backends)})"
            opts = old["options"] or self.opts
            same = [b.name for b in backends] == [b.name for b in self.backends]
            if not same:
                self.known, self.ready = {}, False
            if not same or opts != self.opts:
                self.memo, self.saved_hits = {}, {}  # finished searches of other databases or settings

            if [e["kind"] for e in self.log.entries] == ["session start"]:
                self.log.path.unlink(missing_ok=True)  # nothing happened here worth keeping
            else:
                self.log.add("load", f"Continued in session {path.name}", file=path.name)

            self.log, self.backends, self.opts = log, backends, opts
            self.state, self.runs, self.pinned, self.analysis = old["state"], old["runs"], old["pinned"], old["analysis"]
            self.hasse_extra = {}
            settings = old["settings"]
            if settings.get("terms file") not in (None, "(none)"):
                self.terms_path = Path(settings["terms file"])
            self.settings = {**self.settings, "terms file": settings.get("terms file", self.settings["terms file"]),
                             "backends": [b.label for b in backends], "search field": opts.field,
                             "years": f"{opts.years[0]}-{opts.years[1]}" if opts.years else "any",
                             "sample": f"{opts.sample} per query, {opts.sort} order"}
            log.add("resume", f"Session resumed with {len(self.state)} block(s) and {len(self.runs)} run(s), "
                    f"searching {', '.join(b.label for b in backends)}{note}",
                    settings=self.settings, state=copy.deepcopy(self.state), strategy=strategy_text(self.state),
                    databases=[b.name for b in backends], options=_options(opts))

    # ------------------------------------------------------------- jobs

    def start(self, kind: str, block: int | None = None, n: int | None = None) -> bool:
        """Start a background job; False if there is nothing to search (only possible for "hasse")."""
        with self.lock:
            if kind == "hasse":
                if not isinstance(n, int):
                    raise UIError("say which run or analysis to draw")
                queries = self.hasse_missing(n)
                if not queries:
                    return False  # every node already has a count: draw at once, even while another job runs
            if self.job.get("running"):
                raise UIError("a search is already running; wait for it to finish")
            state = copy.deepcopy(self.state)
            if kind == "hasse":
                work = lambda: self._hasse_search(n, queries)
            elif kind == "run":
                if full_node(state) is None:
                    raise UIError("switch on at least one string first")
                work = lambda: self._run(state)
            elif kind == "analyze":
                name = self._block({"block": block})["name"]
                self.analysis_queries(state, name)  # fails early if there is nothing to analyze
                work = lambda: self._analyze(state, name)
            else:
                raise UIError(f"unknown job {kind!r}")
            self.job = {"running": True, "kind": kind, "done": 0, "total": 0, "message": "Starting", **({"n": n} if kind == "hasse" else {})}
        threading.Thread(target=self._guard, args=(kind, work), daemon=True).start()
        return True

    def _guard(self, kind: str, work) -> None:
        try:
            work()
            self.job = {"running": False}
        except (BackendError, HttpError, QueryError, UIError) as exc:
            self.log.add("error", f"{JOB_NAMES.get(kind, kind)} failed: {_short(exc)}", error=str(exc))
            self.job = {"running": False, "error": str(exc)}
        except Exception as exc:  # keep the page alive and say what happened
            self.log.add("error", f"{JOB_NAMES.get(kind, kind)} failed: {type(exc).__name__}: {_short(exc)}", error=traceback.format_exc())
            self.job = {"running": False, "error": f"{type(exc).__name__}: {exc}"}

    def _status(self, message: str, done: int | None = None, total: int | None = None) -> None:
        self.job = {**self.job, "message": message, **({"done": done} if done is not None else {}),
                    **({"total": total} if total is not None else {})}

    def _ensure_ready(self) -> None:
        if self.ready:
            return
        for backend in self.backends:
            if backend.name == "ebscohost":
                self._status("Connecting to the automation Firefox window: log in there, or answer the cookie banner, "
                             "if it asks (the terminal says what it is waiting for)")
            else:
                self._status(f"Checking {backend.label}")
            backend.check()
        if self.known_ids:
            self._status("Looking up the known items")
            self.known = resolve_known(self.backends, self.known_ids)
        self.ready = True

    def _search(self, queries: list[Query], scoring: list[Block], what: str) -> dict[Node, Outcome]:
        """Outcomes for `queries`, searching only those this session has not already finished."""
        missing = list({q.node: q for q in queries if q.node not in self.memo}.values())
        found: dict[Node, Outcome] = {}
        if missing:
            self._ensure_ready()
            total = len(missing) * len(self.backends)
            done = 0
            self._status(f"{what}: search 1 of {total}", 0, total)

            def step() -> None:
                nonlocal done
                done += 1
                self._status(f"{what}: search {min(done + 1, total)} of {total}", done, total)

            outcomes, _ = run(missing, self.backends, self.opts, scoring, self.known_ids, on_step=step, known=self.known)
            for outcome in outcomes:
                found[outcome.query.node] = outcome
                if not any(e.result.error for e in outcome.by_backend.values()):
                    self.memo[outcome.query.node] = outcome
        return {q.node: found.get(q.node) or self.memo[q.node] for q in queries}

    def _check_saved(self, items: list[tuple[Node, Outcome | None]], what: str, related: list[Node] = ()) -> None:
        """Find which saved articles each query in `items` retrieves (an outcome of None means proved empty),
        searching only for articles it has not been checked against. Containment answers what it can first:
        an article a narrower query retrieves, a broader one does too, and one a broader query misses, a
        narrower one misses. `related` are other queries whose earlier checks can be used that way."""
        nodes = [node for node, _ in items] + list(related)
        for backend in self.backends:
            ids = set(self._saved_ids(backend.name))
            if not ids:
                continue
            for node, outcome in items:
                hits = self.saved_hits.setdefault((backend.name, node), {"checked": set(), "found": set()})
                result = outcome.by_backend[backend.name].result if outcome else None
                if not ids - hits["checked"] or (result is not None and result.error):
                    continue
                if result is None or not result.count:
                    hits["checked"] |= ids
                    continue
                for other in nodes:
                    known = self.saved_hits.get((backend.name, other))
                    if other == node or not known:
                        continue
                    todo = ids - hits["checked"]
                    if (found := known["found"] & todo) and covers(node, other):
                        hits["found"] |= found
                        hits["checked"] |= found
                    if (missed := (known["checked"] - known["found"]) & todo) and covers(other, node):
                        hits["checked"] |= missed
                todo = ids - hits["checked"]
                if not todo:
                    continue
                self._ensure_ready()
                self._status(f"{what}: checking which saved articles it finds")
                try:
                    found = backend.find_known(node, self.opts, sorted(todo))
                except NotImplementedError:
                    break  # this database cannot look records up by id
                except (HttpError, BackendError) as exc:
                    hits["error"] = str(exc)
                    continue
                hits.pop("error", None)
                hits["found"] |= found & todo
                hits["checked"] |= todo

    def _saved_fields(self, backend: str, node: Node) -> dict:
        """What a result shows about the saved articles: how many of them the query retrieves, and which.
        Nothing when there are none from `backend`, or the query was not checked against all of them."""
        ids = self._saved_ids(backend)
        hits = self.saved_hits.get((backend, node))
        if not ids or not hits:
            return {}
        if ids.keys() - hits["checked"]:
            return {"saved_total": len(ids), "saved_found": None, "saved_error": hits["error"]} if hits.get("error") else {}
        return {"saved_total": len(ids), "saved_found": len(ids.keys() & hits["found"]),
                "saved_hits": [ids[i] or i for i in ids if i in hits["found"]],
                "saved_missed": [ids[i] or i for i in ids if i not in hits["found"]]}

    def _entry(self, item: dict, outcome: Outcome | None, scoring: list[Block]) -> dict:
        query = item["query"]
        entry = {k: v for k, v in item.items() if k != "query"}
        entry.update(label=query.label, query=to_generic(query.node), by={})
        for backend in self.backends:
            saved = self._saved_fields(backend.name, query.node)
            if outcome is None:  # proved empty without searching
                entry["by"][backend.name] = {"count": 0, "sent": backend.render(query.node, self.opts), "records": [], **saved}
                continue
            e = outcome.by_backend[backend.name]
            r = e.result
            on_topic = None if r.error else score_records(r.records, scoring)
            known = self.known.get(backend.name)
            entry["by"][backend.name] = {
                "count": r.count, "error": r.error, "warnings": r.warnings, "interpretation": r.interpretation,
                "sent": r.query_sent or backend.render(query.node, self.opts),
                "known_found": e.known_found, "known_total": len(known.indexed) if known else None,
                "known_missed": e.known_missed, "on_topic": on_topic,
                "records": [_record(x) for x in r.records], **saved,
            }
        return entry

    def _run(self, state: list[dict]) -> None:
        base_n = self.baseline()
        plan = self.plan(state, base_n)
        blocks = _blocks(state)
        outcomes = self._search([i["query"] for i in plan if not i.get("proved_empty")], blocks, "Running")
        self._check_saved([(i["query"].node, None if i.get("proved_empty") else outcomes[i["query"].node]) for i in plan],
                          "Running", [self.runs[base_n]] if base_n is not None else [])
        queries = [self._entry(i, outcomes.get(i["query"].node) if not i.get("proved_empty") else None, blocks) for i in plan]
        entry = self.log.add("run", _run_summary(queries, self.backends, base_n), baseline=base_n,
                             strategy=strategy_text(state), state=state, queries=queries, **self._budget())
        with self.lock:
            self.runs[entry["n"]] = plan[0]["query"].node

    def _analyze(self, state: list[dict], name: str) -> None:
        blocks = _blocks(state)
        queries = self.analysis_queries(state, name)
        scoring = with_candidates(blocks)
        outcomes = self._search(queries, scoring, f"Analyzing [{name}]")
        self._check_saved([(q.node, outcomes[q.node]) for q in queries], f"Analyzing [{name}]")
        # refine() returns the full strategy first, then "FULL minus <string>" and "added by <string>" queries
        rows = [self._entry({"role": "full" if i == 0 else "minus" if q.label.startswith("FULL minus ") else "adds", "query": q},
                            outcomes[q.node], scoring)
                for i, q in enumerate(queries)]
        full = rows[0]
        effects: dict[str, dict] = {}
        for row in rows[1:]:
            text = row["label"].split(" ", 2)[2]  # "FULL minus <string>" / "added by <string>"
            row["term"] = text
            by, saved = {}, {}
            for backend in self.backends:
                r, base = row["by"][backend.name], full["by"][backend.name]
                count = r["count"]
                if row["role"] == "minus":
                    r["lost"] = by[backend.name] = None if count is None or base["count"] is None else base["count"] - count
                    if r.get("saved_found") is not None and base.get("saved_found") is not None:
                        r["saved_lost"] = saved[backend.name] = base["saved_found"] - r["saved_found"]
                else:
                    by[backend.name] = count
                    if r.get("saved_found") is not None:
                        saved[backend.name] = r["saved_found"]
            effects[text] = {"kind": "lost" if row["role"] == "minus" else "adds", "by": by, **({"saved": saved} if saved else {})}
        target = next(b for b in blocks if b.name == name)
        for r in find_redundant([target]):
            effects.setdefault(r.text, {"kind": "covered", "reason": r.reason, "by": {}})
        if len(target.terms) == 1:
            only = to_generic(target.terms[0])
            effects.setdefault(only, {"kind": "only", "by": {}})
        primary = self.backends[0].name
        parts = [f"{t}: {'-' if e['kind'] == 'lost' else '+'}{_number(e['by'].get(primary))}" for t, e in effects.items() if e["kind"] in ("lost", "adds")]
        entry = self.log.add("analyze", f"Analyzed [{name}] in {self.backends[0].label}: " + ("; ".join(parts) or "nothing to search"),
                             block=name, strategy=strategy_text(state), state=state, rows=rows, effects=effects, **self._budget())
        with self.lock:
            self.analysis[name] = {"n": entry["n"], "strategy": strategy_text(state), "terms": effects}

    def _hasse_parts(self, n: int) -> tuple[dict, list[dict], list[tuple[str, Node, str]]]:
        """Run or analysis `n`, the searches it made that its diagram shows, and the other nodes of the
        diagram: each block, each switched-on string and each combination of blocks (see
        `_block_combinations`) of its strategy not already among those searches."""
        e = self.log.get(n)
        if e["kind"] not in ("run", "analyze"):
            raise UIError(f"#{n} is not a run or an analysis")
        # a run's "added"/"lost" searches compare with another run's strategy, so they are left out
        searched = [q for q in (e["queries"] if e["kind"] == "run" else e["rows"]) if q["role"] in HASSE_ROLES]
        if n not in self.hasse_extra:  # log entries never change, so this is worked out once
            blocks = _blocks(e["state"])
            nodes = [(block.label, block.node, "blocks") for block in blocks]
            nodes += [(to_generic(t), t, "strings") for block in blocks for t in block.terms]
            nodes += _block_combinations(blocks)
            seen, extra = {q["query"] for q in searched}, []
            for label, node, strategy in nodes:
                text = to_generic(node)
                if text not in seen:
                    seen.add(text)
                    extra.append((label, node, strategy))
            self.hasse_extra[n] = extra
        return e, searched, self.hasse_extra[n]

    def _earlier(self) -> dict[str, dict]:
        """Query text -> backend -> outcome, from every error-free search logged in this session."""
        earlier: dict[str, dict] = {}
        for other in self.log.entries:
            if other["kind"] in ("run", "analyze", "hasse"):
                for q in other.get("queries") or other.get("rows") or []:
                    found = {name: r for name, r in q["by"].items() if not r.get("error")}
                    if found:
                        earlier[q["query"]] = {**earlier.get(q["query"], {}), **found}
        return earlier

    def hasse_missing(self, n: int, earlier: dict[str, dict] | None = None) -> list[Query]:
        """The nodes of run or analysis `n`'s diagram that the current databases have not searched yet.
        Empty if `n` searched other databases (a switch since), since searching now would not fill its diagram."""
        e, searched, extra = self._hasse_parts(n)
        if any(b.name not in searched[0]["by"] for b in self.backends):
            return []
        earlier = self._earlier() if earlier is None else earlier
        return [Query(label, strategy, node) for label, node, strategy in extra
                if any(b.name not in earlier.get(to_generic(node), {}) for b in self.backends)]

    def _hasse_search(self, n: int, queries: list[Query]) -> None:
        e = self.log.get(n)
        blocks = _blocks(e["state"])
        scoring = with_candidates(blocks) if e["kind"] == "analyze" else blocks
        outcomes = self._search(queries, scoring, f"Hasse diagram #{n}")
        rows = [self._entry({"role": q.strategy, "query": q}, outcomes[q.node], scoring) for q in queries]
        failed = sum(any(r.get("error") for r in row["by"].values()) for row in rows)
        self.log.add("hasse", f"Searched {len(rows)} block(s), string(s) and block combination(s) for the Hasse diagram of #{n}"
                     + (f"; {failed} failed" if failed else ""), of=n, queries=rows, **self._budget())

    def hasse_wait_html(self, n: int, theme: str) -> str:
        """Shown in the diagram's tab while its searches run; it reloads itself until they are done."""
        from . import hasse

        colors = hasse.THEMES.get(theme, hasse.THEMES["light"])
        job = self.job
        done, total = job.get("done", 0), job.get("total", 0)
        pct = round(100 * done / total) if total else 0
        return f"""<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="refresh" content="1">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Hasse diagram #{n}</title>
<meta name="color-scheme" content="{'dark' if theme == 'dark' else 'light'}">
<style>body {{ margin: 0; padding: 48px 24px; background: {colors['surface']}; color: {colors['ink']}; font-family: {hasse.FONT}; }}
.bar {{ max-width: 480px; height: 6px; border-radius: 3px; background: {colors['edge']}; overflow: hidden; }}
.bar span {{ display: block; height: 100%; width: {pct}%; background: {colors['ramp'][4]}; }}
p {{ color: {colors['ink2']}; }}</style></head>
<body><h1 style="font-size: 20px">Hasse diagram #{n}</h1>
<p>Searching the blocks, strings and block combinations not yet searched, so every node has a count. The diagram appears here when they are done.</p>
<p>{html.escape(job.get('message') or 'Starting')}</p><div class="bar"><span></span></div></body></html>"""

    def hasse_html(self, n: int, backend: str, theme: str, plotlyjs: str) -> str:
        """A Hasse diagram page for run or analysis `n`: its searched queries plus each block, each
        switched-on string and each combination of blocks of its strategy. Those take their counts from any search of the same query in
        this session (the diagram button searches the missing ones first), and are otherwise drawn hollow."""
        from . import hasse  # plotly loads only when a diagram is asked for

        e, searched, extra = self._hasse_parts(n)
        if theme not in hasse.THEMES:
            theme = "light"
        if backend not in searched[0]["by"]:
            raise UIError(f"#{n} did not search {backend!r}")

        def outcomes(by: dict) -> dict:
            return {name: {"result": {"count": r.get("count"), "error": r.get("error")}, "known_found": r.get("known_found"),
                           "known_total": r.get("known_total"), "on_topic": r.get("on_topic")} for name, r in by.items()}

        earlier = self._earlier()
        queries = [{"n": i, "label": "Full strategy" if q["role"] == "full" else q["label"], "strategy": HASSE_ROLES[q["role"]],
                    "query": q["query"], "backends": outcomes(q["by"])} for i, q in enumerate(searched, 1)]
        for label, node, strategy in extra:
            text = to_generic(node)
            queries.append({"n": len(queries) + 1, "label": label, "strategy": strategy, "query": text,
                            "backends": outcomes(earlier.get(text, {}))})
        what = f"Run #{n}" if e["kind"] == "run" else f"Analysis #{n} of [{e['block']}]"
        label = next((b.label for b in self.pool.values() if b.name == backend), backend)
        figure = hasse.build_figure({"queries": queries}, backend, theme=theme, title=f"{what}: queries ordered by containment, with {label} hits")
        page = figure.to_html(include_plotlyjs=plotlyjs, full_html=True, config={"displaylogo": False})
        head = (f"<head><title>Hasse diagram #{n}</title><meta name=\"color-scheme\" content=\"{theme}\">"
                f"<style>body {{ margin: 0; background: {hasse.THEMES[theme]['surface']}; }}</style>")
        return page.replace("<head>", head, 1)

    def _budget(self) -> dict:
        left = {b.name: b.max_searches - b.searches for b in self.backends if hasattr(b, "max_searches")}
        return {"searches_left": left} if left else {}

    def close(self) -> None:
        for backend in self.backends:
            backend.close()
        self.log.add("session end", f"Session ended after {len(self.runs)} run(s)")


def _block_combinations(blocks: list[Block]) -> list[tuple[str, Node, str]]:
    """Every AND of 2 to all-but-one of the blocks, smallest first, so a diagram runs from single blocks
    down to the full strategy. Sizes that would take the total past MAX_HASSE_COMBINATIONS are left out."""
    combos: list[tuple[str, Node, str]] = []
    for size in range(2, len(blocks)):
        group = list(combinations(blocks, size))
        if len(combos) + len(group) > MAX_HASSE_COMBINATIONS:
            break
        combos += [(" AND ".join(b.label for b in g), all_of(b.node for b in g), "block combinations") for g in group]
    return combos


def _short(text: str, limit: int = 200) -> str:
    """One line for a summary; the full text stays in the entry's other fields."""
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


_plotly_cache: list[bytes] = []


def _plotly_js() -> bytes:
    if not _plotly_cache:
        from plotly.offline import get_plotlyjs
        _plotly_cache.append(get_plotlyjs().encode())
    return _plotly_cache[0]


def _brief(entry: dict) -> dict:
    return {k: entry[k] for k in ("n", "time", "kind", "summary")}


def _run_summary(queries: list[dict], backends: list[Backend], base_n: int | None) -> str:
    primary = backends[0]
    by_role = {q["role"]: q["by"][primary.name] for q in queries if q["role"] in ("full", "added", "lost")}
    full = by_role["full"]
    if full.get("error"):
        return f"Run: {primary.label} failed: {_short(full['error'])}"
    text = f"Run: {primary.label} {_number(full['count'])} hits"
    if full.get("saved_found") is not None:
        text += f", {full['saved_found']}/{full['saved_total']} saved articles"
    if base_n is not None and "added" in by_role:
        added, lost = by_role["added"]["count"], by_role["lost"]["count"]
        text += f"; vs #{base_n}: +{_number(added)} added, -{_number(lost)} lost"
        if by_role["lost"].get("saved_found"):
            text += f" (incl. {by_role['lost']['saved_found']} saved)"
    elif base_n is not None:
        text += f"; same strategy as #{base_n}"
    return text


# ------------------------------------------------------------------- http


class Handler(BaseHTTPRequestHandler):
    server: "Server"

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


if __name__ == "__main__":
    sys.exit(main())
