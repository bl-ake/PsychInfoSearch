"""Run every query against every backend and derive the relevance signals."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Callable

from .backends import Backend, BackendError, Record, SearchOptions, SearchResult
from .combos import Block, Query
from .http import HttpError
from .query import matches

TOP_N = 8


@dataclass
class BackendOutcome:
    result: SearchResult
    known_found: int | None = None
    known_missed: list[str] = field(default_factory=list)  # titles of known items not retrieved
    on_topic: float | None = None  # share of sampled records matching every concept block
    unique: int | None = None  # drop-one: hits lost when this string is removed
    summary: dict = field(default_factory=dict)  # tallies over the sampled records


@dataclass
class Outcome:
    query: Query
    by_backend: dict[str, BackendOutcome] = field(default_factory=dict)


@dataclass
class KnownSet:
    indexed: dict[str, str] = field(default_factory=dict)  # backend id -> title
    error: str = ""


def score_records(records: list[Record], blocks: list[Block]) -> float | None:
    """Tag each record with how many concept blocks its title/abstract match; return the share matching all."""
    if len(blocks) < 2 or not records:
        return None
    nodes = [b.node for b in blocks]
    full = 0
    for record in records:
        text = f"{record.title} {record.abstract}"
        matched = sum(matches(node, text) for node in nodes)
        record.extra["blocks_matched"] = f"{matched}/{len(nodes)}"
        full += matched == len(nodes)
    return full / len(records)


def summarize(records: list[Record]) -> dict:
    if not records:
        return {}
    years = [r.year for r in records if r.year]

    def top(values) -> list[tuple[str, int]]:
        return Counter(v for v in values if v).most_common(TOP_N)

    return {
        "sampled": len(records),
        "with_abstract": sum(bool(r.abstract) for r in records),
        "years": [min(years), max(years)] if years else [],
        "journals": top(r.journal for r in records),
        "subjects": top(s.rstrip("*") for r in records for s in r.subjects),
        "keywords": top(k.lower() for r in records for k in r.keywords),
        "pub_types": top(t for r in records for t in r.pub_types),
        "languages": top(r.language for r in records),
    }


def resolve_known(backends: list[Backend], known_ids: list[str]) -> dict[str, KnownSet]:
    """Look up which known items each backend indexes (empty when there are none)."""
    known: dict[str, KnownSet] = {}
    if known_ids:
        for backend in backends:
            known[backend.name] = KnownSet()
            try:
                known[backend.name].indexed = backend.resolve_known(known_ids)
            except (HttpError, BackendError) as exc:
                known[backend.name].error = str(exc)
    return known


def run(
    queries: list[Query],
    backends: list[Backend],
    opts: SearchOptions,
    blocks: list[Block],
    known_ids: list[str],
    on_step: Callable[[], None] = lambda: None,
    known: dict[str, KnownSet] | None = None,
) -> tuple[list[Outcome], dict[str, KnownSet]]:
    """`known`, when given, is a resolve_known() result to reuse instead of looking `known_ids` up again."""
    if known is None:
        known = resolve_known(backends, known_ids)

    outcomes = []
    for query in queries:
        outcome = Outcome(query)
        for backend in backends:
            result = backend.search(query.node, opts)
            entry = BackendOutcome(result)
            indexed = known[backend.name].indexed if backend.name in known else {}
            if not result.error:
                entry.on_topic = score_records(result.records, blocks)
                entry.summary = summarize(result.records)
                if indexed and result.count:
                    try:
                        found = backend.find_known(query.node, opts, list(indexed))
                    except (HttpError, BackendError) as exc:
                        result.warnings.append(f"known-item check failed: {exc}")
                    else:
                        entry.known_found = len(found)
                        entry.known_missed = [indexed[i] or i for i in indexed if i not in found]
                elif indexed:
                    entry.known_found, entry.known_missed = 0, [t or i for i, t in indexed.items()]
            outcome.by_backend[backend.name] = entry
            on_step()
        outcomes.append(outcome)

    by_node = {o.query.node: o for o in outcomes}
    for outcome in outcomes:
        base = by_node.get(outcome.query.baseline) if outcome.query.baseline else None
        if base is None:
            continue
        for name, entry in outcome.by_backend.items():
            full_count = base.by_backend[name].result.count
            if full_count is not None and entry.result.count is not None:
                entry.unique = full_count - entry.result.count
    return outcomes, known
