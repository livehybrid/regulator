"""Which indexes, over which time range, a search reads.

The SmartStore forecast needs, for every search, the buckets it can touch.
Buckets are per index and per span of ``_time``, so the question is which
indexes a search names and what time bounds apply to each. The dispatch
window is only the default:

* An inline ``earliest=`` / ``latest=`` in the base search overrides the
  dispatch window for that search (``latest`` defaults to the window's when
  only ``earliest`` is given, and vice versa).
* Parenthesised groups joined by ``OR`` can each carry their own index and
  their own time bounds: ``(index=a earliest=-1h) OR (index=b earliest=-7d)``
  reads an hour of ``a`` and a week of ``b``.
* Every subsearch (``append [...]``, ``join [...]``, ``union [...]``,
  ``multisearch [...]``, ``appendcols``, a plain ``[search ...]`` filter) and
  every ``map search="..."`` is a search of its own, with its own indexes and
  its own inline bounds. A subsearch with none inherits the dispatch window.

This is a static reading of the search string, so it has edges, reported as
notes rather than guessed past: a macro, an eventtype, a tag or a data model
hides its index, a ``$token$`` hides a time bound, and an absolute date is
read as UTC where Splunk would use the searching user's time zone.

Only the clauses that select events are read: the implicit or explicit
``search`` before the first pipe, a later ``| search``, and ``tstats`` or
``mstats``. An ``eval earliest=...`` is a field assignment, not a time bound,
and is ignored.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

# index=foo, index="foo", index::foo, index=foo*  (not index!=foo, not _index)
_INDEX_EQ = re.compile(r'(?<![\w.!])index\s*(?:=|::)\s*"?([A-Za-z0-9_\-\*\.]+)"?', re.IGNORECASE)
_INDEX_IN = re.compile(r'(?<![\w.!])index\s+IN\s*\(([^)]*)\)', re.IGNORECASE)
# earliest=-7d@d, latest="01/02/2026:00:00:00"  (not _index_earliest, not earliest(x))
_TIME_MOD = re.compile(r'(?<![\w.])(earliest|latest)\s*=\s*("(?:[^"\\]|\\.)*"|[^\s()\]]+)', re.IGNORECASE)
_MAP_SEARCH = re.compile(r'\bsearch\s*=\s*"((?:[^"\\]|\\.)*)"', re.IGNORECASE)
_INDEX_OR = re.compile(
    r'(?<![\w.!])index\s*(?:=|::)\s*"?[^\s"()]+"?\s+OR\b|\bOR\s+index\s*(?:=|::)',
    re.IGNORECASE,
)
_SCOPE_TERM = re.compile(r'(?<![\w.!])(index\s*(?:=|::|\s+IN\b)|earliest\s*=|latest\s*=)', re.IGNORECASE)

# Commands whose arguments select events, and so carry index and time terms.
_SELECTING = ("search", "tstats", "mstats")
# Generating commands that read no index buckets at all.
_NO_BUCKETS = (
    "inputlookup", "makeresults", "rest", "metadata", "dbinspect", "eventcount",
    "loadjob", "inputcsv", "savedsearch", "datamodel", "from", "gentimes", "mcatalog",
)


@dataclass(frozen=True)
class Clause:
    """One part of a search that reads buckets on its own terms.

    ``indexes`` are lower-cased names or wildcards; empty means the search
    named none and reads the user's default indexes. ``earliest`` and
    ``latest`` are the raw modifier strings, ``None`` meaning "the dispatch
    window's".
    """

    indexes: Tuple[str, ...] = ()
    earliest: Optional[str] = None
    latest: Optional[str] = None
    origin: str = "search"
    metrics: bool = False


@dataclass
class Scope:
    clauses: List[Clause] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


# ------------------------------------------------------------------ scanning


def _split_brackets(text: str) -> Tuple[str, List[str]]:
    """Lift every top-level ``[...]`` out of ``text``, respecting quotes."""
    outer: List[str] = []
    inner: List[str] = []
    depth = 0
    start = 0
    quote = False
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and quote:
            i += 2
            continue
        if ch == '"':
            quote = not quote
        elif not quote and ch == "[":
            if depth == 0:
                outer.append(text[start:i])
                start = i + 1
            depth += 1
        elif not quote and ch == "]" and depth:
            depth -= 1
            if depth == 0:
                inner.append(text[start:i])
                outer.append(" __subsearch__ ")
                start = i + 1
        i += 1
    if depth:
        # Unbalanced: treat the remainder as outer text rather than lose it.
        outer.append("[" + text[start:])
    else:
        outer.append(text[start:])
    return "".join(outer), inner


def _split_top(text: str, sep: str) -> List[str]:
    """Split on ``sep`` outside quotes and parentheses."""
    parts: List[str] = []
    depth = 0
    quote = False
    start = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and quote:
            i += 2
            continue
        if ch == '"':
            quote = not quote
        elif not quote and ch == "(":
            depth += 1
        elif not quote and ch == ")" and depth:
            depth -= 1
        elif not quote and depth == 0 and ch == sep:
            parts.append(text[start:i])
            start = i + 1
        i += 1
    parts.append(text[start:])
    return parts


def _groups(text: str) -> Tuple[str, List[str]]:
    """Lift every top-level ``(...)`` out of ``text``, respecting quotes."""
    outer: List[str] = []
    groups: List[str] = []
    depth = 0
    quote = False
    start = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and quote:
            i += 2
            continue
        if ch == '"':
            quote = not quote
        elif not quote and ch == "(":
            if depth == 0:
                outer.append(text[start:i])
                start = i + 1
            depth += 1
        elif not quote and ch == ")" and depth:
            depth -= 1
            if depth == 0:
                groups.append(text[start:i])
                outer.append(" __group__ ")
                start = i + 1
        i += 1
    outer.append(text[start:] if not depth else "(" + text[start:])
    return "".join(outer), groups


_VALUE_OF = re.compile(r'(?<![\w.!])(earliest|latest|index)\s*(=|::)\s*$', re.IGNORECASE)


def _strip_quoted(text: str) -> str:
    """Blank out quoted strings so their contents are not read as terms.

    A quoted value of ``index=``, ``earliest=`` or ``latest=`` is kept: it is
    the term, not free text that happens to contain one.
    """

    def blank(match: "re.Match[str]") -> str:
        if _VALUE_OF.search(text[: match.start()]):
            return match.group(0)
        return " " * len(match.group(0))

    return re.sub(r'"(?:[^"\\]|\\.)*"', blank, text)


def _expand_in(text: str) -> str:
    """``index IN (a, b)`` as ``index=a index=b``, so the list is not a group."""

    def expand(match: "re.Match[str]") -> str:
        names = [n.strip().strip('"').strip("'") for n in match.group(1).split(",")]
        return " " + " ".join(f"index={n}" for n in names if n) + " "

    return _INDEX_IN.sub(expand, text)


def _indexes(text: str) -> List[str]:
    found: List[str] = []
    for match in _INDEX_EQ.finditer(text):
        found.append(match.group(1).lower())
    for match in _INDEX_IN.finditer(text):
        for part in match.group(1).split(","):
            name = part.strip().strip('"').strip("'").lower()
            if name:
                found.append(name)
    return list(dict.fromkeys(found))


def _modifiers(text: str) -> Tuple[Optional[str], Optional[str]]:
    earliest: Optional[str] = None
    latest: Optional[str] = None
    for match in _TIME_MOD.finditer(text):
        value = match.group(2).strip('"')
        if match.group(1).lower() == "earliest":
            earliest = value
        else:
            latest = value
    return earliest, latest


# ------------------------------------------------------------------ reading


def _clauses_of(text: str, inherited: Clause, origin: str) -> List[Clause]:
    """Clauses for one selecting segment, splitting out scoped OR groups."""
    outside, groups = _groups(_expand_in(text))
    visible = _strip_quoted(outside)
    own_indexes = tuple(_indexes(visible)) or inherited.indexes
    earliest, latest = _modifiers(visible)
    own = Clause(
        indexes=own_indexes,
        earliest=earliest if earliest is not None else inherited.earliest,
        latest=latest if latest is not None else inherited.latest,
        origin=origin,
        metrics=inherited.metrics,
    )
    scoped = [g for g in groups if _SCOPE_TERM.search(_strip_quoted(g))]
    if not scoped:
        return [own]
    out: List[Clause] = []
    # `index=a OR (index=b earliest=-7d)`: the outside index is an alternative
    # of its own, not a qualifier the groups inherit, so it is a clause too.
    # Splunk binds OR tighter than the implicit AND, so in
    # `index=web (A) OR (B)` the OR joins the groups and index=web qualifies
    # both: only an OR touching the index term itself makes it an alternative.
    if _INDEX_OR.search(visible):
        out.append(own)
    for group in scoped:
        out.extend(_clauses_of(group, own, "or-group"))
    return out


def _segment_command(segment: str) -> str:
    match = re.match(r"\s*([A-Za-z_]+)", segment)
    return match.group(1).lower() if match else ""


def _read_level(text: str, origin: str, scope: Scope) -> None:
    outer, subsearches = _split_brackets(text)
    stripped = outer.strip()
    segments = _split_top(stripped, "|")
    reads_buckets = False
    clauses: List[Clause] = []
    leading_pipe = stripped.startswith("|")
    for n, segment in enumerate(segments):
        if n == 0 and leading_pipe:
            continue  # the empty text before a leading pipe
        command = _segment_command(segment)
        implicit = n == 0 and not leading_pipe
        if implicit or command in _SELECTING:
            body = segment if implicit else segment[len(segment) - len(segment.lstrip()) + len(command):]
            if command == "tstats" or command == "mstats":
                # Only the where clause carries index and time terms.
                where = re.search(r"\bwhere\b(.*)", body, re.IGNORECASE | re.DOTALL)
                body = where.group(1) if where else ""
            if not body.strip() or body.strip() == "__subsearch__":
                if implicit or command in ("tstats", "mstats"):
                    reads_buckets = True
                    clauses.append(Clause(origin=origin, metrics=command == "mstats"))
                continue
            reads_buckets = True
            clauses.extend(_clauses_of(body, Clause(origin=origin, metrics=command == "mstats"), origin))
        elif command == "map":
            match = _MAP_SEARCH.search(segment)
            if match:
                _read_level(match.group(1).replace('\\"', '"'), "map", scope)
        elif n == (1 if leading_pipe else 0) and command in _NO_BUCKETS:
            continue
    if reads_buckets:
        # A later | search narrows the base search rather than reading more,
        # so keep the clauses that name an index or a bound; if none do, keep
        # one unscoped clause for the base search.
        scoped = [c for c in clauses if c.indexes or c.earliest or c.latest]
        if scoped:
            # Index-less clauses after an indexed base search are filters on it.
            base_indexes = next((c.indexes for c in scoped if c.indexes), ())
            for clause in scoped:
                if not clause.indexes and base_indexes:
                    clause = Clause(base_indexes, clause.earliest, clause.latest, clause.origin, clause.metrics)
                scope.clauses.append(clause)
        else:
            scope.clauses.append(clauses[0])
    for sub in subsearches:
        _read_level(sub, "subsearch", scope)


def read_scope(spl: str) -> Scope:
    """Every clause of ``spl`` that reads buckets, and what could not be read."""
    scope = Scope()
    _read_level(spl or "", "search", scope)
    if "`" in (spl or ""):
        scope.notes.append("a macro hides what it expands to; its indexes and time bounds are not seen")
    if re.search(r"(?<![\w.])(eventtype|tag)\s*=", spl or "", re.IGNORECASE):
        scope.notes.append("an eventtype or tag hides its index")
    for clause in scope.clauses:
        for value in (clause.earliest, clause.latest):
            if value and "$" in value:
                scope.notes.append(f"time bound {value!r} is a token; the dispatch window is used instead")
    # Deduplicate while keeping order.
    seen = set()
    unique: List[Clause] = []
    for clause in scope.clauses:
        key = (clause.indexes, clause.earliest, clause.latest, clause.metrics)
        if key not in seen:
            seen.add(key)
            unique.append(clause)
    scope.clauses = unique
    scope.notes = list(dict.fromkeys(scope.notes))
    return scope


# ------------------------------------------------------------------ time


_ABSOLUTE_FORMATS = ("%m/%d/%Y:%H:%M:%S", "%m/%d/%Y")


def evaluate(modifier: str, now: float) -> Optional[float]:
    """An inline time modifier as an epoch, or ``None`` if it cannot be read.

    Relative modifiers are evaluated against ``now``, the dispatch time.
    Absolute dates are read as UTC.
    """
    from .savedsearches import RelativeTimeError, evaluate_relative

    text = (modifier or "").strip()
    if not text or "$" in text:
        return None
    for fmt in _ABSOLUTE_FORMATS:
        try:
            return _dt.datetime.strptime(text, fmt).replace(tzinfo=_dt.timezone.utc).timestamp()
        except ValueError:
            pass
    try:
        reference = _dt.datetime.fromtimestamp(now, tz=_dt.timezone.utc)
        return evaluate_relative(text, reference).timestamp()
    except (RelativeTimeError, ValueError, OverflowError):
        return None


def clause_span(
    clause: Clause, window_span: Tuple[float, float], start: float, end: float
) -> Tuple[Tuple[float, float], List[str]]:
    """The time range a clause reads over a run from ``start`` to ``end``.

    ``window_span`` is the union of the dispatch windows over the run, which
    is what a clause with no inline bound reads. A relative bound moves with
    the dispatch time, so its earliest edge is taken at the run's start and
    its latest at the end.
    """
    notes: List[str] = []
    lo, hi = window_span
    if clause.earliest is not None:
        value = evaluate(clause.earliest, start)
        if value is None:
            notes.append(f"could not read earliest={clause.earliest!r}; the dispatch window is used")
        else:
            lo = value
    if clause.latest is not None:
        value = evaluate(clause.latest, end)
        if value is None:
            notes.append(f"could not read latest={clause.latest!r}; the dispatch window is used")
        else:
            hi = value
    if hi < lo:
        lo, hi = hi, lo
    return (lo, hi), notes


def all_indexes(clauses: Sequence[Clause]) -> List[str]:
    return list(dict.fromkeys(i for c in clauses for i in c.indexes))
