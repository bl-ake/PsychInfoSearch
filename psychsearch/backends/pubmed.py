"""PubMed via NCBI E-utilities (no key needed at <= 3 requests/second)."""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET

from ..query import Node, to_pubmed
from .base import Backend, BackendError, Record, SearchOptions, SearchResult, split_known

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
TOOL = "psychsearch"
MAX_KNOWN = 500


class PubMed(Backend):
    name = "pubmed"
    label = "PubMed"
    coverage = "biomedicine incl. psychiatry, clinical and health psychology; MeSH-indexed"
    min_interval = 0.4

    def render(self, node: Node, opts: SearchOptions) -> str:
        term = to_pubmed(node, opts.field)
        if opts.years:
            term = f"({term}) AND {opts.years[0]}:{opts.years[1]}[dp]"
        return term

    def _call(self, endpoint: str, params: dict) -> str:
        text, _ = self.http.get(
            EUTILS + endpoint, {**params, "db": "pubmed", "tool": TOOL}, min_interval=self.min_interval, post=True
        )
        return text

    def _esearch(self, term: str, retmax: int, sort: str = "relevance") -> dict:
        body = json.loads(self._call("esearch.fcgi", {"term": term, "retmode": "json", "retmax": retmax, "sort": sort}))
        data = body.get("esearchresult", {})
        if error := data.get("ERROR") or body.get("error"):
            raise BackendError(f"PubMed: {error}")
        return data

    def _search(self, node: Node, opts: SearchOptions, result: SearchResult) -> None:
        result.query_sent = self.render(node, opts)
        sort = "pub_date" if opts.sort == "date" else "relevance"
        data = self._esearch(result.query_sent, opts.sample, sort)
        result.count = int(data["count"])
        result.interpretation = data.get("querytranslation", "")
        for kind, values in {**data.get("errorlist", {}), **data.get("warninglist", {})}.items():
            if values:
                result.warnings.append(f"{kind}: {', '.join(values)}")
        if ids := data.get("idlist"):
            result.records = self._fetch(ids)

    def _fetch(self, pmids: list[str]) -> list[Record]:
        root = ET.fromstring(self._call("efetch.fcgi", {"id": ",".join(pmids), "retmode": "xml"}))
        by_id = {}
        for article in root.findall("PubmedArticle"):
            record = _parse_article(article)
            by_id[record.id] = record
        return [by_id[p] for p in pmids if p in by_id]  # books etc. are skipped

    def resolve_known(self, ids: list[str]) -> dict[str, str]:
        dois, pmids = split_known(ids)
        clauses = [f'"{d}"[doi]' for d in dois] + [f"{p}[uid]" for p in pmids]
        if not clauses:
            return {}
        if len(clauses) > MAX_KNOWN:
            raise BackendError(f"PubMed: at most {MAX_KNOWN} known items are supported")
        found = self._esearch(" OR ".join(clauses), len(clauses)).get("idlist", [])
        if not found:
            return {}
        summary = json.loads(self._call("esummary.fcgi", {"id": ",".join(found), "retmode": "json"}))["result"]
        return {pmid: summary.get(pmid, {}).get("title", "") for pmid in found}

    def find_known(self, node: Node, opts: SearchOptions, known_ids: list[str]) -> set[str]:
        ids = " OR ".join(f"{i}[uid]" for i in known_ids)
        return set(self._esearch(f"({self.render(node, opts)}) AND ({ids})", len(known_ids)).get("idlist", []))


def _text(element: ET.Element | None) -> str:
    return " ".join("".join(element.itertext()).split()) if element is not None else ""


def _parse_article(article: ET.Element) -> Record:
    citation = article.find("MedlineCitation")
    art = citation.find("Article")
    pmid = _text(citation.find("PMID"))

    abstract_parts = []
    for part in art.findall("Abstract/AbstractText"):
        label = part.get("Label")
        abstract_parts.append(f"{label}: {_text(part)}" if label else _text(part))

    authors = []
    for author in art.findall("AuthorList/Author"):
        name = " ".join(filter(None, [_text(author.find("LastName")), _text(author.find("Initials"))]))
        authors.append(name or _text(author.find("CollectiveName")))

    pub_date = art.find("Journal/JournalIssue/PubDate")
    year_text = _text(pub_date.find("Year")) or _text(pub_date.find("MedlineDate")) if pub_date is not None else ""
    year = re.search(r"\d{4}", year_text)

    doi = ""
    for article_id in article.findall("PubmedData/ArticleIdList/ArticleId"):
        if article_id.get("IdType") == "doi":
            doi = _text(article_id)

    subjects = [
        _text(d) + ("*" if d.get("MajorTopicYN") == "Y" else "")
        for d in citation.findall("MeshHeadingList/MeshHeading/DescriptorName")
    ]
    return Record(
        id=pmid,
        title=_text(art.find("ArticleTitle")),
        authors=authors,
        year=int(year.group()) if year else None,
        journal=_text(art.find("Journal/Title")),
        doi=doi,
        url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        abstract=" ".join(abstract_parts),
        pub_types=[_text(p) for p in art.findall("PublicationTypeList/PublicationType")],
        subjects=subjects,
        keywords=[_text(k) for k in citation.findall("KeywordList/Keyword")],
        language=_text(art.find("Language")),
    )
