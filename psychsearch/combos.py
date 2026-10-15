"""Turn a list of search strings (optionally grouped into concept blocks) into queries to test."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from itertools import combinations, product
from pathlib import Path

from .query import Node, Not, QueryError, all_of, any_of, parse, to_generic

STRATEGIES = {
    "singles": "every search string on its own",
    "blocks": "each concept block (its strings joined with OR)",
    "and": "AND across every subset of blocks",
    "or": "OR across every subset of blocks",
    "cross": "one string from each block, ANDed (every permutation)",
    "drop-one": "the full strategy with one string removed, to show what each string adds",
}
# Not chosen with -s: --refine BLOCK produces these queries (see refine()).
REFINE_HELP = "the full strategy, minus each string of one block, and what each '?' candidate would add"
DEFAULT_STRATEGIES = ("singles", "blocks", "and")


@dataclass
class Block:
    name: str | None  # None for an ungrouped string
    terms: list[Node]
    candidates: list[Node] = field(default_factory=list)  # "? string" lines, only searched by --refine
    entries: list[tuple[int, Node, bool]] = field(default_factory=list)  # (line, string, is candidate), duplicates included

    @property
    def node(self) -> Node:
        return any_of(self.terms)

    @property
    def label(self) -> str:
        return f"[{self.name}]" if self.name else to_generic(self.node)


@dataclass
class Query:
    label: str
    strategy: str
    node: Node
    baseline: Node | None = None  # for drop-one: the full strategy this was derived from


_HEADER = re.compile(r"^\[(.+)\]$")
_CANDIDATE = re.compile(r"^\?\s+(.*)$")


def blocks_from_lines(lines: list[str]) -> list[Block]:
    """``[name]`` lines start a concept block; with no headers each line is its own block.

    A line starting ``? `` inside a block is a candidate string: ignored by the normal strategies,
    tested by :func:`refine`.
    """
    blocks: list[Block] = []
    current: Block | None = None
    for number, raw in enumerate(lines, 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if header := _HEADER.match(line):
            current = Block(header.group(1).strip(), [])
            blocks.append(current)
            continue
        candidate = _CANDIDATE.match(line)
        if candidate and current is None:
            raise QueryError(f"line {number}: a '?' candidate must come after a [name] line")
        try:
            node = parse(candidate.group(1) if candidate else line)
        except QueryError as exc:
            raise QueryError(f"line {number}: {exc}") from None
        if current is None:
            blocks.append(Block(None, [node], entries=[(number, node, False)]))
            continue
        current.entries.append((number, node, bool(candidate)))
        if candidate:
            if node not in current.candidates:
                current.candidates.append(node)
        elif node not in current.terms:
            current.terms.append(node)
    if empty := [b.name for b in blocks if not b.terms]:
        raise QueryError(f"concept block(s) with no search strings: {', '.join(empty)}")
    return blocks


def load_blocks(path: Path) -> list[Block]:
    return blocks_from_lines(path.read_text(encoding="utf-8").splitlines())


def full_strategy(blocks: list[Block]) -> Node:
    return all_of(b.node for b in blocks)


def find_block(blocks: list[Block], name: str) -> Block:
    for block in blocks:
        if block.name and block.name.lower() == name.lower():
            return block
    named = [b.name for b in blocks if b.name]
    if not named:
        raise QueryError("--refine needs a terms file with [name] concept blocks")
    raise QueryError(f"no concept block [{name}]; the file has {', '.join(f'[{n}]' for n in named)}")


def refine(blocks: list[Block], name: str) -> list[Query]:
    """Queries for improving one block while the others stay fixed.

    The full strategy; the full strategy minus each of the block's strings (its "hits lost" is
    what that string alone contributes); and, for each ``?`` candidate, the records it would add:
    ``candidate NOT block`` in the block's place, ANDed with the other blocks.

    Strings the rest of the block already covers (see redundancy.py) would show 0, so they are skipped.
    """
    from .redundancy import find_redundant

    target = find_block(blocks, name)
    covered = {r.line for r in find_redundant([target])}
    # a string written twice is covered only by its copy; the first copy still counts
    covered_nodes = {n for l, n, _ in target.entries if l in covered} - {n for l, n, _ in target.entries if l not in covered}
    full = full_strategy(blocks)
    queries = [Query("FULL: " + _subset_label(tuple(blocks), "AND", full), "refine", full)]
    if len(target.terms) > 1:
        for term in target.terms:
            if term in covered_nodes:
                continue
            reduced = any_of(t for t in target.terms if t != term)
            node = all_of(reduced if b is target else b.node for b in blocks)
            queries.append(Query(f"FULL minus {to_generic(term)}", "refine", node, baseline=full))
    for candidate in target.candidates:
        if candidate in target.terms or candidate in covered_nodes:
            continue
        node = all_of([*(candidate if b is target else b.node for b in blocks), Not(target.node)])
        queries.append(Query(f"added by {to_generic(candidate)}", "refine", node))
    return queries


def with_candidates(blocks: list[Block]) -> list[Block]:
    """Blocks with their candidates counted as members, for scoring records that a candidate retrieved."""
    return [Block(b.name, [*b.terms, *(c for c in b.candidates if c not in b.terms)]) for b in blocks]


def _subset_label(subset: tuple[Block, ...], op: str, node: Node) -> str:
    if all(b.name for b in subset):
        return f" {op} ".join(b.label for b in subset)
    return to_generic(node)


def generate(blocks: list[Block], strategies: tuple[str, ...], max_size: int | None = None) -> list[Query]:
    """Build the queries for the chosen strategies, de-duplicated, in a stable order."""
    queries: list[Query] = []
    seen: set[Node] = set()

    def add(label: str, strategy: str, node: Node, baseline: Node | None = None) -> None:
        if node not in seen:
            seen.add(node)
            queries.append(Query(label, strategy, node, baseline))

    sizes = range(2, min(max_size or len(blocks), len(blocks)) + 1)
    for strategy in strategies:
        if strategy == "singles":
            for block in blocks:
                for term in block.terms:
                    add(to_generic(term), strategy, term)
        elif strategy == "blocks":
            for block in blocks:
                add(block.label, strategy, block.node)
        elif strategy in ("and", "or"):
            join, op = (all_of, "AND") if strategy == "and" else (any_of, "OR")
            for size in sizes:
                for subset in combinations(blocks, size):
                    node = join(b.node for b in subset)
                    add(_subset_label(subset, op, node), strategy, node)
        elif strategy == "cross":
            if len(blocks) > 1:
                for picks in product(*(b.terms for b in blocks)):
                    node = all_of(picks)
                    add(to_generic(node), strategy, node)
        elif strategy == "drop-one":
            full = full_strategy(blocks)
            add("FULL: " + _subset_label(tuple(blocks), "AND", full), strategy, full)
            for i, block in enumerate(blocks):
                if len(block.terms) < 2:
                    continue
                for term in block.terms:
                    reduced = any_of(t for t in block.terms if t != term)
                    node = all_of([*(b.node for b in blocks[:i]), reduced, *(b.node for b in blocks[i + 1 :])])
                    add(f"FULL minus {to_generic(term)}", strategy, node, baseline=full)
        else:
            raise QueryError(f"unknown strategy {strategy!r}; choose from {', '.join(STRATEGIES)}")
    return queries
