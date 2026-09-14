"""Keys are templates, and every question here is whether two of them can meet.

A cache key is a string with `${{ }}` expressions in it, and every check in
this tool reduces to one of two questions: is this key the same string as that
one, and is this restore-key a *prefix* of that one. Both get asked about the
template, because a tool that reads a repository off disk does not have the
values.

The catch is that an expression is not a wildcard. `${{ runner.os }}` is one
of three strings, `${{ env.PREFIX }}` is whatever the workflow set, and
`${{ hashFiles('x') }}` is sixty-four hex characters or nothing at all. So a
comparison has three answers, not two: yes, no, and "an expression sits on the
boundary and could go either way". The third is reported as not-checked rather
than guessed. A cache linter that cries wolf gets uninstalled on the second
run, and unlike a security linter it has nothing to show for the noise.

The representation: every distinct expression gets one character from the
Unicode private use area, so a template becomes a plain string and a prefix
test becomes `str.startswith`. Two expressions are the same atom when their
sources match after whitespace and case are normalised -- `${{ runner.os }}`
and `${{RUNNER.OS}}` are the same thing, and GitHub agrees.
"""

from __future__ import annotations

from dataclasses import dataclass

OPEN = "${{"
CLOSE = "}}"

#: Comparison outcomes. UNKNOWN means an expression is in the way, not that
#: the answer is probably no.
MATCH = "match"
NO_MATCH = "no-match"
UNKNOWN = "unknown"

#: Private use area. One character per distinct expression.
ATOM_FIRST = 0xE000
ATOM_LAST = 0xF8FF


def is_atom(char: str) -> bool:
    return ATOM_FIRST <= ord(char) <= ATOM_LAST


@dataclass(frozen=True)
class Text:
    """Literal text between expressions."""

    text: str


@dataclass(frozen=True)
class Expr:
    """One `${{ ... }}`, normalised."""

    #: Whitespace-stripped, lowercased outside string literals. The identity.
    source: str
    #: As written, for putting in a message.
    raw: str


Part = Text | Expr


def canonical(source: str) -> str:
    """An expression's identity: no whitespace, lowercase outside strings.

    GitHub's expression syntax is case-insensitive for contexts and function
    names, and there is nowhere in it that two identifiers may be adjacent
    with only whitespace between them, so dropping whitespace outright cannot
    join two tokens that were separate. String literals are left exactly as
    written, because `hashFiles('Gemfile.lock')` is a path and paths are not
    case-insensitive anywhere that matters.
    """
    out: list[str] = []
    i = 0
    n = len(source)
    while i < n:
        ch = source[i]
        if ch == "'":
            out.append("'")
            i += 1
            while i < n:
                if source[i] == "'":
                    if i + 1 < n and source[i + 1] == "'":
                        out.append("''")
                        i += 2
                        continue
                    out.append("'")
                    i += 1
                    break
                out.append(source[i])
                i += 1
        elif ch.isspace():
            i += 1
        else:
            out.append(ch.lower())
            i += 1
    return "".join(out)


def _find_close(template: str, start: int) -> int | None:
    """Index of the `}}` that closes an expression opened before `start`.

    A `}}` inside a single-quoted string does not close anything. This is not
    a hypothetical: a glob with a brace in it goes inside quotes.
    """
    i = start
    n = len(template)
    while i < n:
        ch = template[i]
        if ch == "'":
            i += 1
            while i < n:
                if template[i] == "'":
                    if i + 1 < n and template[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue
        if template.startswith(CLOSE, i):
            return i
        i += 1
    return None


def parse(template: str) -> list[Part]:
    """A key template as alternating literal text and expressions.

    An unterminated `${{` is literal text. GitHub would fail the workflow over
    it; this tool is not a syntax checker and says nothing about it.
    """
    parts: list[Part] = []
    buf: list[str] = []
    i = 0
    n = len(template)
    while i < n:
        if template.startswith(OPEN, i):
            end = _find_close(template, i + len(OPEN))
            if end is None:
                buf.append(template[i])
                i += 1
                continue
            if buf:
                parts.append(Text("".join(buf)))
                buf = []
            raw = template[i + len(OPEN) : end]
            # An expression may be written across several lines. Keep the
            # source on one line for printing; `canonical` has already
            # decided that the whitespace does not matter.
            parts.append(Expr(canonical(raw), " ".join(raw.split())))
            i = end + len(CLOSE)
        else:
            buf.append(template[i])
            i += 1
    if buf:
        parts.append(Text("".join(buf)))
    return parts


def split_lines(text: str) -> list[str]:
    """Split a multi-line input on the newlines the action would split on.

    `restore-keys` and `path` are newline-separated lists, and the action
    splits them after GitHub has substituted the expressions -- so a newline
    that falls *inside* a `${{ ... }}` is whitespace in an expression and not
    a separator at all. Written out in a `|` block, this is entirely normal:

        restore-keys: |
          ${{ runner.os }}-${{ runner.arch }}-mypy-${{
          env.CACHE_VERSION }}-

    That is one restore-key. Splitting on the newline first gives two, both
    of them nonsense, and a tool that does it reports a real workflow as
    broken -- which is how this function came to exist.
    """
    out: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        if text.startswith(OPEN, i):
            end = _find_close(text, i + len(OPEN))
            if end is not None:
                buf.append(text[i : end + len(CLOSE)])
                i = end + len(CLOSE)
                continue
        if text[i] == "\n":
            out.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(text[i])
        i += 1
    out.append("".join(buf))
    return [line.strip() for line in out if line.strip()]


def expressions(parts: list[Part]) -> list[Expr]:
    return [p for p in parts if isinstance(p, Expr)]


def rendered(parts: list[Part]) -> str:
    """The template put back together, for printing."""
    return "".join(
        p.text if isinstance(p, Text) else OPEN + " " + p.raw + " " + CLOSE
        for p in parts
    )


class Alphabet:
    """Hands out one private-use character per distinct expression."""

    def __init__(self) -> None:
        self._chars: dict[str, str] = {}

    def char(self, source: str) -> str:
        char = self._chars.get(source)
        if char is None:
            index = ATOM_FIRST + len(self._chars)
            if index > ATOM_LAST:  # pragma: no cover - 6400 distinct keys
                raise ValueError("too many distinct expressions to compare")
            char = chr(index)
            self._chars[source] = char
        return char

    def source(self, char: str) -> str | None:
        for source, assigned in self._chars.items():
            if assigned == char:
                return source
        return None


def _clean(text: str) -> str:
    """Literal text with any private-use characters taken out.

    They would be indistinguishable from an expression atom. Nobody puts one
    in a cache key, but "nobody does that" is how comparison bugs get in.
    """
    return "".join("�" if is_atom(ch) else ch for ch in text)


#: How many concrete forms one template may expand to before its multi-valued
#: expressions are treated as opaque instead. A 5x4 matrix in a key is real;
#: expanding a 12x8 one to compare against every other key is not.
FORM_LIMIT = 64


def forms(
    parts: list[Part],
    values: dict[str, frozenset[str]],
    alphabet: Alphabet,
    limit: int = FORM_LIMIT,
) -> list[str]:
    """Every concrete string this template could be, as atomised strings.

    `values` maps an expression's canonical source to the set of strings it is
    known to expand to. An expression that is not in there, or whose set is
    too large to be worth enumerating, becomes one atom character: it compares
    equal to itself and is ambiguous against anything else.
    """
    out: list[str] = [""]
    for part in parts:
        if isinstance(part, Text):
            piece = _clean(part.text)
            out = [form + piece for form in out]
            continue
        options = values.get(part.source)
        if not options or len(out) * len(options) > limit:
            atom = alphabet.char(part.source)
            out = [form + atom for form in out]
            continue
        out = [form + option for form in out for option in sorted(options)]
    return out


def _compare(prefix: str, whole: str) -> str:
    """Is `prefix` a prefix of `whole`, for two atomised strings.

    NO_MATCH is only returned when two literal characters genuinely differ, or
    when the prefix runs past the end of a key that ends in literal text. Any
    disagreement with an atom on either side of it is UNKNOWN, because an
    expression can expand to anything, including exactly the text it is being
    compared against.
    """
    limit = min(len(prefix), len(whole))
    for i in range(limit):
        if prefix[i] == whole[i]:
            continue
        if is_atom(prefix[i]) or is_atom(whole[i]):
            return UNKNOWN
        return NO_MATCH
    if len(prefix) <= len(whole):
        return MATCH
    # The prefix is longer. It can still match if the key's last part is an
    # expression, because that expression's value carries on past the end of
    # the template.
    if whole and is_atom(whole[-1]):
        return UNKNOWN
    return NO_MATCH


def _best(results: list[str]) -> str:
    if MATCH in results:
        return MATCH
    if UNKNOWN in results:
        return UNKNOWN
    return NO_MATCH


def prefix_match(prefix_forms: list[str], key_forms: list[str]) -> str:
    """Whether any form of a restore-key prefixes any form of a key."""
    return _best(
        [_compare(prefix, whole) for prefix in prefix_forms for whole in key_forms]
    )


def same(left_forms: list[str], right_forms: list[str]) -> str:
    """Whether two keys are certainly, possibly, or certainly not the same.

    Certainly means the two sets of forms overlap -- there is a run in which
    both templates produce the same string. Possibly means an expression is
    in the way. This is what decides a key collision, so it is deliberately
    the strict direction: two identical templates are certainly the same, and
    everything else needs a reason.
    """
    if set(left_forms) & set(right_forms):
        return MATCH
    return _best(
        [
            UNKNOWN if _pairable(left, right) else NO_MATCH
            for left in left_forms
            for right in right_forms
        ]
    )


def _pairable(left: str, right: str) -> bool:
    """Could these two atomised strings ever be the same string?"""
    if not any(is_atom(ch) for ch in left + right):
        return False
    for i in range(min(len(left), len(right))):
        if left[i] == right[i]:
            continue
        return is_atom(left[i]) or is_atom(right[i])
    longer = left if len(left) > len(right) else right
    shorter = right if len(left) > len(right) else left
    if len(left) == len(right):
        return True
    # One ran out. Only an expression at the end of the shorter one can cover
    # the difference.
    return bool(shorter) and is_atom(shorter[-1]) and bool(longer)
