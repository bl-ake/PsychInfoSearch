from .base import Backend, BackendError, Record, SearchOptions, SearchResult
from .ebsco import Ebsco
from .ebscohost import EbscoHost
from .eric import Eric
from .europepmc import EuropePMC
from .openalex import OpenAlex
from .pubmed import PubMed

BACKENDS: dict[str, type[Backend]] = {b.name: b for b in (PubMed, OpenAlex, EuropePMC, Eric, EbscoHost, Ebsco)}

__all__ = ["BACKENDS", "Backend", "BackendError", "Record", "SearchOptions", "SearchResult"]
