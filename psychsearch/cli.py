from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.markup import escape
from rich.progress import Progress

from . import home, report
from .backends import BACKENDS, BackendError, SearchOptions
from .backends.base import MAX_SAMPLE
from .combos import DEFAULT_STRATEGIES, REFINE_HELP, STRATEGIES, Block, Query, generate, load_blocks, refine, with_candidates
from .http import Http
from .redundancy import Redundant, comment_out, find_redundant
from .query import FIELDS, PSYCINFO_SYNTAXES, QueryError, parse, to_generic
from .runner import run

CACHE_DIR = Path(".psychsearch_cache")


def _parser() -> argparse.ArgumentParser:
    strategies = "\n".join(f"  {name:<9} {text}" for name, text in STRATEGIES.items())
    strategies += f"\n  {'refine':<9} (with --refine BLOCK) {REFINE_HELP}"
    backends = "\n".join(f"  {name:<10} {cls.coverage}" for name, cls in BACKENDS.items())
    parser = argparse.ArgumentParser(
        prog="psychsearch",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Test combinations of search strings: hit counts, sample records, relevance signals, "
        "and the same query written in APA PsycInfo syntax.",
        epilog=f"strategies:\n{strategies}\n\nbackends:\n{backends}",
    )
    parser.add_argument("terms", nargs="?", type=Path, help="text file of search strings, one per line; [name] lines start concept blocks")
    parser.add_argument("-q", "--query", action="append", default=[], metavar="STRING", help="an extra query to run as written (repeatable)")
    parser.add_argument("-b", "--backends", default="pubmed,openalex", help="comma-separated (default: %(default)s)")
    parser.add_argument("-s", "--strategies", help=f"comma-separated, or 'all' (default: {','.join(DEFAULT_STRATEGIES)}; none with --refine)")
    parser.add_argument("--refine", metavar="BLOCK", help="work on one concept block: what each of its strings contributes to the full strategy, "
                        "and what each '? candidate' line in it would add")
    parser.add_argument("--dedupe", action="store_true", help="find strings that add nothing to their block (no searching) and comment them "
                        "out in the terms file; with --dry-run, only list them")
    parser.add_argument("--max-size", type=int, metavar="N", help="largest number of blocks to join in and/or combinations")
    parser.add_argument("--field", choices=FIELDS, default="tiab", help="where to search: title+abstract, title, or all fields (default: %(default)s)")
    parser.add_argument("--sample", type=int, default=10, metavar="N", help=f"records to fetch per query, 0-{MAX_SAMPLE} (default: %(default)s)")
    parser.add_argument("--sort", choices=("relevance", "date"), default="relevance", help="which records the sample takes (default: %(default)s)")
    parser.add_argument("--years", metavar="FROM-TO", help="restrict publication years, e.g. 2010-2025")
    parser.add_argument("--known", type=Path, metavar="FILE", help="DOIs/PMIDs of known relevant papers; reports how many each query retrieves")
    parser.add_argument("--facets", action="store_true", help="OpenAlex only: break all hits down by year, type and field (3 extra requests per query)")
    parser.add_argument("--max-queries", type=int, default=150, metavar="N", help="refuse to run more than this many queries (default: %(default)s)")
    parser.add_argument("--out", type=Path, default=Path("results"), help="output folder (default: %(default)s)")
    parser.add_argument("--no-cache", action="store_true", help="ignore and do not write the 7-day response cache")
    parser.add_argument("--dry-run", action="store_true", help="show the generated queries in every syntax without searching")
    return parser


def _load_known(path: Path) -> list[str]:
    ids, bad = [], []
    for raw in path.read_text(encoding="utf-8").splitlines():
        value = raw.split("#", 1)[0].strip()
        if not value:
            continue
        value = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:\s*|pmid:\s*)", "", value, flags=re.I)
        if value.startswith("10."):
            ids.append(value.lower())
        elif value.isdigit():
            ids.append(value)
        else:
            bad.append(raw.strip())
    if bad:
        raise QueryError(f"{path}: not a DOI or PMID: {', '.join(bad)}")
    return list(dict.fromkeys(ids))


def _load_env(path: Path) -> None:
    """Read KEY=value lines from a .env file without overriding the real environment."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _dry_run(console: Console, queries: list[Query], backends, opts: SearchOptions) -> None:
    for index, query in enumerate(queries, 1):
        console.print(f"[bold]{index}. {escape(query.label)}[/bold]  [dim]({query.strategy})[/dim]", highlight=False)
        console.print(f"   generic   {to_generic(query.node)}", highlight=False, markup=False)
        for backend in backends:
            sent = backend.render(query.node, opts)
            console.print(f"   {backend.name:<9} {sent}", highlight=False, markup=False)
        for name, render in PSYCINFO_SYNTAXES.items():
            if name not in {b.name for b in backends}:
                console.print(f"   {name:<9} {render(query.node, opts.field)}", highlight=False, markup=False)


def _print_redundant(console: Console, redundant: list[Redundant]) -> None:
    console.print(f"[yellow]{len(redundant)} string(s) add nothing to their block:[/yellow]", highlight=False)
    for r in redundant:
        what = "candidate " if r.candidate else ""
        console.print(f"  line {r.line:<3} {escape(f'[{r.block}]')} {what}{escape(r.text)}  [dim]{escape(r.reason)}[/dim]", highlight=False)


def _dedupe(console: Console, path: Path, redundant: list[Redundant], dry_run: bool) -> int:
    if not redundant:
        console.print("No redundant strings found.")
        return 0
    _print_redundant(console, redundant)
    if dry_run:
        console.print("\n[dim]--dry-run: the file was not changed[/dim]", highlight=False)
        return 0
    text = path.read_text(encoding="utf-8")
    lines = comment_out(text.splitlines(), redundant)
    path.write_text("\n".join(lines) + ("\n" if text.endswith("\n") else ""), encoding="utf-8")
    console.print(f"\nCommented out {len(redundant)} line(s) in {escape(str(path))}; delete the '# redundant ...: ' prefix to restore one.", highlight=False)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    console = Console()
    if not console.is_terminal:
        console = Console(width=160)  # keep the table readable when piped to a file
    args.terms = home.find(args.terms) if args.terms else None
    args.known = home.find(args.known) if args.known else None
    home.enter()
    _load_env(Path(".env"))
    try:
        return _main(args, parser, console)
    except (QueryError, BackendError, FileNotFoundError) as exc:
        console.print(f"[red]error:[/red] {escape(str(exc))}", highlight=False)
        return 2


def _main(args: argparse.Namespace, parser: argparse.ArgumentParser, console: Console) -> int:
    if not args.terms and not args.query:
        parser.error("give a terms file and/or at least one --query")
    if not 0 <= args.sample <= MAX_SAMPLE:
        parser.error(f"--sample must be between 0 and {MAX_SAMPLE}")

    names = [n.strip() for n in args.backends.split(",") if n.strip()]
    if unknown := [n for n in names if n not in BACKENDS]:
        parser.error(f"unknown backend(s) {', '.join(unknown)}; choose from {', '.join(BACKENDS)}")
    if args.strategies is None:
        strategies = () if args.refine else DEFAULT_STRATEGIES
    elif args.strategies == "all":
        strategies = tuple(STRATEGIES)
    else:
        strategies = tuple(s.strip() for s in args.strategies.split(",") if s.strip())
    if (args.refine or args.dedupe) and not args.terms:
        parser.error(f"{'--refine' if args.refine else '--dedupe'} needs a terms file")

    years = None
    if args.years:
        if not (match := re.fullmatch(r"(\d{4})-(\d{4})", args.years)):
            parser.error("--years must look like 2010-2025")
        years = (int(match.group(1)), int(match.group(2)))

    blocks: list[Block] = load_blocks(args.terms) if args.terms else []
    redundant = find_redundant(blocks)
    if args.dedupe:
        return _dedupe(console, args.terms, redundant, args.dry_run)
    if redundant:
        _print_redundant(console, redundant)
        console.print(f"[dim]--dedupe comments these out of {escape(str(args.terms))}; --refine skips them[/dim]\n", highlight=False)
    queries = generate(blocks, strategies, args.max_size) if blocks else []
    seen = {q.node for q in queries}
    if args.refine:
        for query in refine(blocks, args.refine):
            if query.node not in seen:
                seen.add(query.node)
                queries.append(query)
    for text in args.query:
        node = parse(text)
        if node not in seen:
            seen.add(node)
            queries.append(Query(to_generic(node), "custom", node))
    if len(queries) > args.max_queries:
        raise QueryError(
            f"{len(queries)} queries generated, above --max-queries {args.max_queries}. "
            "Narrow the strategies, lower --max-size, or raise --max-queries."
        )

    opts = SearchOptions(field=args.field, sample=args.sample, sort=args.sort, years=years, facets=args.facets)
    http = Http(None if args.no_cache else CACHE_DIR)
    backends = [BACKENDS[name](http) for name in names]

    if args.dry_run:
        _dry_run(console, queries, backends, opts)
        return 0

    known_ids = _load_known(args.known) if args.known else []
    try:
        for backend in backends:
            backend.check()
        console.print(f"{len(queries)} queries x {len(backends)} backend(s): {', '.join(b.label for b in backends)}", highlight=False)
        with Progress(console=console, transient=True) as progress:
            task = progress.add_task("searching", total=len(queries) * len(backends))
            # Records a candidate retrieved should count as on topic when they match that candidate.
            scoring = with_candidates(blocks) if args.refine else blocks
            outcomes, known = run(queries, backends, opts, scoring, known_ids, on_step=lambda: progress.advance(task))
    finally:
        for backend in backends:
            backend.close()

    report.print_table(console, outcomes, backends, known)

    out_dir = args.out / datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    settings = {
        "run": datetime.now().isoformat(timespec="seconds"),
        "terms file": str(args.terms) if args.terms else "(none)",
        "strategies": ", ".join([*strategies, *([f"refine [{args.refine}]"] if args.refine else [])]) or "(none)",
        "search field": args.field,
        "years": args.years or "any",
        "sample": f"{args.sample} per query, {args.sort} order",
        "known items supplied": len(known_ids),
    }
    report.write_summary_csv(out_dir / "summary.csv", outcomes, backends, known, args.field)
    report.write_records_csv(out_dir / "records.csv", outcomes)
    report.write_json(out_dir / "results.json", outcomes, backends, known, settings, args.field)
    report.write_markdown(out_dir / "report.md", outcomes, backends, known, settings, opts)

    console.print(f"\nWrote summary.csv, records.csv, results.json and report.md to {out_dir.resolve()}/", highlight=False)
    console.print(f"[dim]{http.requests_made} requests sent, {http.cache_hits} answered from cache[/dim]", highlight=False)
    for backend in backends:
        if getattr(backend, "remaining_usd", None) is not None:
            console.print(f"[dim]OpenAlex allowance left today: about {round(backend.remaining_usd / 0.001)} searches[/dim]", highlight=False)

    failed = any(e.result.error for o in outcomes for e in o.by_backend.values()) or any(k.error for k in known.values())
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
