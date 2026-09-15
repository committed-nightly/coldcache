from __future__ import annotations

import pytest

from coldcache.templates import (
    MATCH,
    NO_MATCH,
    UNKNOWN,
    Alphabet,
    Expr,
    Text,
    canonical,
    forms,
    parse,
    prefix_match,
    rendered,
    same,
    split_lines,
)


def atoms(template, values=None):
    return forms(parse(template), values or {}, ALPHABET)


ALPHABET = Alphabet()


class TestCanonical:
    def test_whitespace_goes(self):
        assert canonical(" github . sha ") == "github.sha"

    def test_case_goes_outside_strings(self):
        assert canonical("RUNNER.OS") == "runner.os"

    def test_case_stays_inside_strings(self):
        assert canonical("hashFiles('Gemfile.Lock')") == "hashfiles('Gemfile.Lock')"

    def test_whitespace_stays_inside_strings(self):
        assert canonical("hashFiles( 'a b' )") == "hashfiles('a b')"

    def test_doubled_quote_is_an_escape(self):
        assert canonical("format('it''s', A)") == "format('it''s',a)"

    def test_two_spellings_of_one_expression_agree(self):
        assert canonical("${{runner.os}}"[3:-2]) == canonical(" RUNNER.os ")


class TestParse:
    def test_plain_text(self):
        assert parse("npm-cache") == [Text("npm-cache")]

    def test_one_expression(self):
        assert parse("a-${{ github.sha }}") == [
            Text("a-"),
            Expr("github.sha", "github.sha"),
        ]

    def test_closing_braces_inside_a_string_do_not_close(self):
        parts = parse("${{ hashFiles('a}}b') }}-x")
        assert parts == [Expr("hashfiles('a}}b')", "hashFiles('a}}b')"), Text("-x")]

    def test_unterminated_expression_is_literal(self):
        assert parse("a-${{ github.sha") == [Text("a-${{ github.sha")]

    def test_round_trip(self):
        assert rendered(parse("a-${{ github.sha }}-b")) == "a-${{ github.sha }}-b"


class TestPrefixMatch:
    def test_a_literal_prefix_matches(self):
        assert prefix_match(atoms("os-node-"), atoms("os-node-1234")) == MATCH

    def test_a_prefix_before_an_expression_matches(self):
        assert (
            prefix_match(
                atoms("${{ runner.os }}-npm-"),
                atoms("${{ runner.os }}-npm-${{ hashFiles('a') }}"),
            )
            == MATCH
        )

    def test_a_different_literal_does_not(self):
        assert (
            prefix_match(
                atoms("${{ runner.os }}-yarn-"),
                atoms("${{ runner.os }}-npm-${{ hashFiles('a') }}"),
            )
            == NO_MATCH
        )

    def test_the_same_expression_is_the_same_atom(self):
        assert (
            prefix_match(atoms("${{ RUNNER.OS }}-a"), atoms("${{ runner.os }}-ab"))
            == MATCH
        )

    def test_an_expression_against_text_is_undecided(self):
        # `Linux-npm-` is right on ubuntu and wrong on windows, and nothing
        # in the file says which. Saying "no" here would be a false positive
        # in most repositories.
        assert (
            prefix_match(atoms("Linux-npm-"), atoms("${{ runner.os }}-npm-x"))
            == UNKNOWN
        )

    def test_resolving_the_expression_decides_it(self):
        values = {"runner.os": frozenset({"Linux"})}
        assert (
            prefix_match(
                atoms("Linux-npm-"), atoms("${{ runner.os }}-npm-x", values)
            )
            == MATCH
        )

    def test_resolving_the_expression_can_also_decide_against(self):
        values = {"runner.os": frozenset({"Windows"})}
        assert (
            prefix_match(
                atoms("Linux-npm-"), atoms("${{ runner.os }}-npm-x", values)
            )
            == NO_MATCH
        )

    def test_a_longer_prefix_than_the_key_does_not_match(self):
        assert prefix_match(atoms("os-node-cache"), atoms("os-node-")) == NO_MATCH

    def test_unless_the_key_ends_in_an_expression(self):
        assert (
            prefix_match(atoms("os-node-cache"), atoms("os-node-${{ github.sha }}"))
            == UNKNOWN
        )

    def test_a_key_that_is_its_own_prefix_matches(self):
        assert prefix_match(atoms("abc"), atoms("abc")) == MATCH


class TestSame:
    def test_identical_templates(self):
        key = "${{ runner.os }}-go-${{ hashFiles('go.sum') }}"
        assert same(atoms(key), atoms(key)) == MATCH

    def test_different_literals(self):
        assert same(atoms("a-1"), atoms("a-2")) == NO_MATCH

    def test_overlapping_matrix_values_are_the_same_string_somewhere(self):
        left = atoms("${{ matrix.os }}-x", {"matrix.os": frozenset({"a", "b"})})
        right = atoms("${{ matrix.tag }}-x", {"matrix.tag": frozenset({"b", "c"})})
        assert same(left, right) == MATCH

    def test_disjoint_matrix_values_are_not(self):
        left = atoms("${{ matrix.os }}-x", {"matrix.os": frozenset({"a"})})
        right = atoms("${{ matrix.tag }}-x", {"matrix.tag": frozenset({"b"})})
        assert same(left, right) == NO_MATCH

    def test_an_opaque_expression_makes_it_undecided(self):
        assert same(atoms("${{ env.PREFIX }}-x"), atoms("v2-x")) == UNKNOWN

    def test_different_lengths_with_no_expressions_are_not_the_same(self):
        assert same(atoms("abc"), atoms("abcd")) == NO_MATCH


class TestForms:
    def test_a_known_value_is_substituted(self):
        assert atoms("${{ runner.os }}-x", {"runner.os": frozenset({"Linux"})}) == [
            "Linux-x"
        ]

    def test_every_combination_is_produced(self):
        got = atoms(
            "${{ matrix.os }}-${{ matrix.py }}",
            {
                "matrix.os": frozenset({"a", "b"}),
                "matrix.py": frozenset({"1", "2"}),
            },
        )
        assert sorted(got) == ["a-1", "a-2", "b-1", "b-2"]

    def test_too_many_combinations_falls_back_to_an_atom(self):
        values = {"matrix.x": frozenset(str(i) for i in range(100))}
        got = forms(parse("${{ matrix.x }}"), values, Alphabet(), limit=8)
        assert len(got) == 1

    def test_an_unknown_expression_is_one_character(self):
        assert len(atoms("${{ github.sha }}")) == 1
        assert len(atoms("${{ github.sha }}")[0]) == 1


@pytest.mark.parametrize(
    "left,right",
    [
        ("${{ hashFiles('a') }}", "${{ hashFiles( 'a' ) }}"),
        ("${{ github.sha }}", "${{ GITHUB.SHA }}"),
    ],
)
def test_equivalent_spellings_share_an_atom(left, right):
    alphabet = Alphabet()
    assert forms(parse(left), {}, alphabet) == forms(parse(right), {}, alphabet)


class TestSplitLines:
    def test_plain_lines(self):
        assert split_lines("one-\ntwo-\n") == ["one-", "two-"]

    def test_a_newline_inside_an_expression_is_not_a_separator(self):
        # home-assistant/core writes a restore-key exactly like this. It is
        # one key; splitting it first gives three, all of them nonsense.
        text = (
            "${{ runner.os }}-${{ runner.arch }}-mypy-${{\n"
            "env.MYPY_CACHE_VERSION }}-${{\n"
            "env.HA_SHORT_VERSION }}-\n"
        )
        assert len(split_lines(text)) == 1

    def test_blank_lines_go(self):
        assert split_lines("a\n\n  \nb") == ["a", "b"]

    def test_an_unterminated_expression_still_splits(self):
        assert split_lines("${{ oops\nb") == ["${{ oops", "b"]
