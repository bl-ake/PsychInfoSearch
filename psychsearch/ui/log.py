"""The session log (one JSON line per action or result), the saved relevant articles, and replaying a
log to carry on an earlier session."""

from __future__ import annotations

import copy
import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path

from ..backends import BACKENDS, Record, SearchOptions
from ..query import FIELDS, Node, QueryError
from .strategy import UIError, full_node

SAVED_FILE = "relevant-articles.json"  # beside the ui/ log folder, shared by every session


def record_json(record: Record) -> dict:
    # All authors and subjects are kept; the page shortens the lists itself.
    # Logs written before that have at most 6 authors, plus "more_authors" counting the rest.
    return {
        "id": record.id, "title": record.title, "authors": record.authors,
        "year": record.year, "date": record.date, "journal": record.journal, "doi": record.doi, "url": record.url,
        "abstract": record.abstract, "subjects": record.subjects, "pub_types": record.pub_types,
        "cited_by": record.cited_by, "blocks_matched": record.extra.get("blocks_matched"),
    }


class Log:
    """Append-only JSON lines; every entry is numbered like a database search history."""

    def __init__(self, path: Path, entries: list[dict] | None = None):
        self.path = path
        self.entries: list[dict] = entries or []
        self.lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    @classmethod
    def resume(cls, path: Path) -> Log:
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


def options_json(o: SearchOptions) -> dict:
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
