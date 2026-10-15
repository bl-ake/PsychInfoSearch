"""EBSCO backend against a fake server shaped like the responses in EBSCO's API documentation."""

import json

import httpx
import pytest

from psychsearch.backends import BackendError, SearchOptions
from psychsearch.backends.ebsco import Ebsco
from psychsearch.http import Http
from psychsearch.query import parse

RECORD = {
    "ResultId": 1,
    "Header": {"DbId": "psyh", "DbLabel": "APA PsycInfo", "An": "2010-05835-004", "PubType": "Academic Journal"},
    "PLink": "https://search.ebscohost.com/login.aspx?direct=true&db=psyh&AN=2010-05835-004",
    "Items": [
        {"Name": "Title", "Label": "Title", "Group": "Ti", "Data": "The effect of mindfulness-based therapy"},
        {"Name": "Abstract", "Label": "Abstract", "Group": "Ab", "Data": "Objective: &lt;i&gt;review&lt;/i&gt; of trials."},
        {
            "Name": "Subject",
            "Label": "Subject Terms",
            "Group": "Su",
            "Data": "&lt;searchLink fieldCode=&quot;DE&quot;&gt;Anxiety&lt;/searchLink&gt;&lt;br /&gt;Mindfulness",
        },
    ],
    "RecordInfo": {
        "BibRecord": {
            "BibEntity": {
                "Identifiers": [{"Type": "doi", "Value": "10.1037/a0018555"}],
                "Languages": [{"Text": "English"}],
                "Subjects": [{"SubjectFull": "Anxiety", "Type": "general"}, {"SubjectFull": "Mindfulness", "Type": "general"}],
                "Titles": [{"TitleFull": "The effect of mindfulness-based therapy on anxiety and depression", "Type": "main"}],
            },
            "BibRelationships": {
                "HasContributorRelationships": [{"PersonEntity": {"Name": {"NameFull": "Hofmann, Stefan G."}}}],
                "IsPartOfRelationships": [
                    {
                        "BibEntity": {
                            "Dates": [{"D": "01", "M": "04", "Type": "published", "Y": "2010"}],
                            "Titles": [{"TitleFull": "Journal of Consulting and Clinical Psychology", "Type": "main"}],
                        }
                    }
                ],
            },
        }
    },
}
OTHER_DB_RECORD = {"ResultId": 2, "Header": {"DbId": "cmedm", "DbLabel": "MEDLINE", "An": "20350028"}}


def search_reply(databases, records):
    return {
        "SearchResult": {
            "Statistics": {"TotalHits": sum(d["Hits"] for d in databases), "TotalSearchTime": 5, "Databases": databases},
            "Data": {"RecordFormat": "EP Display", "Records": records},
        }
    }


class FakeEbsco:
    def __init__(self, reply, expire_auth_once=False):
        self.reply = reply
        self.expire_auth_once = expire_auth_once
        self.calls = []
        self.search_bodies = []
        self.auth_count = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        body = json.loads(request.content or b"{}")
        if path.endswith("/uidauth"):
            assert body == {"UserId": "u", "Password": "p", "InterfaceId": "WSapi"}
            self.auth_count += 1
            return httpx.Response(200, json={"AuthToken": f"auth{self.auth_count}", "AuthTimeout": "1800"})
        if path.endswith("/createsession"):
            assert body["Profile"] == "prof" and request.headers["x-authenticationToken"].startswith("auth")
            return httpx.Response(200, json={"SessionToken": "sess"})
        assert path.endswith("/search") and request.headers["x-sessionToken"] == "sess"
        if self.expire_auth_once and request.headers["x-authenticationToken"] == "auth1":
            return httpx.Response(400, json={"ErrorNumber": "104", "ErrorDescription": "Auth Token Invalid"})
        self.search_bodies.append(body)
        return httpx.Response(200, json=self.reply)


@pytest.fixture
def make_backend(monkeypatch):
    def make(reply, **fake_options):
        for key, value in {"EBSCO_USER_ID": "u", "EBSCO_PASSWORD": "p", "EBSCO_PROFILE": "prof"}.items():
            monkeypatch.setenv(key, value)
        monkeypatch.delenv("EBSCO_DB", raising=False)
        monkeypatch.delenv("EBSCO_AUTH", raising=False)
        fake = FakeEbsco(reply, **fake_options)
        http = Http(cache_dir=None)
        http.client = httpx.Client(transport=httpx.MockTransport(fake))
        backend = Ebsco(http)
        backend.min_interval = 0
        backend.check()
        return backend, fake

    return make


def test_counts_psycinfo_and_parses_records(make_backend):
    databases = [
        {"Id": "psyh", "Label": "APA PsycInfo", "Status": "0", "Hits": 42},
        {"Id": "cmedm", "Label": "MEDLINE", "Status": "0", "Hits": 900},
    ]
    backend, fake = make_backend(search_reply(databases, [RECORD, OTHER_DB_RECORD]))
    opts = SearchOptions(sample=5, years=(2010, 2020))
    result = backend.search(parse('mindful* AND (anxiety OR "panic disorder")'), opts)

    assert result.error == ""
    assert result.count == 42  # PsycInfo's own hits, not the profile total
    assert fake.calls == ["/authservice/rest/uidauth", "/edsapi/rest/createsession", "/edsapi/rest/search"]
    criteria = fake.search_bodies[0]["SearchCriteria"]
    assert criteria["SearchMode"] == "bool"
    assert criteria["Queries"] == [{"Term": '( TI ( mindful* ) OR AB ( mindful* ) ) AND ( TI ( anxiety OR "panic disorder" ) OR AB ( anxiety OR "panic disorder" ) )'}]
    assert criteria["Limiters"] == [{"Id": "DT1", "Values": ["2010-01/2020-12"]}]
    assert fake.search_bodies[0]["RetrievalCriteria"]["ResultsPerPage"] == 5

    (record,) = result.records
    assert record.id == "2010-05835-004"
    assert record.title == "The effect of mindfulness-based therapy on anxiety and depression"
    assert record.authors == ["Hofmann, Stefan G."]
    assert (record.year, record.journal) == (2010, "Journal of Consulting and Clinical Psychology")
    assert record.doi == "10.1037/a0018555"
    assert record.abstract == "Objective: review of trials."
    assert record.subjects == ["Anxiety", "Mindfulness"]
    assert "other databases" in result.warnings[0]


def test_reauthenticates_when_token_expires(make_backend):
    backend, fake = make_backend(search_reply([{"Id": "psyh", "Label": "APA PsycInfo", "Hits": 7}], []), expire_auth_once=True)
    result = backend.search(parse("anxiety"), SearchOptions(sample=0))
    assert (result.error, result.count) == ("", 7)
    assert fake.auth_count == 2


def test_reports_when_psycinfo_is_not_in_the_profile(make_backend):
    backend, _ = make_backend(search_reply([{"Id": "cmedm", "Label": "MEDLINE", "Hits": 3}], []))
    result = backend.search(parse("anxiety"), SearchOptions(sample=0))
    assert result.count is None
    assert "cmedm (MEDLINE)" in result.error


def test_known_items(make_backend):
    backend, fake = make_backend(search_reply([{"Id": "psyh", "Label": "APA PsycInfo", "Hits": 1}], [RECORD]))
    indexed = backend.resolve_known(["10.1037/a0018555", "20350028"])
    assert indexed == {"2010-05835-004": "The effect of mindfulness-based therapy on anxiety and depression"}
    assert fake.search_bodies[0]["SearchCriteria"]["Queries"][0]["Term"] == 'DI "10.1037/a0018555" OR PM 20350028'
    assert backend.find_known(parse("anxiety"), SearchOptions(), list(indexed)) == {"2010-05835-004"}
    assert fake.search_bodies[1]["SearchCriteria"]["Queries"][0]["Term"] == '( ( TI ( anxiety ) OR AB ( anxiety ) ) ) AND ( AN "2010-05835-004" )'


def test_check_requires_credentials(monkeypatch):
    for key in ("EBSCO_USER_ID", "EBSCO_PASSWORD", "EBSCO_PROFILE", "EBSCO_AUTH"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(BackendError, match="EBSCO_PROFILE"):
        Ebsco(Http(cache_dir=None)).check()
