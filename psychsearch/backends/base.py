from __future__ import annotations

from dataclasses import dataclass, field

from ..http import Http, HttpError
from ..query import Node

MAX_SAMPLE = 200


class BackendError(Exception):
    pass


@dataclass
class Record:
    id: str
    title: str = ""
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    journal: str = ""
    doi: str = ""
    url: str = ""
    abstract: str = ""
    pub_types: list[str] = field(default_factory=list)
    subjects: list[str] = field(default_factory=list)  # controlled vocabulary / topics
    keywords: list[str] = field(default_factory=list)  # author keywords
    language: str = ""
    cited_by: int | None = None
    is_oa: bool | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class SearchResult:
    backend: str
    query_sent: str = ""
    count: int | None = None
    records: list[Record] = field(default_factory=list)
    interpretation: str = ""  # how the database says it parsed the query, when it reports that
    warnings: list[str] = field(default_factory=list)
    facets: dict[str, list[tuple[str, int]]] = field(default_factory=dict)  # whole result set
    error: str = ""


@dataclass
class SearchOptions:
    field: str = "tiab"
    sample: int = 10
    sort: str = "relevance"
    years: tuple[int, int] | None = None
    facets: bool = False


class Backend:
    name = ""
    label = ""
    coverage = ""
    min_interval = 0.2
    known_chunk: int | None = None  # ids find_known checks per search; None means all at once

    def __init__(self, http: Http):
        self.http = http

    def search(self, node: Node, opts: SearchOptions) -> SearchResult:
        result = SearchResult(backend=self.name)
        try:
            self._search(node, opts, result)
        except (HttpError, BackendError) as exc:
            result.error = str(exc)
        return result

    def check(self) -> None:
        """Raise BackendError if this backend is not configured to run."""

    def close(self) -> None:
        """Release anything the backend holds open."""

    def render(self, node: Node, opts: SearchOptions) -> str:
        """The query exactly as it is sent to this database."""
        raise NotImplementedError

    def _search(self, node: Node, opts: SearchOptions, result: SearchResult) -> None:
        raise NotImplementedError

    def resolve_known(self, ids: list[str]) -> dict[str, str]:
        """Map the known items this database indexes to {backend id: title}."""
        raise NotImplementedError

    def find_known(self, node: Node, opts: SearchOptions, known_ids: list[str]) -> set[str]:
        """Which of the known backend ids does this query retrieve?"""
        raise NotImplementedError


def split_known(ids: list[str]) -> tuple[list[str], list[str]]:
    """Separate a mixed list into (DOIs, PMIDs)."""
    dois = [i for i in ids if i.startswith("10.")]
    pmids = [i for i in ids if i.isdigit()]
    return dois, pmids


def chunks(items: list[str], size: int):
    for start in range(0, len(items), size):
        yield items[start : start + size]
