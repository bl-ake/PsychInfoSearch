"""ERIC (Education Resources Information Center) through its free public API; no key needed."""

from __future__ import annotations

import html
import json
import re

from ..query import Node, to_eric
from .base import Backend, BackendError, Record, SearchOptions, SearchResult, chunks

SEARCH = "https://api.ies.ed.gov/eric/"
DATE_WINDOW = 200
KNOWN_CHUNK = 50
_ERIC_ID = re.compile(r"E[DJ]\d+")
FIELDS = "id,title,author,source,publicationdateyear,description,subject,peerreviewed,url,publicationtype,language"


class Eric(Backend):
    name = "eric"
    label = "ERIC"
    coverage = "education research: journal articles, reports and theses from the U.S. Dept. of Education's ERIC index"
    min_interval = 0.5
    known_chunk = KNOWN_CHUNK

    def render(self, node: Node, opts: SearchOptions) -> str:
        query = to_eric(node, opts.field)
        if opts.years:
            query = f"({query}) AND publicationdateyear:[{opts.years[0]} TO {opts.years[1]}]"
        return query

    def _search(self, node: Node, opts: SearchOptions, result: SearchResult) -> None:
        result.query_sent = self.render(node, opts)
        # The API has no sort option, so date order means sorting the best DATE_WINDOW matches here.
        rows = DATE_WINDOW if opts.sort == "date" else max(opts.sample, 1)
        params = {"search": result.query_sent, "format": "json", "rows": rows, "fields": FIELDS}
        text, _ = self.http.get(SEARCH, params, min_interval=self.min_interval)
        try:
            response = json.loads(text)["response"]
            result.count = int(response["numFound"])
            docs = response.get("docs", [])
        except (KeyError, TypeError, ValueError) as exc:
            raise BackendError(f"ERIC: unexpected response ({exc!r})") from None
        records = [_parse(doc) for doc in docs]
        if opts.sort == "date":
            records.sort(key=lambda r: r.year or 0, reverse=True)
            if result.count > len(records):
                result.warnings.append(f"ERIC cannot sort by date: the sample is the newest of the first {len(records)} matches, not of all {result.count}")
        result.records = records[: opts.sample]

    def resolve_known(self, ids: list[str]) -> dict[str, str]:
        return {}  # known-item lookup works on DOIs and PMIDs, which ERIC does not index

    def find_known(self, node: Node, opts: SearchOptions, known_ids: list[str]) -> set[str]:
        """Which of these ERIC ids (EJ/ED numbers, as saved from ERIC's own results) the query retrieves."""
        ids = [i for i in known_ids if _ERIC_ID.fullmatch(i)]
        found = set()
        for chunk in chunks(ids, KNOWN_CHUNK):
            params = {"search": f"({self.render(node, opts)}) AND id:({' OR '.join(chunk)})", "format": "json",
                      "rows": len(chunk), "fields": "id"}
            text, _ = self.http.get(SEARCH, params, min_interval=self.min_interval)
            try:
                found.update(str(doc["id"]) for doc in json.loads(text)["response"].get("docs", []))
            except (KeyError, TypeError, ValueError) as exc:
                raise BackendError(f"ERIC: unexpected response ({exc!r})") from None
        return found & set(ids)


def _list(value) -> list[str]:
    if not value:
        return []
    return [str(v) for v in value] if isinstance(value, list) else [str(value)]


def _parse(doc: dict) -> Record:
    eric_id = str(doc.get("id", ""))
    year = doc.get("publicationdateyear")
    peer = str(doc.get("peerreviewed", "")).lower()
    return Record(
        id=eric_id,
        title=html.unescape(doc.get("title") or ""),
        authors=_list(doc.get("author")),
        year=int(year) if str(year).isdigit() else None,
        journal=doc.get("source") or "",
        url=doc.get("url") or (f"https://eric.ed.gov/?id={eric_id}" if eric_id else ""),
        abstract=html.unescape(doc.get("description") or ""),
        pub_types=_list(doc.get("publicationtype")),
        subjects=_list(doc.get("subject")),
        language=", ".join(_list(doc.get("language"))),
        extra={"peer_reviewed": peer in ("t", "true", "yes")} if peer else {},
    )
