"""Find search strings that cannot add anything to their concept block.

Strings in a block are ORed, so a string is redundant when everything it matches, the rest of
the block matches too: ``mathematics`` beside ``math*``, ``"serious game*"`` beside ``game*``,
``"middle school" AND classroom`` beside ``"middle school"``. This is worked out from the
strings alone, without searching, and errs towards keeping a string:

* a truncated word covers the words it truncates (``math*`` covers ``mathematics``, ``maths*``);
* a word or phrase covers any phrase that contains it (``game*`` covers ``"serious game*"``);
  hyphens count as spaces, as they do in PsycInfo, so ``game*`` also covers ``game-based``;
* matching ignores case; stemming, plurals and other database expansions are not assumed, so
  ``game`` does not cover ``games``;
* a ``NOT`` part never makes a string redundant, and a string containing ``NOT`` is never
  treated as covering another.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from functools import lru_cache
from itertools import product
from typing import TYPE_CHECKING

from .query import And, Node, Not, Or, Term, any_of, to_generic

if TYPE_CHECKING:
    from .combos import Block

MAX_CONJUNCTIONS = 5_000  # beyond this a string is kept rather than expanded further
_WILDCARD = re.compile(r"[*?]")


def _words(term: Term) -> list[str]:
    return [w for w in re.split(r"[\s\-]+", term.text.lower()) if w]


@lru_cache(maxsize=None)
def _word_regex(word: str) -> re.Pattern:
    return re.compile(re.escape(word).replace(r"\*", r"\w*").replace(r"\?", r"\w"))


def _word_covers(broad: str, narrow: str) -> bool:
    """Every word matching pattern `narrow` also matches pattern `broad`."""
    if broad == narrow:
        return True
    if not _WILDCARD.search(broad):
        return False
    if not _WILDCARD.search(narrow):
        return bool(_word_regex(broad).fullmatch(narrow))
    # Both truncated: only decide the plain case, math* covering mathemat*.
    if broad.endswith("*") and not _WILDCARD.search(broad[:-1]):
        return _WILDCARD.split(narrow, maxsplit=1)[0].startswith(broad[:-1])
    return False


def term_covers(broad: Term, narrow: Term) -> bool:
    """Every record matching `narrow` also matches `broad`: broad's words appear, in order, inside narrow."""
    b, n = _words(broad), _words(narrow)
    if not b or len(b) > len(n):
        return False
    return any(
        all(_word_covers(bw, nw) for bw, nw in zip(b, n[start : start + len(b)]))
        for start in range(len(n) - len(b) + 1)
    )


def _positive(node: Node) -> Node | None:
    """`node` with its NOT parts removed (so it can only get broader); None if nothing is left."""
    if isinstance(node, Term):
        return node
    if isinstance(node, Not):
        return None
    parts = [p for p in (_positive(p) for p in node.parts) if p is not None]
    if not parts:
        return None
    if isinstance(node, Or):
        return any_of(parts) if len(parts) == len(node.parts) else None  # OR NOT x: give up
    return parts[0] if len(parts) == 1 else And(tuple(parts))


def _conjunctions(node: Node) -> set[frozenset]:
    if isinstance(node, Term):
        return {frozenset([node])}
    parts = [_conjunctions(p) for p in node.parts]
    if isinstance(node, Or):
        return set().union(*parts)
    if math.prod(len(p) for p in parts) > MAX_CONJUNCTIONS:
        raise OverflowError
    return {frozenset().union(*combo) for combo in product(*parts)}


def _has_not(node: Node) -> bool:
    return isinstance(node, Not) or (not isinstance(node, Term) and any(_has_not(p) for p in node.parts))


def _satisfied(node: Node, present: frozenset) -> bool:
    if isinstance(node, Term):
        return any(term_covers(node, t) for t in present)
    if isinstance(node, And):
        return all(_satisfied(p, present) for p in node.parts)
    return any(_satisfied(p, present) for p in node.parts)


def covers(broad: Node, narrow: Node) -> bool:
    """True when every record matching `narrow` must match `broad`. False when unsure."""
    narrow = _positive(narrow)
    if narrow is None or _has_not(broad):
        return False
    try:
        return all(_satisfied(broad, conj) for conj in _conjunctions(narrow))
    except OverflowError:
        return False


@dataclass
class Redundant:
    block: str
    line: int
    text: str
    candidate: bool
    covered_by: list[str]  # the strings that each cover it alone; empty if only the rest together do

    @property
    def reason(self) -> str:
        if not self.covered_by:
            return "covered by the rest of the block together"
        return "covered by " + ", ".join(self.covered_by)


def find_redundant(blocks: list["Block"]) -> list[Redundant]:
    """Strings, and '?' candidates, that add nothing to their block; the first of two equivalent strings is kept."""
    found: list[Redundant] = []
    for block in blocks:
        if block.name is None:
            continue
        terms = [(line, node) for line, node, candidate in block.entries if not candidate]
        keep = list(terms)
        for entry in reversed(terms):  # later lines go first, so of two equivalent strings the earlier stays
            others = [e for e in keep if e is not entry]
            if others and covers(any_of(n for _, n in others), entry[1]):
                keep.remove(entry)
                found.append(_redundant(block, entry, others, candidate=False))
        seen: list[tuple[int, Node]] = []
        for line, node, candidate in block.entries:
            if not candidate:
                continue
            others = keep + seen
            if covers(any_of(n for _, n in others), node):
                found.append(_redundant(block, (line, node), others, candidate=True))
            else:
                seen.append((line, node))
    return sorted(found, key=lambda r: r.line)


def _redundant(block: "Block", entry: tuple[int, Node], others: list[tuple[int, Node]], candidate: bool) -> Redundant:
    line, node = entry
    covering = [f"line {l} (same string)" if n == node else to_generic(n) for l, n in others if covers(n, node)]
    return Redundant(block.name or "", line, to_generic(node), candidate, covering)


def comment_out(lines: list[str], redundant: list[Redundant]) -> list[str]:
    """The terms file with each redundant line turned into a comment that says why."""
    out = list(lines)
    for item in redundant:
        out[item.line - 1] = f"# redundant ({item.reason}): {lines[item.line - 1].strip()}"
    return out
