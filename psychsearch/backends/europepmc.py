"""Europe PMC REST search (no key needed)."""

from __future__ import annotations

import json
import re

from ..query import Node, to_europepmc
from .base import Backend, Record, SearchOptions, SearchResult, chunks, split_known

SEARCH = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
KNOWN_CHUNK = 40


class EuropePMC(Backend):
    name = "europepmc"
    label = "Europe PMC"
    coverage = "PubMed plus preprints (incl. PsyArXiv), Agricola and patents; citation counts"
    min_interval = 0.25
    known_chunk = KNOWN_CHUNK

    def render(self, node: Node, opts: SearchOptions) -> str:
        query = to_europepmc(node, opts.field)
        if opts.years:
            query = f"({query}) AND PUB_YEAR:[{opts.years[0]} TO {opts.years[1]}]"
        return query

    def _call(self, query: str, page_size: int, sort: str = "", result_type: str = "core") -> dict:
        params = {"query": query, "format": "json", "pageSize": max(page_size, 1), "resultType": result_type}
        if sort:
            params["sort"] = sort
        text, _ = self.http.get(SEARCH, params, min_interval=self.min_interval)
        return json.loads(text)

    def _search(self, node: Node, opts: SearchOptions, result: SearchResult) -> None:
        result.query_sent = self.render(node, opts)
        data = self._call(result.query_sent, opts.sample, "P_PDATE_D desc" if opts.sort == "date" else "")
        result.count = int(data["hitCount"])
        hits = data.get("resultList", {}).get("result", [])
        result.records = [_parse(hit) for hit in hits[: opts.sample]]

    def _lookup(self, query: str, size: int) -> list[dict]:
        return self._call(query, size, result_type="lite").get("resultList", {}).get("result", [])

    def resolve_known(self, ids: list[str]) -> dict[str, str]:
        dois, pmids = split_known(ids)
        clauses = [f'DOI:"{d}"' for d in dois] + [f"(EXT_ID:{p} AND SRC:MED)" for p in pmids]
        found = {}
        for chunk in chunks(clauses, KNOWN_CHUNK):
            for hit in self._lookup(" OR ".join(chunk), len(chunk) * 3):
                found[f"{hit['source']}:{hit['id']}"] = hit.get("title", "")
        return found

    def find_known(self, node: Node, opts: SearchOptions, known_ids: list[str]) -> set[str]:
        query = self.render(node, opts)
        found = set()
        for chunk in chunks(known_ids, KNOWN_CHUNK):
            clauses = []
            for known in chunk:
                source, ext_id = known.split(":", 1)
                clauses.append(f"(EXT_ID:{ext_id} AND SRC:{source})")
            for hit in self._lookup(f"({query}) AND ({' OR '.join(clauses)})", len(chunk)):
                found.add(f"{hit['source']}:{hit['id']}")
        return found


def _parse(hit: dict) -> Record:
    journal = ((hit.get("journalInfo") or {}).get("journal") or {}).get("title", "")
    book = hit.get("bookOrReportDetails") or {}
    mesh = (hit.get("meshHeadingList") or {}).get("meshHeading", [])
    year = hit.get("pubYear", "")
    return Record(
        id=f"{hit.get('source')}:{hit.get('id')}",
        title=re.sub(r"<[^>]+>", "", hit.get("title", "")),
        authors=[a.strip(" .") for a in hit.get("authorString", "").split(",") if a.strip(" .")],
        year=int(year) if year.isdigit() else None,
        journal=journal or book.get("publisher", ""),
        doi=hit.get("doi", ""),
        url=f"https://europepmc.org/article/{hit.get('source')}/{hit.get('id')}",
        abstract=" ".join(re.sub(r"<[^>]+>", " ", hit.get("abstractText", "")).split()),
        pub_types=(hit.get("pubTypeList") or {}).get("pubType", []),
        subjects=[m["descriptorName"] + ("*" if m.get("majorTopic_YN") == "Y" else "") for m in mesh],
        keywords=(hit.get("keywordList") or {}).get("keyword", []),
        language=hit.get("language", ""),
        cited_by=hit.get("citedByCount"),
        is_oa=hit.get("isOpenAccess") == "Y",
    )
