import json
import time
import urllib.error
import urllib.request
import threading

import pytest

from psychsearch.backends import Backend, Record, SearchOptions
from psychsearch.combos import blocks_from_lines
from psychsearch.http import Http
from psychsearch.query import matches, to_generic
from psychsearch.ui import Log, Server, Session, UIError, read_log, state_from_blocks, terms_file_text

CORPUS = {
    "r1": "gamified math practice for middle school students",
    "r2": "serious game for mathematics students",
    "r3": "game based learning in a math classroom",
    "r4": "gamification of algebra homework for students",
    "r5": "reading games for young children",
}


class Fake(Backend):
    name = "fake"
    label = "Fake"

    def __init__(self, http=None):
        super().__init__(http)
        self.sent = []

    def render(self, node, opts):
        return to_generic(node)

    def _search(self, node, opts, result):
        self.sent.append(to_generic(node))
        hits = [i for i, text in CORPUS.items() if matches(node, text)]
        result.query_sent = to_generic(node)
        result.count = len(hits)
        result.records = [Record(id=i, title=CORPUS[i]) for i in hits[: opts.sample]]


LINES = ["[math]", "math*", "[game]", "game*", "? gamif*", "[people]", "student*"]


def _session(tmp_path, lines=LINES):
    backend = Fake()
    log = Log(tmp_path / "ui" / "log.jsonl")
    session = Session(state_from_blocks(blocks_from_lines(lines)), [backend], SearchOptions(), [], log, None, {"terms file": "x"})
    return session, backend


def _wait(session):
    deadline = time.time() + 10
    while session.job.get("running"):
        assert time.time() < deadline
        time.sleep(0.02)
    assert "error" not in session.job, session.job


def _run(session):
    session.start("run")
    _wait(session)
    return session.log.entries[-1]


def _row(entry, role):
    return next(q for q in entry["queries"] if q["role"] == role)["by"]["fake"]


def test_candidates_start_switched_off(tmp_path):
    session, _ = _session(tmp_path)
    game = session.state[1]
    assert game["terms"] == [{"text": "game*", "on": True}, {"text": "gamif*", "on": False}]
    assert session.view()["strategy"] == "math* AND game* AND student*"


def test_adding_a_string_shows_exactly_what_it_adds_without_a_lost_search(tmp_path):
    session, backend = _session(tmp_path)
    first = _run(session)
    assert [r["id"] for r in _row(first, "full")["records"]] == ["r2"]  # "gamified" is not game*

    session.act({"type": "toggle term", "block": 1, "term": 1})  # switch gamif* on
    view = session.view()
    assert view["pending"]["proved_empty"] == 1  # widening a block cannot lose records
    backend.sent.clear()
    second = _run(session)
    added, lost = _row(second, "added"), _row(second, "lost")
    assert added["count"] == 1 and [r["id"] for r in added["records"]] == ["r1"]
    assert lost["count"] == 0 and not any("NOT (math*" in s and s.startswith("math* AND game*") for s in backend.sent)
    assert second["baseline"] == first["n"]
    assert "+1 added, -0 lost" in second["summary"]


def test_counts_match_the_corpus(tmp_path):
    session, _ = _session(tmp_path)
    entry = _run(session)
    expected = [i for i, t in CORPUS.items() if matches(session.runs[entry["n"]], t)]
    assert _row(entry, "full")["count"] == len(expected)
    blocks = {q["block"]: q["by"]["fake"]["count"] for q in entry["queries"] if q["role"] == "block"}
    assert blocks == {"math": 3, "game": 3, "people": 3}


def test_unchanged_queries_are_not_searched_again(tmp_path):
    session, backend = _session(tmp_path)
    _run(session)
    backend.sent.clear()
    session.act({"type": "toggle block", "block": 2})  # leave [people] out
    session.act({"type": "toggle block", "block": 2})  # and back in
    entry = _run(session)
    assert backend.sent == []
    assert "same strategy" in entry["summary"]


def test_analyze_reports_what_each_string_contributes(tmp_path):
    session, _ = _session(tmp_path)
    session.act({"type": "add term", "block": 0, "text": "algebra"})
    session.start("analyze", 0)
    _wait(session)
    entry = session.log.entries[-1]
    assert entry["kind"] == "analyze"
    effects = entry["effects"]
    assert effects["algebra"] == {"kind": "lost", "by": {"fake": 0}}  # r4 is not a game* record
    assert effects["math*"]["kind"] == "lost"
    assert session.view()["blocks"][0]["terms"][1]["effect"]["kind"] == "lost"


def test_redundant_strings_are_flagged_without_searching(tmp_path):
    session, backend = _session(tmp_path)
    session.act({"type": "add term", "block": 0, "text": "mathematics"})
    terms = session.view()["blocks"][0]["terms"]
    assert terms[1]["redundant"] == "covered by math*"
    assert backend.sent == []


def test_actions_and_rejections_are_logged(tmp_path):
    session, _ = _session(tmp_path)
    session.act({"type": "add term", "block": 2, "text": "pupil*"})
    with pytest.raises(UIError):
        session.act({"type": "add term", "block": 2, "text": '"unbalanced'})
    with pytest.raises(UIError):
        session.act({"type": "add term", "block": 2, "text": "pupil*"})
    kinds = [e["kind"] for e in read_log(session.log.path)]
    assert kinds == ["session start", "add term", "rejected", "rejected"]
    assert session.log.entries[1]["strategy"] == "math* AND game* AND (student* OR pupil*)"


def test_restore_and_compare_with(tmp_path):
    session, _ = _session(tmp_path)
    first = _run(session)
    session.act({"type": "remove term", "block": 2, "term": 0})
    session.act({"type": "add term", "block": 2, "text": "pupil*"})
    second = _run(session)
    session.act({"type": "compare with", "run": first["n"]})
    assert session.baseline() == first["n"]
    session.act({"type": "restore", "run": first["n"]})
    assert session.view()["strategy"] == first["strategy"]
    assert second["n"] in session.runs


def test_save_writes_a_loadable_terms_file(tmp_path):
    session, _ = _session(tmp_path)
    session.act({"type": "toggle block", "block": 2})
    path = tmp_path / "saved.txt"
    session.act({"type": "save", "path": str(path)})
    blocks = blocks_from_lines(path.read_text().splitlines())
    assert [b.name for b in blocks] == ["math", "game"]
    assert [to_generic(c) for c in blocks[1].candidates] == ["gamif*"]
    with pytest.raises(UIError, match="^EXISTS:"):
        session.act({"type": "save", "path": str(path)})
    assert session.log.entries[-1]["kind"] == "save"  # asking to overwrite is not logged as a rejection


def test_terms_file_text_comments_out_empty_blocks():
    state = [{"name": "a", "on": True, "terms": [{"text": "x", "on": False}]}]
    text = terms_file_text(state, "note")
    assert "# [a] (switched off)" in text and "# x" in text


def test_http_needs_the_token_and_local_host(tmp_path):
    session, _ = _session(tmp_path)
    server = Server(0, session)
    port = server.server_address[1]
    server.hosts = {f"127.0.0.1:{port}"}
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{port}"
        page = urllib.request.urlopen(base + "/").read().decode()
        assert server.token in page
        with pytest.raises(urllib.error.HTTPError) as no_token:
            urllib.request.urlopen(base + "/api/state")
        assert no_token.value.code == 403
        request = urllib.request.Request(base + "/api/action", data=json.dumps({"type": "toggle block", "block": 0}).encode(),
                                         headers={"X-Token": server.token, "Content-Type": "application/json"})
        state = json.loads(urllib.request.urlopen(request).read())
        assert state["blocks"][0]["on"] is False
        bad_host = urllib.request.Request(base + "/", headers={"Host": "evil.example"})
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(bad_host)
    finally:
        server.shutdown()
        server.server_close()
