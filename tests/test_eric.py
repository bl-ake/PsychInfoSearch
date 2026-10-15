from psychsearch.backends.eric import Eric, _parse
from psychsearch.backends.base import SearchOptions
from psychsearch.query import parse


def test_render_fielded_and_years():
    sent = Eric(None).render(parse('gamif* AND "math anxiety"'), SearchOptions(years=(2010, 2020)))
    assert "publicationdateyear:[2010 TO 2020]" in sent and "description:(gamif*)" in sent


def test_parse_record():
    record = _parse({"id": "EJ123", "title": "T", "author": ["A, B"], "publicationdateyear": 2015, "source": "J",
                     "description": "abs", "subject": ["Math"], "peerreviewed": "T"})
    assert (record.id, record.year, record.authors, record.url) == ("EJ123", 2015, ["A, B"], "https://eric.ed.gov/?id=EJ123")
    assert record.extra == {"peer_reviewed": True}


def test_date_sort_is_done_locally(monkeypatch):
    docs = [{"id": f"EJ{y}", "publicationdateyear": y} for y in (2001, 2020, 2010)]
    body = '{"response": {"numFound": 500, "docs": %s}}' % __import__("json").dumps(docs)

    class FakeHttp:
        def get(self, url, params, **kw):
            assert "sort" not in params and params["rows"] == 200
            return body, {}

    result = Eric(FakeHttp()).search(parse("x"), SearchOptions(sample=2, sort="date"))
    assert [r.year for r in result.records] == [2020, 2010] and "cannot sort by date" in result.warnings[0]
