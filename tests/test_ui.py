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
from psychsearch.ui import Log, Server, Session, UIError, read_log, state_from_blocks, state_from_string, terms_file_text

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
        self.known_checks = []

    def render(self, node, opts):
        return to_generic(node)

    def find_known(self, node, opts, known_ids):
        self.known_checks.append((to_generic(node), sorted(known_ids)))
        return {i for i in known_ids if i in CORPUS and matches(node, CORPUS[i])}

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


def test_state_from_a_search_string():
    state = state_from_string('(math* OR algebra) AND (game* OR “serious game” OR game*) AND student*')
    assert state == [
        {"name": "block 1", "on": True, "terms": [{"text": "math*", "on": True}, {"text": "algebra", "on": True}]},
        {"name": "block 2", "on": True, "terms": [{"text": "game*", "on": True}, {"text": '"serious game"', "on": True}]},
        {"name": "block 3", "on": True, "terms": [{"text": "student*", "on": True}]},
    ]
    assert state_from_string("a OR (b AND c)")[0]["terms"][1] == {"text": "b AND c", "on": True}  # a string can be any query
    for bad in ("", "   ", "(a OR b", '"a', "a AND NOT b", "a OR b#c"):
        with pytest.raises(UIError):
            state_from_string(bad)


def test_loading_a_search_string_replaces_the_strategy(tmp_path):
    session, _ = _session(tmp_path)
    first = _run(session)
    session.start("analyze", 0)
    _wait(session)
    session.act({"type": "load string", "text": "(math* OR algebra) AND game*"})
    view = session.view()
    assert view["strategy"] == "(math* OR algebra) AND game*"
    assert [b["name"] for b in view["blocks"]] == ["block 1", "block 2"] and session.analysis == {}
    assert session.log.entries[-1]["kind"] == "load string" and session.baseline() == first["n"]
    with pytest.raises(UIError):
        session.act({"type": "load string", "text": "math* AND NOT game*"})
    assert session.view()["strategy"] == "(math* OR algebra) AND game*"  # a rejected string changes nothing
    session.act({"type": "toggle term", "block": 0, "term": 1})
    session.close()

    later, _ = _later_session(tmp_path)
    later.load(session.log.path.name)  # replays the loaded string and the edit after it
    assert later.state == session.state and later.analysis == {}


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


def _later_session(tmp_path, name="later.jsonl"):
    backend = Fake()
    log = Log(tmp_path / "ui" / name)
    return Session([], [backend], SearchOptions(field="ti"), [], log, None, {"terms file": "(none)"}), backend


def test_loading_a_session_carries_on_where_it_left_off(tmp_path):
    session, _ = _session(tmp_path)
    first = _run(session)
    session.act({"type": "add term", "block": 0, "text": "algebra"})
    second = _run(session)
    session.act({"type": "compare with", "run": first["n"]})
    session.start("analyze", 0)
    _wait(session)
    session.act({"type": "add block", "name": "extra"})
    session.act({"type": "add term", "block": 3, "text": "school*"})
    session.act({"type": "toggle term", "block": 1, "term": 1})
    session.act({"type": "remove term", "block": 0, "term": 0})
    session.act({"type": "toggle block", "block": 3})
    session.close()

    later, backend = _later_session(tmp_path)
    stub = later.log.path
    later.load(session.log.path.name)
    assert later.state == session.state
    assert set(later.runs) == {first["n"], second["n"]} and later.pinned == first["n"]
    assert later.analysis["math"]["n"] == session.analysis["math"]["n"]
    assert later.opts == SearchOptions()  # the loaded session's settings, not the ones it was started with
    assert later.log.path == session.log.path and not stub.exists()  # nothing had happened in the new session
    view = later.view()
    assert view["baseline"] == first["n"] and view["blocks"][1]["terms"][1]["on"] is True
    resumed = later.log.entries[-1]
    assert resumed["kind"] == "resume" and resumed["n"] == len(session.log.entries) + 1

    third = _run(later)  # compares with the pinned run of the loaded session, numbered on from its log
    assert third["baseline"] == first["n"] and third["n"] == resumed["n"] + 1
    assert later.log.get(second["n"])["kind"] == "run"
    assert backend.sent  # the new session had searched nothing yet

    again, _ = _later_session(tmp_path, "again.jsonl")
    again.load(session.log.path.name)  # replaying a log that was already resumed once
    assert again.state == later.state and set(again.runs) == {first["n"], second["n"], third["n"]}


def test_loading_keeps_a_session_that_did_something(tmp_path):
    session, _ = _session(tmp_path)
    session.close()
    later, _ = _later_session(tmp_path)
    later.act({"type": "add block", "name": "x"})
    own = later.log.path
    later.load(session.log.path.name)
    assert read_log(own)[-1]["kind"] == "load" and read_log(own)[-1]["file"] == session.log.path.name
    with pytest.raises(UIError):
        later.load(session.log.path.name)  # already open
    with pytest.raises(UIError):
        later.load("nope.jsonl")
    (tmp_path / "ui" / "junk.jsonl").write_text('{"n": 1, "kind": "run"}\n')
    with pytest.raises(UIError):
        later.load("junk.jsonl")


def test_load_over_http(tmp_path):
    session, _ = _session(tmp_path)
    _run(session)
    session.close()
    later, _ = _later_session(tmp_path)
    server = Server(0, later)
    port = server.server_address[1]
    server.hosts = {f"127.0.0.1:{port}"}
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{port}"
        headers = {"X-Token": server.token, "Content-Type": "application/json"}
        sessions = json.loads(urllib.request.urlopen(urllib.request.Request(base + "/api/sessions", headers=headers)).read())
        earlier = next(s for s in sessions if not s["current"])
        assert earlier["runs"] == 1 and earlier["loadable"] and earlier["strategy"] == "math* AND game* AND student*"
        request = urllib.request.Request(base + "/api/load", data=json.dumps({"session": earlier["file"]}).encode(), headers=headers)
        state = json.loads(urllib.request.urlopen(request).read())
        assert state["log_file"] == str(session.log.path) and state["latest"] == 2 and state["strategy"] == earlier["strategy"]
    finally:
        server.shutdown()
        server.server_close()


def test_switch_database_resets_the_comparison(tmp_path):
    class Other(Backend):
        name, label = "other", "Other"

        def render(self, node, opts):
            return to_generic(node)

    session = Session([], [Other(Http(None))], SearchOptions(), [], Log(tmp_path / "log.jsonl"), None, {"terms file": "x"},
                      [Other(Http(None))])
    session.act({"type": "switch database", "name": "other"})
    assert [b.name for b in session.backends] == ["other"] and session.runs == {}
    with pytest.raises(UIError):
        session.act({"type": "switch database", "name": "nope"})


def _save(session, entry, rid):
    session.act({"type": "save article", "n": entry["n"], "database": "fake", "id": rid})


def test_saved_articles_are_counted_in_later_runs(tmp_path):
    session, backend = _session(tmp_path)
    first = _run(session)
    assert "saved_found" not in _row(first, "full")  # nothing saved yet, so nothing checked
    _save(session, first, "r2")  # in the full strategy's sample
    _save(session, first, "r1")  # only in [math]'s and [people]'s samples ("gamified" is not game*)
    with pytest.raises(UIError, match="already saved"):
        _save(session, first, "r1")
    with pytest.raises(UIError, match="no record"):
        _save(session, first, "r9")  # never shown in #1
    assert [a["id"] for a in session.view()["saved"]] == ["r1", "r2"]  # newest first
    assert session.view()["pending"]["saved_checks"] == 4  # at most: the full strategy and each block

    backend.sent.clear()
    second = _run(session)
    assert backend.sent == []  # the strategy did not change, so only the saved articles were checked
    full = _row(second, "full")
    assert (full["saved_found"], full["saved_total"], full["saved_missed"]) == (1, 2, [CORPUS["r1"]])
    blocks = {q["block"]: q["by"]["fake"]["saved_found"] for q in second["queries"] if q["role"] == "block"}
    assert blocks == {"math": 2, "game": 1, "people": 2}
    # the full strategy finds r2, so every block does too: only r1 was looked up in them
    assert backend.known_checks[0] == ("math* AND game* AND student*", ["r1", "r2"])
    assert [ids for _, ids in backend.known_checks[1:]] == [["r1"]] * 3
    assert "1/2 saved articles" in second["summary"]
    assert session.view()["pending"]["saved_checks"] == 0

    session.act({"type": "toggle term", "block": 1, "term": 1})  # switch gamif* on, which finds r1
    third = _run(session)
    assert _row(third, "full")["saved_found"] == 2
    assert _row(third, "added")["saved_hits"] == [CORPUS["r1"]] and _row(third, "lost")["saved_found"] == 0

    session.act({"type": "unsave article", "database": "fake", "id": "r1"})
    assert [a["id"] for a in session.saved] == ["r2"]
    later = Session([], [Fake()], SearchOptions(), [], Log(tmp_path / "ui" / "later.jsonl"), None, {"terms file": "x"})
    assert [a["id"] for a in later.saved] == ["r2"]  # kept in results/relevant-articles.json for every session
    assert later.saved[0]["title"] == CORPUS["r2"] and later.saved[0]["entry"] == first["n"]
    kinds = [e["kind"] for e in read_log(session.log.path)]
    assert kinds.count("save article") == 2 and kinds.count("unsave article") == 1


def test_analysis_shows_the_saved_articles_a_string_carries(tmp_path):
    session, _ = _session(tmp_path)
    session.act({"type": "add term", "block": 0, "text": "algebra"})
    first = _run(session)
    _save(session, first, "r2")
    session.start("analyze", 0)
    _wait(session)
    effects = session.log.entries[-1]["effects"]
    assert effects["math*"]["saved"] == {"fake": 1}  # without math*, r2 ("mathematics") is lost
    assert effects["algebra"]["saved"] == {"fake": 0}


def test_a_damaged_saved_file_is_not_overwritten(tmp_path):
    (tmp_path / "relevant-articles.json").write_text("{not json")
    with pytest.raises(ValueError, match="cannot read"):
        _session(tmp_path)


def test_hasse_diagram_of_a_run(tmp_path):
    session, _ = _session(tmp_path)
    session.act({"type": "add term", "block": 0, "text": "algebra"})
    entry = _run(session)
    page = session.hasse_html(entry["n"], "fake", "dark", "/plotly.min.js")
    assert f"<title>Hasse diagram #{entry['n']}</title>" in page and 'src="/plotly.min.js"' in page
    # the full strategy and blocks were searched; each string of [math] is drawn too, without a count
    for label in ("Full strategy", "[math]", "[game]", "math*", "algebra", "not searched on its own"):
        assert label in page
    with pytest.raises(UIError):
        session.hasse_html(1, "fake", "light", "/plotly.min.js")  # the session start entry

    assert "[math] AND [game]" in page  # each pair of the three blocks sits between them and the full strategy

    # the diagram button first searches the strings and block pairs not searched on their own
    assert session.view()["hasse_costs"][entry["n"]] == 5  # math*, algebra and the three pairs of blocks
    assert session.start("hasse", n=entry["n"])
    _wait(session)
    searched = session.log.entries[-1]
    assert searched["kind"] == "hasse" and {q["query"] for q in searched["queries"]} == {
        "math*", "algebra", "(math* OR algebra) AND game*", "(math* OR algebra) AND student*", "game* AND student*"}
    page = session.hasse_html(entry["n"], "fake", "dark", "/plotly.min.js")
    assert "not searched" not in page
    assert session.view()["hasse_costs"][entry["n"]] == 0
    assert session.start("hasse", n=entry["n"]) is False  # nothing left to search

    server = Server(0, session)
    port = server.server_address[1]
    server.hosts = {f"127.0.0.1:{port}"}
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{port}"
        with pytest.raises(urllib.error.HTTPError) as no_token:
            urllib.request.urlopen(f"{base}/hasse/{entry['n']}")
        assert no_token.value.code == 403
        page = urllib.request.urlopen(f"{base}/hasse/{entry['n']}?backend=fake&token={server.token}").read().decode()
        assert "algebra" in page
        assert b"plotly" in urllib.request.urlopen(base + "/plotly.min.js").read(2000).lower()
    finally:
        server.shutdown()
        server.server_close()
