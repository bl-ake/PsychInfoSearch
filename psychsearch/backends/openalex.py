"""OpenAlex works search. Works without a key on a small daily allowance (about 100 searches);
set OPENALEX_API_KEY (free) to raise it."""

from __future__ import annotations

import json
import os

from ..http import HttpError
from ..query import Node, to_openalex
from .base import Backend, BackendError, Record, SearchOptions, SearchResult, chunks, split_known

WORKS = "https://api.openalex.org/works"
SEARCH_FIELD = {
    "tiab": "title_and_abstract.search.exact",
    "title": "title.search.exact",
    "all": "default.search.exact",
}
SELECT = (
    "id,doi,title,publication_year,language,type,primary_location,authorships,cited_by_count,"
    "open_access,primary_topic,topics,keywords,mesh,abstract_inverted_index,is_retracted"
)
FACETS = {"year": "publication_year", "type": "type", "field": "primary_topic.field.id"}
KNOWN_CHUNK = 50


class OpenAlex(Backend):
    name = "openalex"
    label = "OpenAlex"
    coverage = "broadest: ~all scholarly works incl. psychology/social-science journals outside MEDLINE, books, theses"
    min_interval = 0.15

    def __init__(self, http):
        super().__init__(http)
        self.api_key = os.environ.get("OPENALEX_API_KEY", "")
        self.remaining_usd: float | None = None
        self._exhausted = ""

    def _call(self, params: dict) -> dict:
        if self._exhausted:
            raise BackendError(self._exhausted)
        if self.api_key:
            params = {**params, "api_key": self.api_key}
        try:
            text, headers = self.http.get(WORKS, params, min_interval=self.min_interval, secret=("api_key",))
        except HttpError as exc:
            if exc.status == 429:
                reset = (exc.headers or {}).get("x-ratelimit-reset", "?")
                self._exhausted = (
                    f"OpenAlex daily allowance used up (resets in {reset}s). "
                    "Set OPENALEX_API_KEY for a larger one, or rerun later; finished queries are cached."
                )
                raise BackendError(self._exhausted) from None
            raise
        if headers is not None and "x-ratelimit-remaining-usd" in headers:
            self.remaining_usd = float(headers["x-ratelimit-remaining-usd"])
        return json.loads(text)

    def render(self, node: Node, opts: SearchOptions, warnings: list[str] | None = None) -> str:
        flt = f"{SEARCH_FIELD[opts.field]}:{to_openalex(node, warnings)}"
        if opts.years:
            flt += f",publication_year:{opts.years[0]}-{opts.years[1]}"
        return flt

    def _search(self, node: Node, opts: SearchOptions, result: SearchResult) -> None:
        flt = self.render(node, opts, result.warnings)
        result.query_sent = flt
        sort = "publication_date:desc" if opts.sort == "date" else "relevance_score:desc"
        data = self._call({"filter": flt, "per-page": max(opts.sample, 1), "sort": sort, "select": SELECT})
        result.count = data["meta"]["count"]
        result.records = [_parse(work) for work in data["results"][: opts.sample]]
        if opts.facets and result.count:
            for name, group_by in FACETS.items():
                groups = self._call({"filter": flt, "group_by": group_by})["group_by"]
                pairs = [(str(g.get("key_display_name") or g["key"]), g["count"]) for g in groups]
                if name == "year":
                    pairs.sort(key=lambda p: p[0], reverse=True)
                result.facets[name] = pairs

    def resolve_known(self, ids: list[str]) -> dict[str, str]:
        dois, pmids = split_known(ids)
        found = {}
        for attribute, values in (("doi", dois), ("pmid", pmids)):
            for chunk in chunks(values, KNOWN_CHUNK):
                data = self._call({"filter": f"{attribute}:{'|'.join(chunk)}", "per-page": 200, "select": "id,title"})
                for work in data["results"]:
                    found[_short_id(work["id"])] = work.get("title") or ""
        return found

    def find_known(self, node: Node, opts: SearchOptions, known_ids: list[str]) -> set[str]:
        found = set()
        for chunk in chunks(known_ids, KNOWN_CHUNK):
            flt = f"{self.render(node, opts)},openalex:{'|'.join(chunk)}"
            data = self._call({"filter": flt, "per-page": 200, "select": "id"})
            found.update(_short_id(work["id"]) for work in data["results"])
        return found


def _short_id(url: str) -> str:
    return url.rsplit("/", 1)[-1]


def _abstract(inverted: dict | None) -> str:
    if not inverted:
        return ""
    positions = sorted((pos, word) for word, places in inverted.items() for pos in places)
    return " ".join(word for _, word in positions)


def _parse(work: dict) -> Record:
    doi = (work.get("doi") or "").removeprefix("https://doi.org/")
    source = ((work.get("primary_location") or {}).get("source") or {}).get("display_name") or ""
    primary = work.get("primary_topic") or {}
    topics = [t["display_name"] for t in work.get("topics") or []]
    mesh = [m["descriptor_name"] + ("*" if m.get("is_major_topic") else "") for m in work.get("mesh") or []]
    return Record(
        id=_short_id(work["id"]),
        title=work.get("title") or "",
        authors=[a["author"]["display_name"] for a in work.get("authorships") or [] if a.get("author")],
        year=work.get("publication_year"),
        journal=source,
        doi=doi,
        url=work.get("doi") or work["id"],
        abstract=_abstract(work.get("abstract_inverted_index")),
        pub_types=[work["type"]] if work.get("type") else [],
        subjects=topics + mesh,
        keywords=[k["display_name"] for k in work.get("keywords") or []],
        language=work.get("language") or "",
        cited_by=work.get("cited_by_count"),
        is_oa=(work.get("open_access") or {}).get("is_oa"),
        extra={
            "field": (primary.get("field") or {}).get("display_name", ""),
            "subfield": (primary.get("subfield") or {}).get("display_name", ""),
            "is_retracted": work.get("is_retracted", False),
        },
    )
