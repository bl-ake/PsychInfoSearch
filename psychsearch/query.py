"""Boolean query model: parse a search string once, render it in each database's syntax.

Input syntax (deliberately close to what PsycInfo platforms accept):
  * ``AND`` / ``OR`` / ``NOT`` in capitals are operators; lowercase words are just words.
  * ``"quoted text"`` is a phrase. Consecutive unquoted words are also a phrase,
    so ``social media`` means ``"social media"``.
  * ``*`` truncates (``adolescen*``); ``?`` is a single-character wildcard.
  * Parentheses group.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable, Iterable, Union

FIELDS = ("tiab", "title", "all")


class QueryError(ValueError):
    pass


@dataclass(frozen=True)
class Term:
    text: str

    @property
    def is_phrase(self) -> bool:
        return " " in self.text

    @property
    def has_wildcard(self) -> bool:
        return "*" in self.text or "?" in self.text


@dataclass(frozen=True)
class Not:
    part: "Node"


@dataclass(frozen=True)
class And:
    parts: tuple


@dataclass(frozen=True)
class Or:
    parts: tuple


Node = Union[Term, Not, And, Or]


def _combine(cls, nodes: Iterable[Node]) -> Node:
    flat: list[Node] = []
    for node in nodes:
        for part in node.parts if isinstance(node, cls) else (node,):
            if part not in flat:
                flat.append(part)
    if not flat:
        raise QueryError("cannot combine an empty list of queries")
    return flat[0] if len(flat) == 1 else cls(tuple(flat))


def any_of(nodes: Iterable[Node]) -> Node:
    return _combine(Or, nodes)


def all_of(nodes: Iterable[Node]) -> Node:
    return _combine(And, nodes)


# ---------------------------------------------------------------- parsing

_TOKEN = re.compile(r'"([^"]*)"|([()])|([^\s()"]+)')
_OPERATORS = {"AND", "OR", "NOT"}


def _tokenize(text: str) -> list[tuple[str, str]]:
    text = text.replace("“", '"').replace("”", '"')
    if text.count('"') % 2:
        raise QueryError(f"unbalanced quotes in: {text}")
    tokens = []
    for phrase, paren, word in _TOKEN.findall(text):
        if paren:
            tokens.append((paren, paren))
        elif word in _OPERATORS:
            tokens.append(("op", word))
        elif word:
            tokens.append(("word", word))
        else:
            phrase = " ".join(phrase.split())
            if not phrase:
                raise QueryError(f"empty quoted phrase in: {text}")
            tokens.append(("phrase", phrase))
    return tokens


class _Parser:
    def __init__(self, text: str):
        self.text = text
        self.tokens = _tokenize(text)
        self.pos = 0

    def peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def take(self):
        token = self.peek()
        self.pos += 1
        return token

    def parse(self) -> Node:
        if not self.tokens:
            raise QueryError("empty query")
        node = self.parse_or()
        if self.peek() is not None:
            raise QueryError(f"unexpected {self.peek()[1]!r} in: {self.text}")
        return node

    def parse_or(self) -> Node:
        parts = [self.parse_and()]
        while self.peek() == ("op", "OR"):
            self.take()
            parts.append(self.parse_and())
        return any_of(parts)

    def parse_and(self) -> Node:
        parts = [self.parse_unary()]
        while (token := self.peek()) is not None:
            if token == ("op", "AND"):
                self.take()
                parts.append(self.parse_unary())
            elif token == ("op", "NOT"):
                self.take()
                parts.append(Not(self.parse_unary()))
            elif token[0] in ("word", "phrase", "("):
                parts.append(self.parse_unary())  # adjacency without an operator means AND
            else:
                break
        return all_of(parts)

    def parse_unary(self) -> Node:
        token = self.take()
        if token is None:
            raise QueryError(f"query ends unexpectedly: {self.text}")
        kind, value = token
        if kind == "(":
            node = self.parse_or()
            if self.take() != (")", ")"):
                raise QueryError(f"missing ')' in: {self.text}")
            return node
        if kind == "phrase":
            return Term(value)
        if kind == "word":
            words = [value]
            while (nxt := self.peek()) is not None and nxt[0] == "word":
                words.append(self.take()[1])
            return Term(" ".join(words))
        raise QueryError(f"unexpected {value!r} in: {self.text}")


def parse(text: str) -> Node:
    return _Parser(text).parse()


# -------------------------------------------------------------- rendering


def _quoted(term: Term) -> str:
    return f'"{term.text}"' if term.is_phrase else term.text


def _is_term_group(node: Node) -> bool:
    return isinstance(node, Term) or (
        isinstance(node, Or) and all(isinstance(p, Term) for p in node.parts)
    )


def _render(
    node: Node,
    term: Callable[[Term], str],
    ops: tuple[str, str, str] = ("AND", "OR", "NOT"),
    group: Callable[[Node], str] | None = None,
    top: bool = True,
) -> str:
    """Render a tree. `group`, when given, takes over any term or OR-list of terms."""
    and_op, or_op, not_op = ops
    if group is not None and _is_term_group(node):
        return group(node)
    if isinstance(node, Term):
        return term(node)
    if isinstance(node, Not):
        return f"{not_op} {_render(node.part, term, ops, group, top=False)}"
    out = ""
    for i, part in enumerate(node.parts):
        rendered = _render(part, term, ops, group, top=False)
        if i == 0:
            out = rendered
        elif isinstance(part, Not):
            out += f" {rendered}"
        else:
            out += f" {and_op if isinstance(node, And) else or_op} {rendered}"
    return out if top else f"({out})"


def to_generic(node: Node) -> str:
    return _render(node, _quoted)


def to_pubmed(node: Node, field: str = "tiab") -> str:
    tag = {"tiab": "tiab", "title": "ti", "all": "all"}[field]
    return _render(node, lambda t: f"{_quoted(t)}[{tag}]")


def to_europepmc(node: Node, field: str = "tiab") -> str:
    prefix = {"tiab": "TITLE_ABS:", "title": "TITLE:", "all": ""}[field]
    return _render(node, lambda t: f"{prefix}{_quoted(t)}")


def to_openalex(node: Node, warnings: list[str] | None = None) -> str:
    """Value for an OpenAlex ``*.search.exact`` filter (unstemmed, wildcard-capable)."""

    def term(t: Term) -> str:
        text = re.sub(r"[,|]", " ", t.text)  # filter-syntax separators
        if t.is_phrase and t.has_wildcard:
            # OpenAlex rejects wildcards inside a quoted phrase.
            text = re.sub(r"[*?]", "", text)
            if warnings is not None:
                warnings.append(f'wildcard dropped inside phrase "{t.text}" (unsupported by OpenAlex)')
        text = " ".join(text.split())
        return f'"{text}"' if " " in text else text

    return _render(node, term)


def _inner(node: Node, term: Callable[[Term], str], or_op: str) -> str:
    parts = node.parts if isinstance(node, Or) else (node,)
    return f" {or_op} ".join(term(p) for p in parts)


def to_ebsco(node: Node, field: str = "tiab") -> str:
    """APA PsycInfo on EBSCOhost."""

    def group(n: Node) -> str:
        inner = _inner(n, _quoted, "OR")
        if field == "title":
            return f"TI ( {inner} )"
        if field == "all":
            return f"( {inner} )"
        return f"( TI ( {inner} ) OR AB ( {inner} ) )"

    return _render(node, _quoted, group=group)


_OVID_RESERVED = re.compile(r"\b(and|or|not|adj\d*|use|from)\b", re.I)


def to_ovid(node: Node, field: str = "tiab") -> str:
    """APA PsycInfo on Ovid (unquoted adjacent words are already a phrase there)."""
    suffix = {"tiab": ".ti,ab.", "title": ".ti.", "all": ".mp."}[field]

    def term(t: Term) -> str:
        return f'"{t.text}"' if t.is_phrase and _OVID_RESERVED.search(t.text) else t.text

    return _render(
        node, term, ops=("and", "or", "not"), group=lambda n: f"({_inner(n, term, 'or')}){suffix}"
    )


def to_proquest(node: Node, field: str = "tiab") -> str:
    """APA PsycInfo on ProQuest."""
    prefix = {"tiab": "TI,AB", "title": "TI", "all": ""}[field]
    return _render(node, _quoted, group=lambda n: f"{prefix}({_inner(n, _quoted, 'OR')})")


PSYCINFO_SYNTAXES: dict[str, Callable[[Node, str], str]] = {
    "ebsco": to_ebsco,
    "ovid": to_ovid,
    "proquest": to_proquest,
}


# ------------------------------------------------------- matching on text


@lru_cache(maxsize=None)
def _term_regex(text: str) -> re.Pattern:
    words = [
        re.escape(w).replace(r"\*", r"\w*").replace(r"\?", r"\w?") for w in text.split()
    ]
    return re.compile(r"(?<!\w)" + r"[\s\-]+".join(words) + r"(?!\w)", re.I)


def matches(node: Node, text: str) -> bool:
    """Evaluate the query against free text (used to score sampled records)."""
    if isinstance(node, Term):
        return bool(_term_regex(node.text).search(text))
    if isinstance(node, Not):
        return not matches(node.part, text)
    if isinstance(node, And):
        return all(matches(p, text) for p in node.parts)
    return any(matches(p, text) for p in node.parts)
