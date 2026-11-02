"""One page's working session: the strategy, its runs and analyses, and the searches behind them."""

from __future__ import annotations

import copy
import html
import math
import shlex
import threading
import traceback
from datetime import datetime
from functools import partial
from itertools import combinations
from pathlib import Path

from ..backends import BACKENDS, Backend, BackendError, SearchOptions
from ..combos import Block, Query, blocks_from_lines, refine, with_candidates
from ..http import HttpError
from ..query import Node, Not, QueryError, all_of, to_generic
from ..redundancy import covers, find_redundant
from ..runner import KnownSet, Outcome, resolve_known, run, score_records
from .log import SAVED_FILE, Log, load_saved, options_json, record_json, replay, write_saved
from .strategy import LINE, UIError, clean_name, clean_term, full_node, state_from_string, strategy_text, terms_file_text, to_blocks

RECENT = 12  # log entries shown beside the results
# searches of a run or analysis that its Hasse diagram shows, and the diagram's name for each
HASSE_ROLES = {"full": "full strategy", "block": "blocks", "minus": "full minus a string", "adds": "added by a string"}
MAX_HASSE_COMBINATIONS = 60  # block combinations a diagram adds; with 7+ blocks the larger ones are left out
JOB_NAMES = {"run": "Run", "analyze": "Analyze", "hasse": "Searching for the Hasse diagram"}


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
        self.last_saved: Path | None = None  # the terms file this page saved to last
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
                databases=[b.name for b in backends], options=options_json(opts))

    # ---------------------------------------------------------- reading

    def latest_run(self) -> int | None:
        return max(self.runs) if self.runs else None

    def baseline(self) -> int | None:
        return self.pinned if self.pinned is not None else self.latest_run()

    def plan(self, state: list[dict], base_n: int | None) -> list[dict]:
        """What a run of `state` searches: the full strategy, each block, and the change from run `base_n`."""
        blocks = to_blocks(state)
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
        blocks = to_blocks(state)
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
                for r in find_redundant(to_blocks(state, include_off=True)):
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
                "saved": [{k: a.get(k) for k in ("database", "id", "title", "year", "journal", "url", "saved")}
                          | {"authors": (a.get("authors") or [])[:3]} for a in reversed(self.saved)],
                "saved_file": str(self.saved_path),
            }

    def _default_save_path(self) -> Path:
        """Where Save suggests: the file saved to last, else beside the terms file the page started from,
        else the psychsearch folder."""
        if self.last_saved is not None:
            return self.last_saved
        if self.terms_path is None:
            return Path.cwd() / "terms-ui.txt"  # the psychsearch folder
        return self.terms_path.with_name(f"{self.terms_path.stem}-ui{self.terms_path.suffix or '.txt'}")

    def _save_target(self, text: str) -> Path:
        """The file a name typed in the save dialog means: a bare name goes in the suggested folder, a path
        (absolute, or starting with ~) is used as is, and a name with no extension gets .txt."""
        text = text.strip()
        if not text:
            raise UIError("type a file name first")
        path = Path(text).expanduser()
        if not path.is_absolute():
            path = self._default_save_path().parent / path
        if text.endswith(("/", "\\")) or path.is_dir():
            raise UIError(f"{path} is a folder; type a file name, such as {path / 'my-terms.txt'}")
        return path if path.suffix else path.with_name(path.name + ".txt")

    def save_preview(self) -> dict:
        """What the save dialog shows: the suggested folder and name, and the file as it would be saved."""
        with self.lock:
            path = self._default_save_path()
            return {"folder": str(path.parent), "folder_shown": _tilde(path.parent), "name": path.name,
                    "text": terms_file_text(self.state, f"Saved by psychsearch-ui on {datetime.now():%Y-%m-%d %H:%M}")}

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
            text = clean_term(str(action.get("text", "")))
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
            name = clean_name(str(action.get("name", "")), self.state)
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
        path = self._save_target(path_text)
        if path.exists() and not overwrite:
            raise UIError(f"EXISTS:{path}")
        text = terms_file_text(self.state, f"Saved by psychsearch-ui on {datetime.now():%Y-%m-%d %H:%M}; log {self.log.path}")
        try:
            blocks_from_lines(text.splitlines())  # what is saved must load again
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        except (OSError, QueryError) as exc:
            raise UIError(f"could not save to {path}: {exc}") from None
        self.last_saved = path
        # psychsearch-ui looks for a bare file name in the psychsearch folder, which is the current folder here
        name = str(path.relative_to(Path.cwd())) if path.parent == Path.cwd() else str(path)
        command = f"psychsearch-ui {shlex.quote(name)}"
        return f"Saved the strategy to {_tilde(path)}", {"path": str(path), "text": text, "command": command}

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
                    databases=[b.name for b in backends], options=options_json(opts))

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
                work = partial(self._hasse_search, n, queries)
            elif kind == "run":
                if full_node(state) is None:
                    raise UIError("switch on at least one string first")
                work = partial(self._run, state)
            elif kind == "analyze":
                name = self._block({"block": block})["name"]
                self.analysis_queries(state, name)  # fails early if there is nothing to analyze
                work = partial(self._analyze, state, name)
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
                "records": [record_json(x) for x in r.records], **saved,
            }
        return entry

    def _run(self, state: list[dict]) -> None:
        base_n = self.baseline()
        plan = self.plan(state, base_n)
        blocks = to_blocks(state)
        outcomes = self._search([i["query"] for i in plan if not i.get("proved_empty")], blocks, "Running")
        self._check_saved([(i["query"].node, None if i.get("proved_empty") else outcomes[i["query"].node]) for i in plan],
                          "Running", [self.runs[base_n]] if base_n is not None else [])
        queries = [self._entry(i, outcomes.get(i["query"].node) if not i.get("proved_empty") else None, blocks) for i in plan]
        entry = self.log.add("run", _run_summary(queries, self.backends, base_n), baseline=base_n,
                             strategy=strategy_text(state), state=state, queries=queries, **self._budget())
        with self.lock:
            self.runs[entry["n"]] = plan[0]["query"].node

    def _analyze(self, state: list[dict], name: str) -> None:
        blocks = to_blocks(state)
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
            blocks = to_blocks(e["state"])
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
        blocks = to_blocks(e["state"])
        scoring = with_candidates(blocks) if e["kind"] == "analyze" else blocks
        outcomes = self._search(queries, scoring, f"Hasse diagram #{n}")
        rows = [self._entry({"role": q.strategy, "query": q}, outcomes[q.node], scoring) for q in queries]
        failed = sum(any(r.get("error") for r in row["by"].values()) for row in rows)
        self.log.add("hasse", f"Searched {len(rows)} block(s), string(s) and block combination(s) for the Hasse diagram of #{n}"
                     + (f"; {failed} failed" if failed else ""), of=n, queries=rows, **self._budget())

    def hasse_wait_html(self, n: int, theme: str) -> str:
        """Shown in the diagram's tab while its searches run; it reloads itself until they are done."""
        from .. import hasse

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
        from .. import hasse  # plotly loads only when a diagram is asked for

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
        # every database the page could search, not only the current one: a switch leaves the old one open
        for backend in {id(b): b for b in [*self.backends, *self.pool.values()]}.values():
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


def _tilde(path: Path) -> str:
    """`path` as people see it, with the home folder written ~."""
    home = Path.home()
    return f"~/{path.relative_to(home)}" if path.is_relative_to(home) and path != home else str(path)


def _number(value: int | None) -> str:
    return "-" if value is None else f"{value:,}"


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
