import pytest

from psychsearch.combos import blocks_from_lines, refine
from psychsearch.query import Not, Or, Term, parse
from psychsearch.redundancy import comment_out, covers, find_redundant, term_covers


@pytest.mark.parametrize("broad, narrow", [
    ("math*", "mathematics"),
    ("math*", "mathemat*"),
    ("game*", '"serious game*"'),
    ("game*", "game-based learning"),
    ('"game based"', '"serious game-based learning"'),
    ("Teen*", "teenager"),
    ("wom?n", "women"),
    ("anxiety", "anxiety"),
])
def test_term_covers(broad, narrow):
    assert term_covers(parse(broad), parse(narrow))


@pytest.mark.parametrize("broad, narrow", [
    ("mathematics", "math*"),
    ("game*", "gamifi*"),
    ("game", "games"),  # no plural folding assumed
    ('"serious game*"', "game*"),
    ('"game based"', '"based game"'),
    ("mathemat*", "math*"),
    ("wom?n", "wom*"),
])
def test_term_does_not_cover(broad, narrow):
    assert not term_covers(parse(broad), parse(narrow))


def test_covers_boolean():
    assert covers(parse('"middle school"'), parse('"middle school" AND classroom'))
    assert covers(parse("teen* OR adolescen*"), parse("(teenager OR adolescence) AND school"))
    assert not covers(parse("teen*"), parse("teenager OR youth"))
    assert covers(parse("a"), parse("a NOT b"))  # NOT only narrows
    assert not covers(parse("a NOT b"), parse("a"))
    assert not covers(parse("a"), Or((Term("a"), Not(Term("b")))))


LINES = [
    "[math]",              # 1
    "math*",               # 2
    "mathematics",         # 3
    "[gam]",               # 4
    "gamifi*",             # 5
    "game*",               # 6
    '? "serious game*"',   # 7
    "? play*",             # 8
    "? play*",             # 9
    "[pop]",               # 10
    '"middle school"',     # 11
    '"middle school" AND classroom',  # 12
    '"middle school"  # again',       # 13
]


def test_find_redundant():
    found = find_redundant(blocks_from_lines(LINES))
    assert [(r.line, r.candidate, r.reason) for r in found] == [
        (3, False, "covered by math*"),
        (7, True, "covered by game*"),
        (9, True, "covered by line 8 (same string)"),
        (12, False, 'covered by "middle school"'),
        (13, False, "covered by line 11 (same string)"),
    ]


def test_equivalent_strings_keep_the_first():
    found = find_redundant(blocks_from_lines(["[a]", "teen*", "teen* OR teen*", "TEEN*"]))
    assert [r.line for r in found] == [3, 4]


def test_covered_by_the_block_together():
    found = find_redundant(blocks_from_lines(["[a]", "teen*", "adolescen*", "teenager OR adolescent"]))
    assert [(r.line, r.reason) for r in found] == [(4, "covered by the rest of the block together")]


def test_comment_out():
    found = find_redundant(blocks_from_lines(LINES))
    out = comment_out(LINES, found)
    assert out[2] == "# redundant (covered by math*): mathematics"
    assert blocks_from_lines(out)[0].terms == [Term("math*")]
    assert find_redundant(blocks_from_lines(out)) == []


def test_refine_skips_covered_strings():
    lines = ["[pop]", "teen*", "teenager", "adolescen*", "? youth", "? teens", "[int]", "mindfulness"]
    labels = [q.label for q in refine(blocks_from_lines(lines), "pop")]
    assert labels == ["FULL: [pop] AND [int]", "FULL minus teen*", "FULL minus adolescen*", "added by youth"]
