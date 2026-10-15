"""APA PsycInfo itself, through the EBSCO Discovery Service (EDS) API.

This is EBSCO's sanctioned route for scripted searching; it needs API credentials that the
library (or EBSCO Support) issues for your institution. Configure with environment variables
or a .env file:

    EBSCO_USER_ID, EBSCO_PASSWORD   API user (not your personal library login)
    EBSCO_PROFILE                   API profile id
    EBSCO_AUTH=ip                   instead of user/password, when on a registered campus IP
    EBSCO_DB                        database short name to count (default psyh = APA PsycInfo);
                                    "all" counts every database in the profile

Written from EBSCO's published API documentation (developer.ebsco.com/eds-api).
"""

from __future__ import annotations

import html
import json
import os
import re

from ..http import HttpError
from ..query import Node, to_ebsco
from .base import Backend, BackendError, Record, SearchOptions, SearchResult, chunks, split_known

AUTH = "https://eds-api.ebscohost.com/authservice/rest/"
API = "https://eds-api.ebscohost.com/edsapi/rest/"
MAX_PER_PAGE = 100
KNOWN_CHUNK = 40
AUTH_EXPIRED = {"104", "107"}
SESSION_EXPIRED = {"108", "109"}


class Ebsco(Backend):
    name = "ebsco"
    label = "PsycInfo (EBSCO API)"
    coverage = "APA PsycInfo itself, via your institution's EBSCO API profile (credentials required, see README)"
    min_interval = 0.2

    def __init__(self, http):
        super().__init__(http)
        env = os.environ
        self.user = env.get("EBSCO_USER_ID", "")
        self.password = env.get("EBSCO_PASSWORD", "")
        self.profile = env.get("EBSCO_PROFILE", "")
        self.use_ip = env.get("EBSCO_AUTH", "").lower() == "ip"
        self.db = env.get("EBSCO_DB", "psyh").lower()
        self._auth_token = ""
        self._session_token = ""

    def check(self) -> None:
        needed = {"EBSCO_PROFILE": self.profile}
        if not self.use_ip:
            needed |= {"EBSCO_USER_ID": self.user, "EBSCO_PASSWORD": self.password}
        if missing := [name for name, value in needed.items() if not value]:
            raise BackendError(
                f"the ebsco backend needs {', '.join(missing)} (environment or .env file). "
                "These are API credentials from your library; see 'Real PsycInfo counts through EBSCO' in README.md."
            )

    # ------------------------------------------------------------ tokens

    def _post(self, url: str, body: dict, headers: dict | None = None, cache: bool = False) -> dict:
        text, _ = self.http.get(
            url,
            {},
            json_body=body,
            headers={"Accept": "application/json", **(headers or {})},
            min_interval=self.min_interval,
            cache=cache,
            cache_scope=f"{self.profile}/{self.db}",
        )
        return json.loads(text)

    def _authenticate(self) -> None:
        if self.use_ip:
            reply = self._post(AUTH + "ipauth", {})
        else:
            reply = self._post(AUTH + "uidauth", {"UserId": self.user, "Password": self.password, "InterfaceId": "WSapi"})
        self._auth_token = reply["AuthToken"]

    def _start_session(self) -> None:
        reply = self._post(API + "createsession", {"Profile": self.profile, "Guest": "n"}, {"x-authenticationToken": self._auth_token})
        self._session_token = reply["SessionToken"]

    def _call(self, body: dict) -> dict:
        for attempt in range(3):
            try:
                if not self._auth_token:
                    self._authenticate()
                if not self._session_token:
                    self._start_session()
                tokens = {"x-authenticationToken": self._auth_token, "x-sessionToken": self._session_token}
                return self._post(API + "search", body, tokens, cache=True)
            except HttpError as exc:
                number, message = _api_error(exc)
                if number in AUTH_EXPIRED and attempt < 2:
                    self._auth_token = ""
                elif number in SESSION_EXPIRED and attempt < 2:
                    self._session_token = ""
                else:
                    raise BackendError(f"EBSCO: {message}") from None
        raise BackendError("EBSCO: could not obtain a valid session")

    # ------------------------------------------------------------ search

    def render(self, node: Node, opts: SearchOptions) -> str:
        return to_ebsco(node, opts.field)

    def _body(self, term: str, opts: SearchOptions, page_size: int, view: str) -> dict:
        criteria = {
            "Queries": [{"Term": term}],
            "SearchMode": "bool",
            "IncludeFacets": "n",
            "Sort": "date" if opts.sort == "date" else "relevance",
            "AutoSuggest": "n",
            "AutoCorrect": "n",
        }
        if opts.years:
            criteria["Limiters"] = [{"Id": "DT1", "Values": [f"{opts.years[0]}-01/{opts.years[1]}-12"]}]
        retrieval = {
            "View": view,
            "ResultsPerPage": max(1, min(page_size, MAX_PER_PAGE)),
            "PageNumber": 1,
            "Highlight": "n",
            "IncludeImageQuickView": "n",
        }
        return {"SearchCriteria": criteria, "RetrievalCriteria": retrieval, "Actions": None}

    def _records(self, data: dict) -> tuple[list[dict], int]:
        """Records from the configured database, and how many on the page came from elsewhere."""
        records = ((data.get("SearchResult") or {}).get("Data") or {}).get("Records") or []
        if self.db == "all":
            return records, 0
        mine = [r for r in records if (r.get("Header") or {}).get("DbId", "").lower() == self.db]
        return mine, len(records) - len(mine)

    def _search(self, node: Node, opts: SearchOptions, result: SearchResult) -> None:
        result.query_sent = self.render(node, opts)
        data = self._call(self._body(result.query_sent, opts, opts.sample, "detailed"))
        stats = (data.get("SearchResult") or {}).get("Statistics") or {}
        total = int(stats.get("TotalHits") or 0)
        databases = stats.get("Databases") or []
        if self.db == "all":
            result.count = total
        else:
            match = [d for d in databases if str(d.get("Id", "")).lower() == self.db]
            if match:
                result.count = int(match[0].get("Hits") or 0)
            elif total == 0:
                result.count = 0
            else:
                listed = ", ".join(f"{d.get('Id')} ({d.get('Label')})" for d in databases) or "none listed"
                raise BackendError(
                    f"EBSCO: database '{self.db}' is not among those this profile searched: {listed}. "
                    "Set EBSCO_DB to the PsycInfo id shown there, or ask for PsycInfo to be added to the profile."
                )
        mine, others = self._records(data)
        result.records = [_parse(r) for r in mine[: opts.sample]]
        if others:
            result.warnings.append(
                f"{others} of the {others + len(mine)} sampled records came from other databases in this profile "
                "and were left out; a PsycInfo-only profile gives a full sample"
            )
        if opts.sample > MAX_PER_PAGE:
            result.warnings.append(f"EBSCO returns at most {MAX_PER_PAGE} records per request")

    # ------------------------------------------------------- known items

    def resolve_known(self, ids: list[str]) -> dict[str, str]:
        dois, pmids = split_known(ids)
        clauses = [f'DI "{d}"' for d in dois] + [f"PM {p}" for p in pmids]
        found = {}
        for chunk in chunks(clauses, KNOWN_CHUNK):
            data = self._call(self._body(" OR ".join(chunk), SearchOptions(), MAX_PER_PAGE, "title"))
            for raw in self._records(data)[0]:
                record = _parse(raw)
                found[record.id] = record.title
        return found

    def find_known(self, node: Node, opts: SearchOptions, known_ids: list[str]) -> set[str]:
        found = set()
        for chunk in chunks(known_ids, KNOWN_CHUNK):
            # Quoted: on the EBSCOhost site an unquoted accession number matches loosely.
            term = f"( {self.render(node, opts)} ) AND ( {' OR '.join(f'AN "{an}"' for an in chunk)} )"
            data = self._call(self._body(term, opts, MAX_PER_PAGE, "title"))
            found.update(_parse(raw).id for raw in self._records(data)[0])
        return found & set(known_ids)


def _api_error(exc: HttpError) -> tuple[str, str]:
    """(EBSCO error number, readable message) from a failed call."""
    try:
        payload = json.loads(exc.body)
    except (ValueError, TypeError):
        return "", str(exc)
    if not isinstance(payload, dict):
        return "", str(exc)
    number = str(payload.get("ErrorNumber") or payload.get("ErrorCode") or "")
    parts = [payload.get("ErrorDescription") or payload.get("Reason"), payload.get("DetailedErrorDescription")]
    message = "; ".join(p for p in parts if p) or str(exc)
    return number, f"error {number}: {message}" if number else message


def _clean(data: str) -> str:
    text = html.unescape(data or "")
    text = re.sub(r"<br\s*/?>", "; ", text)
    return " ".join(re.sub(r"<[^>]+>", "", text).split())


def _first(entries: list | None, key: str) -> str:
    return (entries[0].get(key) or "") if entries else ""


def _parse(raw: dict) -> Record:
    header = raw.get("Header") or {}
    items: dict[str, list[str]] = {}
    for item in raw.get("Items") or []:
        items.setdefault(item.get("Name", ""), []).append(_clean(item.get("Data", "")))

    bib = (raw.get("RecordInfo") or {}).get("BibRecord") or {}
    entity = bib.get("BibEntity") or {}
    relations = bib.get("BibRelationships") or {}
    part_of = relations.get("IsPartOfRelationships") or []
    container = (part_of[0].get("BibEntity") or {}) if part_of else {}

    authors = []
    for contributor in relations.get("HasContributorRelationships") or []:
        name = ((contributor.get("PersonEntity") or {}).get("Name") or {}).get("NameFull")
        if name:
            authors.append(name)
    year = _first(container.get("Dates"), "Y")
    doi = next((i.get("Value", "") for i in entity.get("Identifiers") or [] if i.get("Type") == "doi"), "")
    subjects = [s["SubjectFull"] for s in entity.get("Subjects") or [] if s.get("SubjectFull")]

    return Record(
        id=header.get("An", ""),
        title=_first(entity.get("Titles"), "TitleFull") or " ".join(items.get("Title", [])),
        authors=authors,
        year=int(year) if year.isdigit() else None,
        journal=_first(container.get("Titles"), "TitleFull"),
        doi=doi,
        url=raw.get("PLink", ""),
        abstract=" ".join(items.get("Abstract", [])),
        pub_types=[header["PubType"]] if header.get("PubType") else [],
        subjects=subjects,
        keywords=[k for entry in items.get("Keyword", []) for k in entry.split("; ") if k],
        language=_first(entity.get("Languages"), "Text"),
        extra={"database": header.get("DbLabel", "")},
    )
