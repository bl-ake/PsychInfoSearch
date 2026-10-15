from psychsearch.combos import blocks_from_lines, generate
from psychsearch.hasse import Item, build_figure, build_order, implies
from psychsearch.query import parse, to_generic


def test_implies():
    assert implies(parse("a AND b"), parse("a"))
    assert not implies(parse("a"), parse("a AND b"))
    assert implies(parse("a"), parse("a OR b"))
    assert implies(parse("(a OR b) AND c"), parse("(a OR b OR d) AND (c OR e)"))
    assert not implies(parse("a OR b"), parse("a"))
    assert implies(parse("a NOT b"), parse("a"))
    assert not implies(parse("a"), parse("a NOT b"))


def _run(lines, strategies, counts=None):
    queries = generate(blocks_from_lines(lines), strategies)
    return {
        "known_items": {},
        "queries": [
            {"n": n, "label": q.label, "strategy": q.strategy, "query": to_generic(q.node),
             "backends": {"pubmed": {"result": {"count": (counts or {}).get(q.label, 10 * n), "error": ""}}}}
            for n, q in enumerate(queries, 1)
        ],
    }


LINES = ["[pop]", "teen*", "adolescen*", "[int]", "mindfulness", "[out]", "anxiety"]


def test_covers_form_the_subset_lattice():
    run = _run(LINES, ("blocks", "and"))
    items = [Item(q["n"], q["label"], q["strategy"], q["query"], parse(q["query"]), q["backends"]) for q in run["queries"]]
    items, undecided = build_order(items)
    label = lambda i: items[i].label
    parents = {item.label: sorted(label(p) for p in item.parents) for item in items}
    assert undecided == 0
    assert parents["[pop]"] == []
    assert parents["[pop] AND [int]"] == ["[int]", "[pop]"]
    # the triple sits under the three pairs only, not under the single blocks (transitive reduction)
    assert parents["[pop] AND [int] AND [out]"] == ["[int] AND [out]", "[pop] AND [int]", "[pop] AND [out]"]
    assert {item.label: item.level for item in items}["[pop] AND [int] AND [out]"] == 2


def test_figure_flags_counts_that_break_containment():
    counts = {"[pop]": 100, "[int]": 50, "[out]": 80, "[pop] AND [int]": 70}  # 70 > 50: impossible
    figure = build_figure(_run(LINES, ("blocks", "and"), counts))
    names = [trace.name for trace in figure.data]
    assert any("MORE hits" in (name or "") for name in names)
    assert sum(len(t.x) for t in figure.data if t.mode == "markers+text") == 7
