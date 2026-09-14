"""Deciding whether a `hashFiles()` pattern matches anything in the repository.

This matters more than it sounds like it should. `hashFiles` returns the empty
string when nothing matches -- not an error, not a warning, an empty string
spliced into the middle of your cache key. The key becomes a constant, the
first run of the workflow saves a cache under it, and because a cache entry
can never be overwritten, every run after that restores that first one. For
ever. The workflow goes green the whole time and the cache hit rate is a
hundred percent, which is exactly what it looks like when everything is fine.

The pattern syntax is `@actions/glob`, which is minimatch with these options:

    {dot: true, nobrace: true, nocase: IS_WINDOWS, noext: true, nonegate: true}

Three of those are worth knowing before you write a pattern.

`dot: true` -- `*` matches a leading dot, unlike minimatch's default and
unlike your shell.

`nobrace: true` -- **brace expansion is off**. `**/*.{js,ts}` does not mean
what it means in every other glob you have ever written; the `{js,ts}` is six
literal characters, and the pattern matches a file called `app.{js,ts}`.

`noext: true` -- no extglob either, so `+(a|b)` is literal too.

Note also that this is not the same syntax as a workflow's `paths:` filter,
which is GitHub's own thing where `?` and `+` are regex quantifiers applying
to the preceding character. Same file, two glob dialects, ten lines apart.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

#: Directories never worth walking to answer "does this glob match a file".
SKIP_DIRS = {".git"}

OUTSIDE = "outside the workspace"


@dataclass
class Tree:
    """The paths in a checkout, as `hashFiles` would see them."""

    files: frozenset[str]
    dirs: frozenset[str]

    @classmethod
    def scan(cls, root: str) -> Tree:
        files: set[str] = set()
        dirs: set[str] = set()
        for current, subdirs, names in os.walk(root):
            subdirs[:] = [d for d in subdirs if d not in SKIP_DIRS]
            relative = os.path.relpath(current, root)
            base = "" if relative == "." else relative.replace(os.sep, "/")
            if base:
                dirs.add(base)
            for name in names:
                files.add(f"{base}/{name}" if base else name)
        return cls(frozenset(files), frozenset(dirs))

    @classmethod
    def of(cls, paths: list[str]) -> Tree:
        """A tree from a list of file paths, with their parents implied."""
        files = set()
        dirs = set()
        for path in paths:
            path = path.strip("/")
            files.add(path)
            parts = path.split("/")[:-1]
            for i in range(1, len(parts) + 1):
                dirs.add("/".join(parts[:i]))
        return cls(frozenset(files), frozenset(dirs))


@dataclass
class Verdict:
    """Whether a set of patterns matched, or why that could not be decided."""

    #: True, False, or None when it could not be decided.
    matched: bool | None
    #: Set when matched is False or None: which pattern is responsible.
    pattern: str | None = None
    #: Set when matched is None: what about it could not be handled.
    reason: str | None = None


def _translate_segment(segment: str) -> str:
    out: list[str] = []
    i = 0
    n = len(segment)
    while i < n:
        char = segment[i]
        if char == "\\" and i + 1 < n:
            out.append(re.escape(segment[i + 1]))
            i += 2
        elif char == "*":
            while i < n and segment[i] == "*":
                i += 1
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
            i += 1
        elif char == "[":
            end = _class_end(segment, i)
            if end is None:
                out.append(re.escape("["))
                i += 1
            else:
                out.append(_translate_class(segment[i + 1 : end]))
                i = end + 1
        else:
            out.append(re.escape(char))
            i += 1
    return "".join(out)


def _class_end(segment: str, start: int) -> int | None:
    i = start + 1
    if i < len(segment) and segment[i] in "!^":
        i += 1
    if i < len(segment) and segment[i] == "]":
        i += 1
    while i < len(segment):
        if segment[i] == "\\":
            i += 2
            continue
        if segment[i] == "]":
            return i
        i += 1
    return None


def _translate_class(body: str) -> str:
    negated = body[:1] in ("!", "^")
    if negated:
        body = body[1:]
    inner = body.replace("\\", "\\\\").replace("]", "\\]")
    return "[" + ("^" if negated else "") + inner + "]"


def _translate(pattern: str) -> re.Pattern[str]:
    segments = pattern.split("/")
    out: list[str] = []
    for index, segment in enumerate(segments):
        last = index == len(segments) - 1
        if segment == "**":
            out.append(".*" if last else "(?:[^/]+/)*")
        else:
            out.append(_translate_segment(segment))
            if not last:
                out.append("/")
    return re.compile("^" + "".join(out) + "$")


@dataclass
class _Pattern:
    raw: str
    negate: bool
    directory_only: bool
    regex: re.Pattern[str] | None
    problem: str | None


def _prepare(raw: str) -> _Pattern:
    pattern = raw
    negate = False
    while pattern.startswith("!"):
        negate = not negate
        pattern = pattern[1:].strip()

    if pattern.startswith("~"):
        return _Pattern(raw, negate, False, None, "starts at the home directory")
    if pattern.startswith("/") or (len(pattern) > 1 and pattern[1] == ":"):
        return _Pattern(raw, negate, False, None, OUTSIDE)

    pattern = pattern.replace("\\", "/") if os.sep == "\\" else pattern
    while pattern.startswith("./"):
        pattern = pattern[2:]
    if pattern == "." or pattern == "":
        return _Pattern(raw, negate, True, _translate("**"), None)
    if any(part == ".." for part in pattern.split("/")):
        return _Pattern(raw, negate, False, None, OUTSIDE)

    directory_only = pattern.endswith("/")
    pattern = pattern.rstrip("/")
    return _Pattern(raw, negate, directory_only, _translate(pattern), None)


def _hits(prepared: _Pattern, tree: Tree) -> set[str]:
    """Every path in the tree this one pattern matches.

    A pattern that names a directory matches every file under it, which is how
    `hashFiles('src')` manages to hash anything at all.
    """
    assert prepared.regex is not None
    if prepared.directory_only:
        return {d for d in tree.dirs if prepared.regex.match(d)}
    found = {f for f in tree.files if prepared.regex.match(f)}
    for directory in tree.dirs:
        if prepared.regex.match(directory):
            found |= {f for f in tree.files if f.startswith(directory + "/")}
    return found


def evaluate(patterns: list[str], tree: Tree) -> Verdict:
    """Whether these `hashFiles` patterns, taken together, match any file.

    Patterns are applied in order and the last one to touch a path wins, so a
    trailing `!**/vendor/**` can take back everything an earlier line found.
    """
    if not patterns:
        return Verdict(False, None, None)

    selected: set[str] = set()
    last_positive: str | None = None
    outside: str | None = None
    for raw in patterns:
        prepared = _prepare(raw)
        if prepared.problem is not None:
            if prepared.problem == OUTSIDE and not prepared.negate:
                # A definite answer, not a gap: hashFiles only ever sees files
                # under GITHUB_WORKSPACE, so this one matches nothing, always.
                outside = outside or raw
                continue
            return Verdict(None, raw, prepared.problem)
        hits = _hits(prepared, tree)
        if prepared.negate:
            selected -= hits
        else:
            selected |= hits
            last_positive = raw

    if selected:
        return Verdict(True, None, None)
    if outside is not None and last_positive is None:
        return Verdict(False, outside, OUTSIDE)
    return Verdict(False, last_positive or patterns[0], None)
