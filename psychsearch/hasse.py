"""Hasse diagram of the search strings in a run, drawn with Plotly.

The order is logical containment: query A sits below query B when everything A can retrieve, B
retrieves too (``a AND b`` is below ``a``; ``a`` is below ``a OR b``). An edge joins a query to
each next-broader query with nothing in between. Containment is worked out from the Boolean
structure alone, treating each search term as independent, so ``teen*`` and ``teenager`` are
not recognised as related.

    uv run psychsearch-hasse                      # latest run in results/ of the psychsearch folder
    uv run psychsearch-hasse results/20261013-114144 -b ebscohost -s singles,blocks,and

`build_figure` returns a plotly Figure, so it can be reused in a notebook or a Dash app.
"""

from __future__ import annotations

import argparse
import html
import json
import math
import sys
import textwrap
from dataclasses import dataclass, field
from itertools import product
from pathlib import Path

import plotly.graph_objects as go

from . import home
from .backends import BACKENDS
from .query import And, Node, Not, Or, QueryError, Term, parse

MAX_CONJUNCTIONS = 50_000  # refuse to expand a query further than this when comparing
MAX_TRUTH_TABLE_TERMS = 16
STAGGER_ABOVE = 7  # layers wider than this are split over two rows
STAGGER_DROP = 0.36  # how far the second row sits below the first, in levels

SYMBOLS = {
    "singles": "circle",
    "blocks": "square",
    "and": "diamond",
    "or": "triangle-up",
    "cross": "hexagon",
    "drop-one": "triangle-down",
    "refine": "pentagon",
    "custom": "star",
}
# Colour roles. Hits use one hue (light = few, dark = many on the light theme); strategy is
# carried by marker shape, so the diagram never relies on telling several hues apart.
THEMES = {
    "light": {
        "surface": "#fcfcfb", "ink": "#0b0b0b", "ink2": "#52514e", "muted": "#898781",
        "edge": "#c3c2b7", "flag": "#d03b3b",
        "ramp": ["#86b6ef", "#6da7ec", "#5598e7", "#3987e5", "#2a78d6", "#256abf", "#1c5cab", "#184f95", "#104281", "#0d366b"],
    },
    "dark": {
        "surface": "#1a1a19", "ink": "#f0efec", "ink2": "#c3c2b7", "muted": "#898781",
        "edge": "#52514e", "flag": "#d03b3b",
        "ramp": ["#184f95", "#1c5cab", "#256abf", "#2a78d6", "#3987e5", "#5598e7", "#6da7ec", "#86b6ef", "#9ec5f4", "#b7d3f6"],
    },
}
FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif'


# ------------------------------------------------------------ containment


def _terms(node: Node) -> set[Term]:
    if isinstance(node, Term):
        return {node}
    if isinstance(node, Not):
        return _terms(node.part)
    return set().union(*(_terms(p) for p in node.parts))


def _has_not(node: Node) -> bool:
    if isinstance(node, Term):
        return False
    return isinstance(node, Not) or any(_has_not(p) for p in node.parts)


def _holds(node: Node, true_terms: frozenset) -> bool:
    if isinstance(node, Term):
        return node in true_terms
    if isinstance(node, Not):
        return not _holds(node.part, true_terms)
    if isinstance(node, And):
        return all(_holds(p, true_terms) for p in node.parts)
    return any(_holds(p, true_terms) for p in node.parts)


def _conjunctions(node: Node) -> set[frozenset]:
    """Disjunctive normal form of a NOT-free query: the term sets that each satisfy it."""
    if isinstance(node, Term):
        return {frozenset([node])}
    parts = [_conjunctions(p) for p in node.parts]
    if isinstance(node, Or):
        return set().union(*parts)
    size = math.prod(len(p) for p in parts)
    if size > MAX_CONJUNCTIONS:
        raise OverflowError
    return {frozenset().union(*combo) for combo in product(*parts)}


def implies(a: Node, b: Node) -> bool | None:
    """True if every record matching `a` must match `b`. None if the pair is too large to decide."""
    try:
        if not _has_not(a) and not _has_not(b):
            return all(_holds(b, conj) for conj in _conjunctions(a))
    except OverflowError:
        return None
    terms = sorted(_terms(a) | _terms(b), key=lambda t: t.text)
    if len(terms) > MAX_TRUTH_TABLE_TERMS:
        return None
    for mask in range(1 << len(terms)):
        chosen = frozenset(t for i, t in enumerate(terms) if mask >> i & 1)
        if _holds(a, chosen) and not _holds(b, chosen):
            return False
    return True


# ------------------------------------------------------------ the order


@dataclass
class Item:
    n: int
    label: str
    strategy: str
    query: str
    node: Node
    data: dict  # per-backend outcome from results.json
    same_as: list[str] = field(default_factory=list)  # labels of logically identical queries
    parents: list[int] = field(default_factory=list)  # indices of covering (next-broader) items
    level: int = 0
    x: float = 0.0
    y: float = 0.0

    def hits(self, backend: str) -> int | None:
        return ((self.data.get(backend) or {}).get("result") or {}).get("count")


def build_order(items: list[Item]) -> tuple[list[Item], int]:
    """Merge logically identical queries, then give each item its covering parents and level.

    Returns the surviving items and how many pairs could not be compared."""
    below = {}  # (i, j) -> items[i] implies items[j]
    undecided = 0
    for i, a in enumerate(items):
        for j, b in enumerate(items):
            if i != j:
                verdict = implies(a.node, b.node)
                undecided += verdict is None
                below[i, j] = bool(verdict)

    keep: list[int] = []
    for i, item in enumerate(items):
        twin = next((k for k in keep if below[i, k] and below[k, i]), None)
        if twin is None:
            keep.append(i)
        else:
            items[twin].same_as.append(item.label)

    strictly = {(i, j) for i in keep for j in keep if i != j and below[i, j] and not below[j, i]}
    kept = [items[i] for i in keep]
    position = {i: p for p, i in enumerate(keep)}
    for i in keep:
        above = [j for j in keep if (i, j) in strictly]
        covers = [j for j in above if not any((i, k) in strictly and (k, j) in strictly for k in above)]
        items[i].parents = [position[j] for j in covers]

    def level(item: Item, seen: dict) -> int:
        if id(item) not in seen:
            seen[id(item)] = 1 + max((level(kept[p], seen) for p in item.parents), default=-1)
        return seen[id(item)]

    seen: dict = {}
    for item in kept:
        item.level = level(item, seen)
    return kept, undecided // 2


def lay_out(items: list[Item]) -> None:
    """Assign x (ordered within each level to shorten edges) and y (the containment level)."""
    layers: dict[int, list[int]] = {}
    for index, item in enumerate(items):
        layers.setdefault(item.level, []).append(index)
    children: dict[int, list[int]] = {i: [] for i in range(len(items))}
    for index, item in enumerate(items):
        for parent in item.parents:
            children[parent].append(index)

    slot = {i: (k + 0.5) / len(layer) for layer in layers.values() for k, i in enumerate(layer)}
    for sweep in range(6):
        downward = sweep % 2 == 0
        for depth in sorted(layers, reverse=not downward):
            def centre(i: int) -> float:
                neighbours = items[i].parents if downward else children[i]
                return sum(slot[n] for n in neighbours) / len(neighbours) if neighbours else slot[i]

            layers[depth].sort(key=centre)
            for k, i in enumerate(layers[depth]):
                slot[i] = (k + 0.5) / len(layers[depth])

    for depth, layer in layers.items():
        staggered = len(layer) > STAGGER_ABOVE
        step = 0.5 if staggered else 1.0
        for k, i in enumerate(layer):
            item = items[i]
            item.x = (k - (len(layer) - 1) / 2) * step
            item.y = -depth - (STAGGER_DROP if staggered and k % 2 else 0.0)


# ------------------------------------------------------------- the figure


def _wrap(text: str, width: int, max_lines: int) -> str:
    lines = textwrap.wrap(text, width) or [""]
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1][: width - 1].rstrip() + "…"
    return "<br>".join(html.escape(line, quote=False) for line in lines)


def _num(value: int | None) -> str:
    return "no count" if value is None else f"{value:,}"


def _hover(item: Item, items: list[Item], backend: str, backends: list[str], known: dict) -> str:
    lines = [f"<b>{_wrap(item.label, 70, 3)}</b>", f"{item.strategy}", ""]
    lines.append(_wrap(item.query, 80, 6))
    lines.append("")
    for name in backends:
        outcome = item.data.get(name) or {}
        result = outcome.get("result") or {}
        row = f"{name}: <b>{_num(result.get('count'))}</b> hits"
        if result.get("error"):
            row = f"{name}: error"
        if outcome.get("known_found") is not None:
            row += f" · known {outcome['known_found']}/{len((known.get(name) or {}).get('indexed') or {})}"
        if outcome.get("on_topic") is not None:
            row += f" · on-topic {outcome['on_topic']:.0%}"
        lines.append(row)
    mine = item.hits(backend)
    for parent in item.parents[:5]:
        broader = items[parent].hits(backend)
        if mine is not None and broader:
            lines.append(f"keeps {mine / broader:.1%} of: {_wrap(items[parent].label, 60, 1)}")
    if item.same_as:
        lines.append("same as: " + _wrap("; ".join(item.same_as), 70, 2))
    return "<br>".join(lines)


def load_items(run: dict, strategies: set[str] | None = None) -> list[Item]:
    items = []
    for entry in run["queries"]:
        if strategies and entry["strategy"] not in strategies:
            continue
        items.append(Item(entry["n"], entry["label"], entry["strategy"], entry["query"], parse(entry["query"]), entry["backends"]))
    return items


def build_figure(
    run: dict,
    backend: str | None = None,
    strategies: set[str] | None = None,
    theme: str = "light",
) -> go.Figure:
    """Build the diagram from a loaded results.json.

    backend     whose hit counts to colour by (default: the first PsycInfo backend, else the first)
    strategies  keep only queries from these strategies
    """
    backends = list(run["queries"][0]["backends"]) if run["queries"] else []
    if backend is None:
        backend = next((b for b in backends if b.startswith("ebsco")), backends[0] if backends else "")
    if backend not in backends:
        raise QueryError(f"backend {backend!r} is not in this run; it has: {', '.join(backends) or 'none'}")
    items = load_items(run, strategies)
    if not items:
        raise QueryError("no queries to draw (check --strategies)")
    items, undecided = build_order(items)
    lay_out(items)
    colors = THEMES[theme]

    counts = [item.hits(backend) for item in items]
    logs = [math.log10(c + 1) for c in counts if c is not None]
    low, high = (min(logs), max(logs)) if logs else (0.0, 1.0)
    if high - low < 1e-9:
        high = low + 1.0

    figure = go.Figure()
    plain, flagged = [[], []], [[], []]
    for item in items:
        for parent in item.parents:
            broader = items[parent]
            mine, theirs = item.hits(backend), broader.hits(backend)
            target = flagged if mine is not None and theirs is not None and mine > theirs else plain
            target[0] += [item.x, broader.x, None]
            target[1] += [item.y, broader.y, None]
    figure.add_trace(go.Scatter(x=plain[0], y=plain[1], mode="lines", hoverinfo="skip", showlegend=False,
                                line={"color": colors["edge"], "width": 1.5}))
    if flagged[0]:
        figure.add_trace(go.Scatter(x=flagged[0], y=flagged[1], mode="lines", hoverinfo="skip",
                                    name="narrower query has MORE hits (check)", line={"color": colors["flag"], "width": 2}))

    ramp = colors["ramp"]
    scale = [[k / (len(ramp) - 1), c] for k, c in enumerate(ramp)]
    known = run.get("known_items") or {}
    for strategy in dict.fromkeys(item.strategy for item in items):
        for counted in (True, False):
            group = [i for i in items if i.strategy == strategy and (i.hits(backend) is not None) == counted]
            if not group:
                continue
            marker = {"symbol": SYMBOLS.get(strategy, "circle"), "size": 17, "line": {"color": colors["surface"], "width": 2}}
            if counted:
                marker |= {"color": [math.log10(i.hits(backend) + 1) for i in group], "coloraxis": "coloraxis"}
            else:
                marker |= {"color": colors["surface"], "line": {"color": colors["muted"], "width": 1.5}}
            figure.add_trace(go.Scatter(
                x=[i.x for i in group], y=[i.y for i in group], mode="markers+text",
                name=strategy if counted else f"{strategy} (no count)", marker=marker,
                text=[f"{_wrap(i.label, 22, 2)}<br><b>{_num(i.hits(backend))}</b>" for i in group],
                textposition="bottom center", textfont={"size": 11, "color": colors["ink2"], "family": FONT},
                hovertext=[_hover(i, items, backend, backends, known) for i in group], hoverinfo="text",
                customdata=[[i.n, i.label, i.query] for i in group],
            ))

    ticks = list(range(math.floor(low), math.ceil(high) + 1))
    label = BACKENDS[backend].label if backend in BACKENDS else backend
    levels = max(i.level for i in items) + 1
    widest = max(abs(i.x) for i in items) * 2 + 1
    note = "Lower = narrower. A line joins a query to the next broader query that contains it."
    if undecided:
        note += f" {undecided} pair(s) were too large to compare and are shown unconnected."
    figure.update_layout(
        title={"text": f"Search strings ordered by containment, with {label} hits<br><sup>{note}</sup>",
               "font": {"size": 17, "color": colors["ink"], "family": FONT}, "x": 0.01, "xanchor": "left"},
        paper_bgcolor=colors["surface"], plot_bgcolor=colors["surface"],
        font={"family": FONT, "color": colors["ink2"], "size": 12},
        width=int(min(2800, max(960, widest * 165 + 260))), height=int(min(2600, max(540, levels * 225 + 200))),
        margin={"l": 40, "r": 40, "t": 90, "b": 70}, hovermode="closest",
        hoverlabel={"bgcolor": colors["surface"], "bordercolor": colors["edge"], "align": "left",
                    "font": {"family": FONT, "color": colors["ink"], "size": 12}},
        legend={"orientation": "h", "x": 0, "y": -0.02, "yanchor": "top", "title": {"text": "Shape = strategy  "},
                "font": {"color": colors["ink2"]}, "itemsizing": "constant"},
        coloraxis={"colorscale": scale, "cmin": low, "cmax": high,
                   "colorbar": {"title": {"text": "hits (log scale)", "side": "right"}, "tickvals": ticks,
                                "ticktext": [f"{10 ** t:,}" for t in ticks], "thickness": 12, "len": 0.6,
                                "outlinewidth": 0, "tickfont": {"color": colors["muted"]}}},
        xaxis={"visible": False, "range": [-(widest / 2 + 0.4), widest / 2 + 0.4]},
    )
    figure.update_yaxes(visible=False, range=[-(levels - 1) - 0.85, 0.3])
    return figure


# -------------------------------------------------------------------- CLI


def _find_run(path: Path | None) -> Path:
    if path is None:
        runs = sorted(Path("results").glob("*/results.json"))
        if not runs:
            raise QueryError("no runs found in results/ of the psychsearch folder; pass the path to a results.json or its folder")
        return runs[-1]
    path = path / "results.json" if path.is_dir() else path
    if not path.is_file():
        raise QueryError(f"{path} not found")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="psychsearch-hasse", description="Draw a run's search strings as an interactive Hasse diagram.")
    parser.add_argument("run", nargs="?", type=Path, help="a results.json or the run folder holding it (default: latest in results/ of the psychsearch folder)")
    parser.add_argument("-b", "--backend", help="backend whose hits colour the nodes (default: PsycInfo if present)")
    parser.add_argument("-s", "--strategies", help="comma-separated strategies to include (default: all in the run)")
    parser.add_argument("--theme", choices=tuple(THEMES), default="light")
    parser.add_argument("--embed", action="store_true", help="embed plotly.js in the file so it works offline (about 5 MB)")
    parser.add_argument("-o", "--out", type=Path, help="output HTML file (default: hasse.html beside the results)")
    args = parser.parse_args(argv)
    args.run = home.find(args.run) if args.run else None
    home.enter()
    try:
        source = _find_run(args.run)
        run = json.loads(source.read_text(encoding="utf-8"))
        strategies = {s.strip() for s in args.strategies.split(",") if s.strip()} if args.strategies else None
        figure = build_figure(run, args.backend, strategies, args.theme)
    except QueryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    out = args.out or source.with_name("hasse.html")
    figure.write_html(out, include_plotlyjs=True if args.embed else "cdn", full_html=True)
    nodes = sum(len(trace.x) for trace in figure.data if trace.mode == "markers+text")
    print(f"Wrote {out} ({nodes} queries)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
