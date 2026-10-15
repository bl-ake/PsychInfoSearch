import pytest

from psychsearch.combos import blocks_from_lines, generate, refine, with_candidates
from psychsearch.query import (
    And, Not, Or, QueryError, Term, matches, parse, to_ebsco, to_europepmc, to_generic,
    to_openalex, to_ovid, to_proquest, to_pubmed,
)


def test_parse_phrases_and_operators():
    assert parse("social media") == Term("social media")
    assert parse('"young people" OR teen*') == Or((Term("young people"), Term("teen*")))
    assert parse("acceptance and commitment therapy") == Term("acceptance and commitment therapy")
    assert parse("a AND (b OR c) NOT d") == And((Term("a"), Or((Term("b"), Term("c"))), Not(Term("d"))))
    assert parse('"social media" use') == And((Term("social media"), Term("use")))


@pytest.mark.parametrize("bad", ["", "(a OR b", 'a "b', "AND a", "a OR"])
def test_parse_errors(bad):
    with pytest.raises(QueryError):
        parse(bad)


def test_render_each_syntax():
    node = parse('(adolescen* OR "young people") AND mindfulness NOT review')
    assert to_generic(node) == '(adolescen* OR "young people") AND mindfulness NOT review'
    assert to_pubmed(node) == '(adolescen*[tiab] OR "young people"[tiab]) AND mindfulness[tiab] NOT review[tiab]'
    assert to_europepmc(node) == '(TITLE_ABS:adolescen* OR TITLE_ABS:"young people") AND TITLE_ABS:mindfulness NOT TITLE_ABS:review'
    assert to_openalex(node) == '(adolescen* OR "young people") AND mindfulness NOT review'
    assert to_ebsco(node) == (
        '( TI ( adolescen* OR "young people" ) OR AB ( adolescen* OR "young people" ) ) '
        "AND ( TI ( mindfulness ) OR AB ( mindfulness ) ) NOT ( TI ( review ) OR AB ( review ) )"
    )
    assert to_ovid(node) == "(adolescen* or young people).ti,ab. and (mindfulness).ti,ab. not (review).ti,ab."
    assert to_proquest(node) == 'TI,AB(adolescen* OR "young people") AND TI,AB(mindfulness) NOT TI,AB(review)'


def test_render_edge_cases():
    assert to_ovid(parse('"acceptance and commitment therapy"')) == '("acceptance and commitment therapy").ti,ab.'
    warnings = []
    assert to_openalex(parse('"young adult*"'), warnings) == '"young adult"'
    assert warnings
    assert to_pubmed(parse("a OR b"), "title") == "a[ti] OR b[ti]"


def test_matches():
    node = parse('(adolescen* OR "young people") AND mindfulness NOT review')
    assert matches(node, "Mindfulness for adolescents: a trial")
    assert matches(node, "Mindfulness in young-people")
    assert not matches(node, "Mindfulness for adolescents: a review")
    assert not matches(node, "Mindfulness for preadolescents")


GROUPED = ["[pop]", "teen*", "adolescen*", "", "[int]", "mindfulness  # comment", "meditation", "[out]", "anxiety"]


def test_generate_grouped():
    blocks = blocks_from_lines(GROUPED)
    assert [b.name for b in blocks] == ["pop", "int", "out"]
    queries = generate(blocks, ("singles", "blocks", "and"))
    by_strategy = {}
    for q in queries:
        by_strategy.setdefault(q.strategy, []).append(q.label)
    assert len(by_strategy["singles"]) == 5
    assert by_strategy["blocks"] == ["[pop]", "[int]"]  # [out] is a single string, already covered
    assert by_strategy["and"] == ["[pop] AND [int]", "[pop] AND [out]", "[int] AND [out]", "[pop] AND [int] AND [out]"]
    assert len({q.node for q in queries}) == len(queries)


def test_generate_cross_and_drop_one():
    blocks = blocks_from_lines(GROUPED)
    assert len(generate(blocks, ("cross",))) == 4
    drop = generate(blocks, ("drop-one",))
    assert drop[0].label.startswith("FULL: ")
    assert [q.label for q in drop[1:]] == ["FULL minus teen*", "FULL minus adolescen*", "FULL minus mindfulness", "FULL minus meditation"]
    assert all(q.baseline == drop[0].node for q in drop[1:])


REFINING = ["[pop]", "teen*", "adolescen*", "? youth", "? teen*", "[int]", "mindfulness", "? meditation"]


def test_candidates_are_kept_apart():
    blocks = blocks_from_lines(REFINING)
    assert blocks[0].terms == [Term("teen*"), Term("adolescen*")]
    assert blocks[0].candidates == [Term("youth"), Term("teen*")]
    labels = [q.label for q in generate(blocks, ("singles", "blocks", "and"))]
    assert not any("youth" in label or "meditation" in label for label in labels)
    with pytest.raises(QueryError, match="line 1"):
        blocks_from_lines(["? youth", "teen*"])


def test_refine():
    blocks = blocks_from_lines(REFINING)
    queries = refine(blocks, "POP")
    full = parse("(teen* OR adolescen*) AND mindfulness")
    assert queries[0].node == full
    assert [q.label for q in queries[1:]] == ["FULL minus teen*", "FULL minus adolescen*", "added by youth"]  # teen* is already a term
    assert queries[1].node == parse("adolescen* AND mindfulness") and queries[1].baseline == full
    assert queries[3].node == parse("youth AND mindfulness NOT (teen* OR adolescen*)")
    assert queries[3].baseline is None
    # a one-string block has nothing to drop; its candidate goes in the block's place
    assert [q.node for q in refine(blocks, "int")[1:]] == [parse("(teen* OR adolescen*) AND meditation NOT mindfulness")]
    with pytest.raises(QueryError, match=r"\[pop\], \[int\]"):
        refine(blocks, "outcome")


def test_with_candidates():
    scoring = with_candidates(blocks_from_lines(REFINING))
    assert scoring[0].terms == [Term("teen*"), Term("adolescen*"), Term("youth")]
    assert matches(scoring[1].node, "a meditation study")


def test_generate_flat_list():
    blocks = blocks_from_lines(["mindfulness", "anxiety", "teen* OR adolescen*"])
    labels = [q.label for q in generate(blocks, ("singles", "and"), max_size=2)]
    assert labels == [
        "mindfulness", "anxiety", "teen* OR adolescen*",
        "mindfulness AND anxiety", "mindfulness AND (teen* OR adolescen*)", "anxiety AND (teen* OR adolescen*)",
    ]
