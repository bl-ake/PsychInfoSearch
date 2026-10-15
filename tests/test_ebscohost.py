"""EBSCOhost website backend against a fake browser returning page states as the real page script does."""

import pytest

from psychsearch.backends import BackendError, SearchOptions
from psychsearch.backends.ebscohost import CONSENT_PENDING_JS, EbscoHost
from psychsearch.http import Http
from psychsearch.query import parse

BASE = "https://research-ebsco-com.ezproxy.example.edu/c/abc123"
ITEM = {
    "id": "2010-05835-004-psyh",
    "title": "The effect of mindfulness-based therapy on anxiety and depression: A meta-analytic review.",
    "href": f"{BASE}/search/details/xyz?db=psyh",
    "authors": ["Hofmann, Stefan G.", "Sawyer, Alice T."],
    "published": ["Journal of Consulting and Clinical Psychology", "Apr 2010", "volume 78", "issue 2"],
    "publishedText": "Journal of Consulting and Clinical Psychology, Apr 2010, volume 78, issue 2",
    "database": "APA PsycInfo",
    "abstract": "Objective: review.",
    "subjects": ["Anxiety", "Mindfulness"],
    "types": ["Journal, Peer Reviewed Journal"],
    "peerReviewed": True,
    "citedBy": "Cited by: 2,345".replace(",", ""),
}


def page(count=None, items=(), url=f"{BASE}/search/results?q=x", **extra):
    return {"count": count, "noResults": False, "databases": "APA PsycInfo", "filters": [], "title": "t", "url": url, "items": list(items), **extra}


class FakeBiDi:
    """Shows states[i] for as long as the i-th page load is on screen."""

    def __init__(self, states, consent=(), ready=()):
        self.states = list(states)
        self.consent = list(consent)  # answers to "is the cookie banner showing?"
        self.ready = list(ready)  # answers to the login wait's {form, consent} check
        self.visited = []
        self.fronted = 0
        self.alerts = 0
        self.clicked_after = None  # the user clicks Continue once this many alerts have been drawn

    def bring_to_front(self, context=None):
        self.fronted += 1

    def tabs(self):
        return [{"context": "ctx", "url": f"{BASE}/search?db=psyh"}]

    def navigate(self, context, url):
        self.visited.append(url)

    def evaluate(self, context, script):
        if script == f"return {CONSENT_PENDING_JS}":
            return self.consent.pop(0) if self.consent else False
        if script.startswith("return {form:"):
            return self.ready.pop(0)
        if "psychsearch-alert" in script:
            self.alerts += 1
            return self.clicked_after is not None and self.alerts >= self.clicked_after
        return self.states[len(self.visited) - 1]

    def close(self):
        pass


@pytest.fixture
def make_backend(monkeypatch):
    def make(states, **env):
        for key in ("EBSCOHOST_URL", "EBSCOHOST_DB", "EBSCOHOST_DELAY", "EBSCOHOST_MAX_SEARCHES"):
            monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        backend = EbscoHost(Http(cache_dir=None))
        backend.delay = 0
        backend._page, backend._context, backend._base = FakeBiDi(states), "ctx", BASE
        return backend

    return make


def test_search_reads_count_and_records(make_backend):
    backend = make_backend([page("1-10 of 7,061 total results", [ITEM])])
    result = backend.search(parse('mindful* AND "panic disorder"'), SearchOptions(sample=5, years=(2010, 2020), sort="date"))

    assert (result.error, result.count) == ("", 7061)
    assert backend._page.visited == [
        f"{BASE}/search/results?q=%28%20TI%20%28%20mindful%2A%20%29%20OR%20AB%20%28%20mindful%2A%20%29%20%29%20AND%20"
        "%28%20TI%20%28%20%22panic%20disorder%22%20%29%20OR%20AB%20%28%20%22panic%20disorder%22%20%29%20%29"
        "&db=psyh&searchMode=boolean&expanders=None&autocorrect=n&limiters=DT1%3A2010-01-01%2F2020-12-31&sort=date%3Adesc"
    ]
    (record,) = result.records
    assert record.id == "2010-05835-004"
    assert (record.year, record.journal) == (2010, "Journal of Consulting and Clinical Psychology")
    assert record.authors == ["Hofmann, Stefan G.", "Sawyer, Alice T."]
    assert record.pub_types == ["Journal", "Peer Reviewed Journal"]
    assert record.cited_by == 2345


def test_no_results_is_zero(make_backend):
    backend = make_backend([page(noResults=True)])
    assert backend.search(parse("zzqxqzz"), SearchOptions()).count == 0


@pytest.fixture(autouse=True)
def quick_waits(monkeypatch):
    monkeypatch.setattr("psychsearch.backends.ebscohost.LOGIN_WAIT", 0.3)
    monkeypatch.setattr("psychsearch.backends.ebscohost.time.sleep", lambda _s: None)


def test_unexpected_page_shows_alert_and_continues_when_cleared(make_backend, monkeypatch):
    monkeypatch.setattr("psychsearch.backends.ebscohost.LOAD_WAIT", 0.05)
    login = page(url="https://login.example.edu/sso", title="Sign in", text="Please sign in")
    backend = make_backend([login, page("1-10 of 9 total results", [ITEM])])
    backend._page.clicked_after = 2
    result = backend.search(parse("anxiety"), SearchOptions())
    assert (result.error, result.count) == ("", 9)
    assert backend._page.alerts >= 2 and backend._page.fronted >= 1
    assert len(backend._page.visited) == 2


def test_stops_for_good_when_page_is_not_results(make_backend, monkeypatch):
    monkeypatch.setattr("psychsearch.backends.ebscohost.LOAD_WAIT", 0.05)
    login = page(url="https://login.example.edu/sso", title="Sign in", text="Please sign in")
    backend = make_backend([login] * 5)
    first = backend.search(parse("anxiety"), SearchOptions())
    second = backend.search(parse("depression"), SearchOptions())
    assert "no results page appeared" in first.error and "login.example.edu" in first.error
    assert second.error == first.error
    assert len(backend._page.visited) == 1  # the second search never touched the site


BLANK = page(title="", text="", blank=True)


def test_blank_page_is_loaded_once_more(make_backend, monkeypatch):
    monkeypatch.setattr("psychsearch.backends.ebscohost.LOAD_WAIT", 0.05)
    backend = make_backend([BLANK, page("1-10 of 42 total results", [ITEM])])
    result = backend.search(parse("gamifi*"), SearchOptions())
    assert (result.error, result.count) == ("", 42)
    assert len(backend._page.visited) == 2 and backend._page.visited[0] == backend._page.visited[1]
    assert backend.searches == 2  # the retry counts towards the cap


def test_page_blank_twice_stops_the_run(make_backend, monkeypatch):
    monkeypatch.setattr("psychsearch.backends.ebscohost.LOAD_WAIT", 0.05)
    backend = make_backend([BLANK, BLANK, page("1-1 of 1 total results", [ITEM])])
    first = backend.search(parse("gamifi*"), SearchOptions())
    assert "stayed blank" in first.error and "twice" in first.error
    assert backend.search(parse("anxiety"), SearchOptions()).error == first.error
    assert len(backend._page.visited) == 2


BANNER = page(title="", text="", blank=True, consentPending=True)


def test_waits_for_the_cookie_banner_then_reloads(make_backend, monkeypatch, capsys):
    monkeypatch.setattr("psychsearch.backends.ebscohost.LOAD_WAIT", 0.05)
    monkeypatch.setattr("psychsearch.backends.ebscohost.time.sleep", lambda s: None)
    backend = make_backend([BANNER, page("1-10 of 42 total results", [ITEM])])
    backend._page.consent = [True, True, False]  # answered on the third look
    result = backend.search(parse("gamifi*"), SearchOptions())
    assert (result.error, result.count) == ("", 42)
    assert len(backend._page.visited) == 2 and backend._page.consent == []
    assert "cookie banner" in capsys.readouterr().err


def test_unanswered_cookie_banner_stops_the_run(make_backend, monkeypatch):
    monkeypatch.setattr("psychsearch.backends.ebscohost.LOAD_WAIT", 0.05)
    monkeypatch.setattr("psychsearch.backends.ebscohost.LOGIN_WAIT", -1)
    monkeypatch.setattr("psychsearch.backends.ebscohost.time.sleep", lambda s: None)
    backend = make_backend([BANNER, BANNER])
    result = backend.search(parse("gamifi*"), SearchOptions())
    assert "cookie banner" in result.error and "still unanswered" in result.error
    assert len(backend._page.visited) == 2


def test_searches_wait_for_the_cookie_banner_on_the_search_page(make_backend, monkeypatch, capsys):
    monkeypatch.setattr("psychsearch.backends.ebscohost.time.sleep", lambda s: None)
    backend = make_backend([])
    backend._page.ready = [{"form": False, "consent": False}, {"form": True, "consent": True}, {"form": True, "consent": False}]
    backend._wait_for_login()
    assert backend._page.ready == []
    err = capsys.readouterr().err
    assert "Waiting for you: Log in to EBSCO" in err and "Waiting for you: Answer EBSCO's cookie banner" in err
    assert err.rstrip().endswith("Done; continuing.")
    assert backend._page.fronted == 2  # once per thing asked


def test_waiting_for_you_is_repeated_and_has_a_limit(make_backend, monkeypatch, capsys):
    clock = iter(range(0, 10_000, 40))  # each look at the clock is 40 s later
    monkeypatch.setattr("psychsearch.backends.ebscohost.time.monotonic", lambda: next(clock))
    monkeypatch.setattr("psychsearch.backends.ebscohost.time.sleep", lambda s: None)
    monkeypatch.setattr("psychsearch.backends.ebscohost.LOGIN_WAIT", 300)
    backend = make_backend([])
    backend._page.tabs = lambda: [{"context": "ctx", "url": "https://login.example.edu/sso"}]
    with pytest.raises(BackendError, match="gave up after 5 minutes. Still needed: Log in to EBSCO"):
        backend._wait_for_login()
    err = capsys.readouterr().err
    assert err.count("Waiting for you:") == 1 and err.count("Still waiting for you to log in to EBSCO") >= 3
    assert "minutes left" in err and "\a" in err


def test_no_retry_at_the_search_cap(make_backend, monkeypatch):
    monkeypatch.setattr("psychsearch.backends.ebscohost.LOAD_WAIT", 0.05)
    backend = make_backend([BLANK, page("1-1 of 1 total results", [ITEM])], EBSCOHOST_MAX_SEARCHES="1")
    assert "limit of 1 searches" in backend.search(parse("gamifi*"), SearchOptions()).error
    assert len(backend._page.visited) == 1


def test_search_cap(make_backend):
    backend = make_backend([page("1-1 of 1 total results", [ITEM])] * 3, EBSCOHOST_MAX_SEARCHES="1")
    assert backend.search(parse("anxiety"), SearchOptions()).count == 1
    assert "limit of 1 searches" in backend.search(parse("depression"), SearchOptions()).error


def test_known_items_use_quoted_accession_numbers(make_backend):
    stray = {**ITEM, "id": "2021-24678-001-psyh"}
    backend = make_backend([page("1-1 of 1 total results", [ITEM]), page("1-2 of 2 total results", [ITEM, stray])])
    indexed = backend.resolve_known(["10.1037/a0018555", "20350028"])
    assert list(indexed) == ["2010-05835-004"]
    assert backend.find_known(parse("anxiety"), SearchOptions(), list(indexed)) == {"2010-05835-004"}
    assert "AN%20%222010-05835-004%22" in backend._page.visited[1]
