"""Terminal table plus CSV / JSON / Markdown output."""

from __future__ import annotations

import csv
import json
from dataclasses import asdict
from pathlib import Path

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from .backends import Backend, SearchOptions
from .query import PSYCINFO_SYNTAXES, to_generic
from .runner import KnownSet, Outcome


def _num(value: int | None) -> str:
    return "-" if value is None else f"{value:,}"


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.0%}"


def _known(entry, known: KnownSet | None) -> str:
    if known is None or entry.known_found is None:
        return "-"
    return f"{entry.known_found}/{len(known.indexed)}"


def print_table(console: Console, outcomes: list[Outcome], backends: list[Backend], known: dict[str, KnownSet]) -> None:
    names = [b.name for b in backends]
    entries = [e for o in outcomes for e in o.by_backend.values()]
    show_known = bool(known)
    show_topic = any(e.on_topic is not None for e in entries)
    show_unique = any(e.unique is not None for e in entries)
    joined = " | ".join(b.label for b in backends)

    table = Table(title="Search string results")
    table.add_column("#", justify="right")
    table.add_column("strategy")
    table.add_column("query", overflow="fold", min_width=24, max_width=60)
    for backend in backends:
        table.add_column(f"{backend.label} hits", justify="right")
    if show_known:
        table.add_column(f"known found\n{joined}", justify="right")
    if show_topic:
        table.add_column(f"on-topic (sample)\n{joined}", justify="right")
    if show_unique:
        table.add_column(f"hits lost\n{joined}", justify="right")

    for index, outcome in enumerate(outcomes, 1):
        by = outcome.by_backend
        row = [str(index), outcome.query.strategy, escape(outcome.query.label)]
        row += ["[red]error[/red]" if by[n].result.error else _num(by[n].result.count) for n in names]
        if show_known:
            row.append(" | ".join(_known(by[n], known.get(n)) for n in names))
        if show_topic:
            row.append(" | ".join(_pct(by[n].on_topic) for n in names))
        if show_unique:
            row.append(" | ".join(_num(by[n].unique) for n in names))
        table.add_row(*row)
    console.print(table)

    errors = dict.fromkeys(f"{n}: {o.by_backend[n].result.error}" for o in outcomes for n in names if o.by_backend[n].result.error)
    for error in errors:
        console.print(f"[red]error[/red] {escape(error)}")
    for name, item in known.items():
        if item.error:
            console.print(f"[red]error[/red] {name}: could not look up known items: {escape(item.error)}")


def psycinfo_strings(outcome: Outcome, field: str) -> dict[str, str]:
    return {name: render(outcome.query.node, field) for name, render in PSYCINFO_SYNTAXES.items()}


def write_summary_csv(path: Path, outcomes: list[Outcome], backends: list[Backend], known: dict[str, KnownSet], field: str) -> None:
    names = [b.name for b in backends]
    header = ["n", "strategy", "label", "query"]
    for name in names:
        header += [f"{name}_hits", f"{name}_known_found", f"{name}_known_indexed", f"{name}_on_topic", f"{name}_hits_lost", f"{name}_query_sent", f"{name}_error"]
    header += [f"psycinfo_{s}" for s in PSYCINFO_SYNTAXES]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        for index, outcome in enumerate(outcomes, 1):
            row = [index, outcome.query.strategy, outcome.query.label, to_generic(outcome.query.node)]
            for name in names:
                entry = outcome.by_backend[name]
                row += [
                    entry.result.count,
                    entry.known_found,
                    len(known[name].indexed) if name in known else None,
                    None if entry.on_topic is None else round(entry.on_topic, 3),
                    entry.unique,
                    entry.result.query_sent,
                    entry.result.error,
                ]
            row += list(psycinfo_strings(outcome, field).values())
            writer.writerow(["" if v is None else v for v in row])


def write_records_csv(path: Path, outcomes: list[Outcome]) -> None:
    header = ["n", "label", "backend", "rank", "id", "title", "authors", "year", "journal", "doi", "url", "pub_types",
              "subjects", "keywords", "language", "cited_by", "is_oa", "blocks_matched", "field", "database", "abstract"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        for index, outcome in enumerate(outcomes, 1):
            for name, entry in outcome.by_backend.items():
                for rank, r in enumerate(entry.result.records, 1):
                    writer.writerow([
                        index, outcome.query.label, name, rank, r.id, r.title, "; ".join(r.authors), r.year or "", r.journal,
                        r.doi, r.url, "; ".join(r.pub_types), "; ".join(r.subjects), "; ".join(r.keywords), r.language,
                        "" if r.cited_by is None else r.cited_by, "" if r.is_oa is None else r.is_oa,
                        r.extra.get("blocks_matched", ""), r.extra.get("field", ""), r.extra.get("database", ""), r.abstract,
                    ])


def write_json(path: Path, outcomes: list[Outcome], backends: list[Backend], known: dict[str, KnownSet], settings: dict, field: str) -> None:
    payload = {
        "settings": settings,
        "known_items": {name: asdict(item) for name, item in known.items()},
        "queries": [
            {
                "n": index,
                "label": o.query.label,
                "strategy": o.query.strategy,
                "query": to_generic(o.query.node),
                "psycinfo": psycinfo_strings(o, field),
                "backends": {name: asdict(entry) for name, entry in o.by_backend.items()},
            }
            for index, o in enumerate(outcomes, 1)
        ],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _tally(pairs: list) -> str:
    return ", ".join(f"{name} ({count:,})" for name, count in pairs)


def write_markdown(path: Path, outcomes: list[Outcome], backends: list[Backend], known: dict[str, KnownSet], settings: dict, opts: SearchOptions) -> None:
    names = [b.name for b in backends]
    lines = ["# Search string test report", ""]
    lines += [f"- **{key}**: {value}" for key, value in settings.items()]
    for backend in backends:
        lines.append(f"- **{backend.label}** covers {backend.coverage}")
    for name, item in known.items():
        lines.append(f"- **known items in {name}**: {len(item.indexed)} indexed" + (f" (lookup failed: {item.error})" if item.error else ""))

    lines += ["", "## Summary", "", "| # | strategy | query | " + " | ".join(f"{n} hits" for n in names) + " |",
              "|" + "---|" * (3 + len(names))]
    for index, o in enumerate(outcomes, 1):
        counts = " | ".join("error" if o.by_backend[n].result.error else _num(o.by_backend[n].result.count) for n in names)
        lines.append(f"| {index} | {o.query.strategy} | {o.query.label.replace('|', '/')} | {counts} |")

    lines += ["", "## Queries"]
    for index, o in enumerate(outcomes, 1):
        lines += ["", f"### {index}. {o.query.label}", "", f"`{to_generic(o.query.node)}`", "", "PsycInfo syntax:", ""]
        lines += [f"- {name}: `{text}`" for name, text in psycinfo_strings(o, opts.field).items()]
        for name in names:
            entry = o.by_backend[name]
            result = entry.result
            lines += ["", f"**{name}**: " + (f"error: {result.error}" if result.error else f"{_num(result.count)} hits")]
            if result.error:
                continue
            lines.append(f"- sent: `{result.query_sent}`")
            if result.interpretation and result.interpretation != result.query_sent:
                lines.append(f"- interpreted as: `{result.interpretation}`")
            lines += [f"- warning: {w}" for w in result.warnings]
            if entry.known_found is not None:
                lines.append(f"- known items retrieved: {_known(entry, known.get(name))}")
                lines += [f"  - missed: {title}" for title in entry.known_missed]
            if entry.on_topic is not None:
                lines.append(f"- on-topic in sample (matches every concept block): {_pct(entry.on_topic)}")
            if entry.unique is not None:
                lines.append(f"- hits lost versus the full strategy: {_num(entry.unique)}")
            for facet, pairs in result.facets.items():
                lines.append(f"- all hits by {facet}: {_tally(pairs[:12])}")
            summary = entry.summary
            if summary:
                span = "-".join(str(y) for y in summary["years"])
                lines.append(f"- sample of {summary['sampled']} ({opts.sort} order): years {span}; {summary['with_abstract']} with abstract")
                for key in ("subjects", "keywords", "journals", "pub_types"):
                    if summary[key]:
                        lines.append(f"  - {key.replace('_', ' ')}: {_tally(summary[key])}")
            for rank, r in enumerate(result.records, 1):
                authors = (r.authors[0] + (" et al." if len(r.authors) > 1 else "")) if r.authors else "n.a."
                matched = f" [blocks {r.extra['blocks_matched']}]" if "blocks_matched" in r.extra else ""
                lines.append(f"  {rank}. [{r.title or 'untitled'}]({r.url}) - {authors} ({r.year or 'n.d.'}), *{r.journal or 'n.a.'}*{matched}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
