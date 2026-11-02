"""The working strategy: blocks of strings as plain JSON, checked and turned into queries and terms files.

The state is ``[{"name": str, "on": bool, "terms": [{"text": str, "on": bool}]}]``, so it can be logged,
snapshotted and restored as is.
"""

from __future__ import annotations

import re

from ..combos import Block
from ..query import And, Node, Not, Or, QueryError, all_of, parse, to_generic

LINE = 1000  # synthetic line numbers for find_redundant: block index * LINE + string index


class UIError(Exception):
    """A request the page cannot carry out; the message is shown to the user."""


def state_from_blocks(blocks: list[Block]) -> list[dict]:
    """The working state for a terms file: '?' candidates start switched off."""
    state = []
    for number, block in enumerate(blocks, 1):
        terms: list[dict] = []
        for _, node, candidate in block.entries:
            text = to_generic(node)
            if not any(t["text"] == text for t in terms):
                terms.append({"text": text, "on": not candidate})
        state.append({"name": block.name or f"block {number}", "on": True, "terms": terms})
    return state


def state_from_string(text: str) -> list[dict]:
    """The working state for a pasted search string such as ``(a OR b) AND (c OR d)``: each part
    joined by AND becomes a block, named block 1, block 2 and so on, and each part of it joined by OR
    becomes one of its strings, switched on."""
    no_not = UIError("NOT cannot be loaded, since a block's strings are ORed and blocks are ANDed; "
                     "take the NOT part out of the string and load it again")
    if not text.strip():
        raise UIError("paste a search string first")
    try:
        node = parse(text)
    except QueryError as exc:
        if re.search(r"\bNOT\b", re.sub(r'"[^"]*"', "", text)):  # "a AND NOT b" fails to parse
            raise no_not from None
        raise UIError(str(exc)) from None
    state = []
    for number, part in enumerate(node.parts if isinstance(node, And) else (node,), 1):
        if isinstance(part, Not):  # "a NOT b"
            raise no_not
        terms: list[dict] = []
        for term in part.parts if isinstance(part, Or) else (part,):
            text = clean_term(to_generic(term))
            if not any(t["text"] == text for t in terms):
                terms.append({"text": text, "on": True})
        state.append({"name": f"block {number}", "on": True, "terms": terms})
    return state


def to_blocks(state: list[dict], include_off: bool = False) -> list[Block]:
    """Blocks with at least one string switched on; switched-off strings become candidates.
    Blocks that are switched off are left out unless `include_off`."""
    blocks = []
    for bi, b in enumerate(state):
        entries = [(bi * LINE + ti, parse(t["text"]), not t["on"]) for ti, t in enumerate(b["terms"])]
        terms = list(dict.fromkeys(n for _, n, off in entries if not off))
        if terms and (b["on"] or include_off):
            candidates = list(dict.fromkeys(n for _, n, off in entries if off and n not in terms))
            blocks.append(Block(b["name"], terms, candidates, entries))
    return blocks


def full_node(state: list[dict]) -> Node | None:
    blocks = to_blocks(state)
    return all_of(b.node for b in blocks) if blocks else None


def strategy_text(state: list[dict]) -> str:
    node = full_node(state)
    return to_generic(node) if node is not None else ""


def terms_file_text(state: list[dict], note: str) -> str:
    """The state as a terms file. Switched-off strings are '?' candidates; a block with nothing
    switched on is commented out, since a block needs at least one string."""
    lines = [f"# {note}", "# Strings marked '?' are switched off; commented-out blocks were switched off.", ""]
    for b in state:
        if b["on"] and any(t["on"] for t in b["terms"]):
            lines.append(f"[{b['name']}]")
            lines += [t["text"] if t["on"] else f"? {t['text']}" for t in b["terms"]]
        else:
            lines.append(f"# [{b['name']}] (switched off)")
            lines += [f"# {t['text']}" for t in b["terms"]]
        lines.append("")
    return "\n".join(lines)


def clean_term(text: str) -> str:
    text = " ".join(text.split())
    if not text:
        raise UIError("type a search string first")
    if "#" in text:
        raise UIError("'#' starts a comment in the terms file, so it cannot be part of a string")
    if text.startswith("?") or re.fullmatch(r"\[.*\]", text):
        raise UIError("a string cannot start with '?' or be wrapped in [ ] (those mark candidates and block names in the terms file)")
    try:
        return to_generic(parse(text))
    except QueryError as exc:
        raise UIError(str(exc)) from None


def clean_name(name: str, state: list[dict]) -> str:
    name = " ".join(name.split())
    if not name or re.search(r"[\[\]#]", name):
        raise UIError("a block name cannot be empty or contain [, ] or #")
    if any(b["name"].lower() == name.lower() for b in state):
        raise UIError(f"there is already a block [{name}]")
    return name
