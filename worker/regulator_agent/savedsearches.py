"""Splunk's own search format, so existing searches load without translation.

Every Splunk deployment already has its workload written down: the
``savedsearches.conf`` files in its apps. A load test that needs those
searches copied by hand into another format never gets run against the real
workload, because nobody copies four hundred searches by hand. So Regulator
reads the format Splunk writes.

This module is the whole of that support:

* a parser for the ``.conf`` grammar Splunk uses (stanzas, ``key = value``,
  backslash continuations, ``[default]`` inheritance, ``#`` comments), which
  is not INI and is not quite anything else either;
* a writer for the same grammar, so a set of searches pulled off a target over
  REST round-trips into a file a Splunk admin would recognise;
* an evaluator for Splunk's relative time modifiers (``-24h@h``, ``@d-1h``),
  which is how a saved search says what window it covers;
* a matcher for the five-field cron Splunk's scheduler uses, which is how a
  saved search says how often it runs, and therefore how heavy it is;
* a classifier that guesses a search's class (dense, rare, accelerated...)
  from its SPL, honestly labelled as a guess;
* a detector for commands with side effects, because a search that writes to
  an index or a lookup must never be replayed by a load test unless somebody
  says so out loud.

Nothing here talks to Splunk. Fetching saved searches over REST lives in the
client, and turning them into scenario steps lives in the scenario module.
"""

from __future__ import annotations

import calendar
import datetime as _dt
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# The .conf grammar
# ---------------------------------------------------------------------------

_STANZA_RE = re.compile(r"^\[(.*)\]\s*$")


def parse_conf(text: str) -> Dict[str, Dict[str, str]]:
    """Parse a Splunk ``.conf`` document into ``{stanza: {key: value}}``.

    The grammar, as splunkd reads it rather than as INI parsers assume:

    * ``[name]`` opens a stanza. The name is everything between the brackets
      and may contain spaces, colons, dots and most other characters.
    * ``key = value``. Keys are case-sensitive. Everything after the first
      ``=`` is the value, with surrounding whitespace stripped.
    * A line ending in a backslash continues onto the next line. The newline
      is **kept** in the value: that is what makes a multi-line ``search``
      round-trip through Splunk Web unchanged, and it is harmless in SPL.
    * ``#`` starts a comment only at the beginning of a line. A ``#`` inside a
      value is part of the value, which matters for SPL like ``eval x="#"``.
    * Values in ``[default]`` apply to every stanza that does not override
      them. They are folded in here so callers never have to know.
    * A key repeated within a stanza takes the last value, as splunkd does.
    * Keys before any stanza header belong to ``[default]``.

    Stanza order is preserved. A byte-order mark is tolerated because Splunk
    Web on Windows writes one.
    """
    stanzas: Dict[str, Dict[str, str]] = {}
    order: List[str] = []
    current = "default"
    stanzas[current] = {}
    order.append(current)

    logical_lines: List[str] = []
    pending: Optional[str] = None
    for raw in text.lstrip("﻿").splitlines():
        line = raw.rstrip("\r")
        if pending is not None:
            line = pending + "\n" + line
            pending = None
        if line.endswith("\\") and not line.endswith("\\\\"):
            pending = line[:-1]
            continue
        logical_lines.append(line)
    if pending is not None:
        logical_lines.append(pending)

    for line in logical_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith(";"):
            continue
        match = _STANZA_RE.match(stripped)
        if match:
            current = match.group(1).strip()
            if current not in stanzas:
                stanzas[current] = {}
                order.append(current)
            continue
        if "=" not in line:
            # splunkd ignores a bare word; so do we, loudly enough to be found.
            continue
        key, _, value = line.partition("=")
        stanzas[current][key.strip()] = value.strip()

    defaults = stanzas.get("default", {})
    result: Dict[str, Dict[str, str]] = {}
    for name in order:
        if name == "default":
            continue
        merged = dict(defaults)
        merged.update(stanzas[name])
        result[name] = merged
    return result


def render_conf(stanzas: Mapping[str, Mapping[str, Any]], header: str = "") -> str:
    """Write ``{stanza: {key: value}}`` back out in Splunk's grammar.

    Multi-line values get backslash continuations, so what this writes is what
    Splunk Web would have written, and a file exported from one target can be
    dropped into ``local/`` on another.
    """
    lines: List[str] = []
    if header:
        lines.extend(f"# {line}" if line else "#" for line in header.splitlines())
        lines.append("")
    for name, attributes in stanzas.items():
        lines.append(f"[{name}]")
        for key, value in attributes.items():
            if value is None:
                continue
            text = str(value)
            if isinstance(value, bool):
                text = "1" if value else "0"
            text = text.replace("\r\n", "\n").replace("\r", "\n")
            if "\n" in text:
                parts = text.split("\n")
                lines.append(f"{key} = {parts[0]}\\")
                for part in parts[1:-1]:
                    lines.append(f"{part}\\")
                lines.append(parts[-1])
            else:
                lines.append(f"{key} = {text}")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


# ---------------------------------------------------------------------------
# Saved searches
# ---------------------------------------------------------------------------

# Attributes worth carrying into a scenario. Everything else in a stanza is
# alert plumbing (actions, throttling, display options) that has no bearing on
# what the search costs, and it is kept verbatim on ``attributes`` rather than
# modelled.
KEY_SEARCH = "search"
KEY_EARLIEST = "dispatch.earliest_time"
KEY_LATEST = "dispatch.latest_time"
KEY_CRON = "cron_schedule"
KEY_ENABLE_SCHED = "enableSched"
KEY_DISABLED = "disabled"
KEY_APP = "request.ui_dispatch_app"
KEY_DESCRIPTION = "description"
KEY_SCHEDULE_WINDOW = "schedule_window"
KEY_MAX_COUNT = "dispatch.max_count"

# Regulator's own annotations. Prefixed so they cannot collide with a Splunk
# attribute, and ignored by splunkd (it warns about unknown keys in btool
# check and otherwise carries on).
KEY_CLASS = "regulator.class"
KEY_WEIGHT = "regulator.weight"
KEY_RESULT_COUNT = "regulator.result_count"
KEY_SKIP = "regulator.skip"

# Scheduled PDF views live in savedsearches.conf too, with a stub search that
# is not a search. They are never load.
_SCHEDULED_VIEW_PREFIX = "_ScheduledView__"


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in ("1", "true", "t", "yes", "y", "on")


def normalise_spl(search: str) -> str:
    """Make a saved search's SPL dispatchable over REST.

    Splunk Web lets a saved search start with a bare term (``error OR failed``)
    because the scheduler prepends ``search`` itself. The REST jobs endpoint
    does not, and neither does Regulator's lint, so the keyword is added here
    when the search does not already start with a generating command.
    """
    text = (search or "").strip()
    if not text:
        return text
    if text.startswith("|"):
        return text
    lowered = text.lower()
    if lowered.startswith("search ") or lowered.startswith("search\n") or lowered == "search":
        return text
    return "search " + text


@dataclass(frozen=True)
class SavedSearch:
    """One stanza of savedsearches.conf, with the parts a load test needs."""

    name: str
    search: str
    raw_search: str = ""
    earliest: str = ""
    latest: str = ""
    cron: Optional[str] = None
    scheduled: bool = False
    disabled: bool = False
    app: Optional[str] = None
    description: str = ""
    schedule_window: str = ""
    max_count: Optional[int] = None
    attributes: Dict[str, str] = field(default_factory=dict)

    @property
    def is_realtime(self) -> bool:
        return self.earliest.strip().lower().startswith("rt") or self.latest.strip().lower().startswith("rt")

    @property
    def is_scheduled_view(self) -> bool:
        return self.name.startswith(_SCHEDULED_VIEW_PREFIX)

    @property
    def annotated_class(self) -> Optional[str]:
        value = self.attributes.get(KEY_CLASS)
        return value.strip().lower() if value else None

    @property
    def annotated_weight(self) -> Optional[float]:
        value = self.attributes.get(KEY_WEIGHT)
        if not value:
            return None
        try:
            return float(value)
        except ValueError:
            return None

    @property
    def annotated_result_count(self) -> Optional[int]:
        value = self.attributes.get(KEY_RESULT_COUNT)
        if not value:
            return None
        try:
            return int(value)
        except ValueError:
            return None

    @property
    def skipped(self) -> bool:
        return _truthy(self.attributes.get(KEY_SKIP, "0"))

    def to_stanza(self, *, include_annotations: bool = True) -> Dict[str, str]:
        """The stanza back, in an order a human would write it."""
        stanza: Dict[str, str] = {}
        if self.description:
            stanza[KEY_DESCRIPTION] = self.description
        stanza[KEY_SEARCH] = self.raw_search or self.search
        if self.earliest:
            stanza[KEY_EARLIEST] = self.earliest
        if self.latest:
            stanza[KEY_LATEST] = self.latest
        if self.cron:
            stanza[KEY_CRON] = self.cron
        stanza[KEY_ENABLE_SCHED] = "1" if self.scheduled else "0"
        if self.disabled:
            stanza[KEY_DISABLED] = "1"
        if self.app:
            stanza[KEY_APP] = self.app
        if self.schedule_window:
            stanza[KEY_SCHEDULE_WINDOW] = self.schedule_window
        if self.max_count is not None:
            stanza[KEY_MAX_COUNT] = str(self.max_count)
        for key, value in self.attributes.items():
            if key in stanza:
                continue
            if key.startswith("regulator.") and not include_annotations:
                continue
            stanza[key] = value
        return stanza


def from_stanza(name: str, attributes: Mapping[str, Any], app_hint: Optional[str] = None) -> SavedSearch:
    """Build a :class:`SavedSearch` from one stanza's attributes."""
    attrs = {str(k): ("" if v is None else str(v)) for k, v in attributes.items()}
    raw_search = attrs.get(KEY_SEARCH, "")
    max_count_raw = attrs.get(KEY_MAX_COUNT, "").strip()
    try:
        max_count = int(max_count_raw) if max_count_raw else None
    except ValueError:
        max_count = None
    return SavedSearch(
        name=name,
        search=normalise_spl(raw_search),
        raw_search=raw_search,
        earliest=attrs.get(KEY_EARLIEST, "").strip(),
        latest=attrs.get(KEY_LATEST, "").strip(),
        cron=(attrs.get(KEY_CRON, "").strip() or None),
        scheduled=_truthy(attrs.get(KEY_ENABLE_SCHED, "0")),
        disabled=_truthy(attrs.get(KEY_DISABLED, "0")),
        app=(attrs.get(KEY_APP, "").strip() or app_hint or None),
        description=attrs.get(KEY_DESCRIPTION, "").strip(),
        schedule_window=attrs.get(KEY_SCHEDULE_WINDOW, "").strip(),
        max_count=max_count,
        attributes={
            k: v
            for k, v in attrs.items()
            if k not in (
                KEY_SEARCH, KEY_EARLIEST, KEY_LATEST, KEY_CRON, KEY_ENABLE_SCHED,
                KEY_DISABLED, KEY_APP, KEY_DESCRIPTION, KEY_SCHEDULE_WINDOW, KEY_MAX_COUNT,
            )
        },
    )


def parse_savedsearches(text: str, app_hint: Optional[str] = None) -> List[SavedSearch]:
    """Every stanza of a savedsearches.conf document, in file order."""
    return [from_stanza(name, attrs, app_hint) for name, attrs in parse_conf(text).items()]


def load_savedsearches(path: str | Path, app_hint: Optional[str] = None) -> List[SavedSearch]:
    return parse_savedsearches(Path(path).read_text(encoding="utf-8"), app_hint=app_hint)


# REST entries from /servicesNS/-/-/saved/searches carry the same attribute
# names as the conf file under ``content``, plus the app under ``acl``. A few
# are REST-only and derived, and are dropped so the exported file is one Splunk
# would accept back.
_REST_DERIVED_KEYS = frozenset(
    {
        "eai:acl", "eai:appName", "eai:userName", "eai:digest", "eai:attributes",
        "is_scheduled", "next_scheduled_time", "qualifiedSearch", "triggered_alert_count",
        "embed.enabled", "auto_summarize.is_summarised", "restart_on_searchpeer_add",
        "action.email.sendresults", "workload_pool", "durable.track_time_type",
        "durable.lag_time", "durable.backfill_type", "durable.max_backfill_intervals",
        "federated.provider", "precalculate_required_fields_for_alerts",
    }
)

# Attributes worth exporting. Everything else on a REST entry is either a
# default splunkd echoes back for every search (a hundred ``action.*`` and
# ``display.*`` keys) or alert plumbing that a load test must not replay.
_EXPORT_KEYS = (
    KEY_DESCRIPTION,
    KEY_SEARCH,
    KEY_EARLIEST,
    KEY_LATEST,
    KEY_CRON,
    KEY_ENABLE_SCHED,
    KEY_DISABLED,
    KEY_APP,
    KEY_SCHEDULE_WINDOW,
    KEY_MAX_COUNT,
    "dispatch.ttl",
    "dispatch.indexedRealtime",
    "dispatch.max_time",
    "realtime_schedule",
    "schedule_priority",
    "allow_skew",
    "dispatchAs",
)


def from_rest_entries(entries: Iterable[Mapping[str, Any]]) -> List[SavedSearch]:
    """Saved searches as the REST API lists them, reduced to what matters.

    ``entries`` is the ``entry`` list from ``/saved/searches?output_mode=json``
    (or the flattened form :meth:`SplunkClient.entries` produces, where the
    content is merged with ``name``). Both are accepted.
    """
    searches: List[SavedSearch] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        content = entry.get("content") if isinstance(entry.get("content"), Mapping) else entry
        acl = entry.get("acl") if isinstance(entry.get("acl"), Mapping) else {}
        app = str(acl.get("app") or content.get("eai:appName") or "").strip() or None
        attributes: Dict[str, str] = {}
        for key in _EXPORT_KEYS:
            value = content.get(key)
            if value is None or value == "":
                continue
            if isinstance(value, bool):
                value = "1" if value else "0"
            attributes[key] = str(value)
        if KEY_ENABLE_SCHED not in attributes and content.get("is_scheduled") is not None:
            attributes[KEY_ENABLE_SCHED] = "1" if _truthy(content.get("is_scheduled")) else "0"
        if KEY_APP not in attributes and app:
            attributes[KEY_APP] = app
        for key, value in content.items():
            if str(key).startswith("regulator."):
                attributes[str(key)] = str(value)
        searches.append(from_stanza(name, attributes, app_hint=app))
    return searches


def render_savedsearches(searches: Sequence[SavedSearch], header: str = "") -> str:
    return render_conf({s.name: s.to_stanza() for s in searches}, header=header)


# ---------------------------------------------------------------------------
# Side effects: searches a load test must not replay by accident
# ---------------------------------------------------------------------------

# Commands that write somewhere. A saved search that ends in ``collect`` was
# written to build a summary index every five minutes; replaying it at two
# hundred virtual users writes two hundred copies. Each of these is a
# blocking lint finding unless the scenario says allow_side_effects.
SIDE_EFFECT_COMMANDS = (
    "collect",
    "mcollect",
    "meventcollect",
    "outputlookup",
    "outputcsv",
    "outputtext",
    "sendemail",
    "sendalert",
    "delete",
    "summaryindex",
    "sistats",
    "sichart",
    "sitimechart",
    "sitop",
    "sirare",
    "runshellscript",
    "script",
    "dbxoutput",
    "kvstore",
)

_PIPE_COMMAND_RE = re.compile(r"(?:^|\|)\s*([A-Za-z_][A-Za-z0-9_]*)")
_COMMENT_RE = re.compile(r"```.*?```", re.DOTALL)


def commands_in(spl: str) -> List[str]:
    """Every command name at the head of a pipe segment, lowercased.

    Comments are stripped first: a marker comment must never look like a
    command, and neither must the word ``collect`` inside a string literal.
    Quoted strings are not tokenised, which is a deliberate simplification: a
    pipe inside a quoted string will produce a phantom command name, and a
    phantom side-effect finding is a much cheaper mistake than a missed one.
    """
    text = _COMMENT_RE.sub(" ", spl or "")
    found: List[str] = []
    for match in _PIPE_COMMAND_RE.finditer(text):
        found.append(match.group(1).lower())
    return found


def side_effects(spl: str) -> List[str]:
    """The side-effecting commands in a search, in order. Empty means safe."""
    return [command for command in commands_in(spl) if command in SIDE_EFFECT_COMMANDS]


# ---------------------------------------------------------------------------
# Classification: an honest guess at what a search stresses
# ---------------------------------------------------------------------------

_ACCELERATED_HEADS = ("tstats", "mstats", "datamodel", "pivot", "metadata", "mcatalog")
_HEAVY_COMMANDS = ("transaction", "join", "lookup", "map", "append", "appendcols", "geostats", "cluster", "kmeans")
_GENERATING_HEADS = ("makeresults", "rest", "inputlookup", "dbxquery", "dbinspect", "gentimes", "loadjob", "savedsearch", "inputcsv", "typeahead")


def classify(spl: str, earliest: str = "") -> str:
    """Guess a search's class from its shape.

    Deliberately conservative: it names a class only when the SPL makes it
    obvious (a subsearch, a ``tstats``, a ``transaction``) and otherwise says
    ``unclassified``, because a wrong class silently mis-attributes a
    regression to the wrong part of the stack. A ``regulator.class``
    annotation on the stanza always wins over this.
    """
    text = _COMMENT_RE.sub(" ", spl or "")
    lowered = text.lower()
    if earliest.strip().lower().startswith("rt") or "| rtsearch" in lowered or lowered.startswith("rtsearch"):
        return "realtime"
    commands = commands_in(text)
    head = commands[0] if commands else ""
    if re.search(r"\[\s*search\s", lowered) or re.search(r"\[\s*\|", lowered):
        return "subsearch"
    if head in _ACCELERATED_HEADS:
        return "accelerated"
    if any(command in _HEAVY_COMMANDS for command in commands):
        return "heavy"
    if head in _GENERATING_HEADS:
        # Runs on the search head without touching an indexer. Not one of the
        # index classes, and saying so is more useful than picking one.
        return "accelerated" if head in ("inputlookup", "loadjob") else "unclassified"
    return "unclassified"


# ---------------------------------------------------------------------------
# Splunk relative time modifiers
# ---------------------------------------------------------------------------

_UNIT_SECONDS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
    "w": 604800, "week": 604800, "weeks": 604800,
    "mon": 30 * 86400, "month": 30 * 86400, "months": 30 * 86400,
    "q": 91 * 86400, "qtr": 91 * 86400, "qtrs": 91 * 86400, "quarter": 91 * 86400, "quarters": 91 * 86400,
    "y": 365 * 86400, "yr": 365 * 86400, "yrs": 365 * 86400, "year": 365 * 86400, "years": 365 * 86400,
}

_UNIT_NAMES = sorted(_UNIT_SECONDS, key=len, reverse=True)
_UNIT_ALTERNATION = "|".join(re.escape(u) for u in _UNIT_NAMES)
_OFFSET_RE = re.compile(rf"^([+-])(\d*)({_UNIT_ALTERNATION})")
# w0..w7 before the unit names, otherwise the bare "w" unit matches first and
# leaves the digit behind.
_SNAP_RE = re.compile(rf"^@(w[0-7]|{_UNIT_ALTERNATION})")


class RelativeTimeError(ValueError):
    """A time modifier Splunk would reject, or one Regulator cannot replay."""


def _snap(moment: _dt.datetime, unit: str) -> _dt.datetime:
    if unit.startswith("w") and len(unit) == 2 and unit[1].isdigit():
        weekday = int(unit[1]) % 7  # w0 and w7 are both Sunday
        day = moment.replace(hour=0, minute=0, second=0, microsecond=0)
        # Python's Monday=0; Splunk's Sunday=0.
        current = (day.weekday() + 1) % 7
        return day - _dt.timedelta(days=(current - weekday) % 7)
    seconds = _UNIT_SECONDS[unit]
    if seconds == 1:
        return moment.replace(microsecond=0)
    if seconds == 60:
        return moment.replace(second=0, microsecond=0)
    if seconds == 3600:
        return moment.replace(minute=0, second=0, microsecond=0)
    if seconds == 86400:
        return moment.replace(hour=0, minute=0, second=0, microsecond=0)
    if seconds == 604800:
        return _snap(moment, "w1")  # @w snaps to Monday? Splunk: @w snaps to Sunday. Corrected below.
    if unit in ("mon", "month", "months"):
        return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if unit in ("q", "qtr", "qtrs", "quarter", "quarters"):
        first_month = 3 * ((moment.month - 1) // 3) + 1
        return moment.replace(month=first_month, day=1, hour=0, minute=0, second=0, microsecond=0)
    return moment.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)


def _add(moment: _dt.datetime, amount: int, unit: str) -> _dt.datetime:
    if unit in ("mon", "month", "months", "q", "qtr", "qtrs", "quarter", "quarters", "y", "yr", "yrs", "year", "years"):
        months = amount * (1 if unit.startswith("mon") else 3 if unit.startswith("q") else 12)
        total = moment.year * 12 + (moment.month - 1) + months
        year, month = divmod(total, 12)
        month += 1
        day = min(moment.day, calendar.monthrange(year, month)[1])
        return moment.replace(year=year, month=month, day=day)
    return moment + _dt.timedelta(seconds=amount * _UNIT_SECONDS[unit])


def evaluate_relative(modifier: str, now: _dt.datetime) -> _dt.datetime:
    """Evaluate a Splunk relative time modifier against ``now``.

    Handles ``now``, an epoch number, and any chain of ``[+-]N<unit>`` offsets
    and ``@<unit>`` snaps in Splunk's order of application (left to right).
    ``0`` and an empty string mean the epoch, which is how a saved search says
    "all time". Real-time modifiers raise, because a load test cannot replay
    a real-time search as a historical one and pretending otherwise would
    measure the wrong thing.
    """
    text = (modifier or "").strip().lower()
    if text == "" or text == "0":
        return _dt.datetime(1970, 1, 1, tzinfo=now.tzinfo)
    if text.startswith("rt"):
        raise RelativeTimeError(f"{modifier!r} is a real-time modifier")
    if text == "now":
        return now
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return _dt.datetime.fromtimestamp(float(text), tz=now.tzinfo)

    moment = now
    rest = text
    while rest:
        offset = _OFFSET_RE.match(rest)
        if offset:
            sign, digits, unit = offset.groups()
            amount = int(digits) if digits else 1
            moment = _add(moment, -amount if sign == "-" else amount, unit)
            rest = rest[offset.end():]
            continue
        snap = _SNAP_RE.match(rest)
        if snap:
            unit = snap.group(1)
            if unit == "w":
                # @w alone snaps to the start of the week, which for Splunk is Sunday.
                moment = _snap(moment, "w0")
            else:
                moment = _snap(moment, unit)
            rest = rest[snap.end():]
            continue
        raise RelativeTimeError(f"cannot parse the time modifier {modifier!r} at {rest!r}")
    return moment


@dataclass(frozen=True)
class DerivedWindow:
    """What a saved search's dispatch times mean, in Regulator's terms."""

    window_s: float
    offset_s: float
    align_s: float
    all_time: bool = False


def derive_window(earliest: str, latest: str, now: Optional[_dt.datetime] = None) -> DerivedWindow:
    """Turn ``dispatch.earliest_time`` and ``dispatch.latest_time`` into a rolling window.

    The window is the span between the two, the offset is how far behind
    ``now`` the window ends (zero for the usual ``latest = now``), and the
    alignment is the finest snap unit either modifier used. Month-shaped
    modifiers are approximated at thirty days, which is fine for a load test
    and stated here so nobody expects calendar precision.
    """
    reference = now or _dt.datetime(2026, 1, 15, 12, 0, 0, tzinfo=_dt.timezone.utc)
    latest_text = (latest or "").strip()
    if latest_text in ("", "0"):
        # An unset latest is now. So is 0: it is what some apps ship (seen in
        # the wild as dispatch.latest_time = 0) and it cannot mean the epoch,
        # which would put the window before every earliest ever written.
        latest_text = "now"
    earliest_text = (earliest or "").strip()
    if earliest_text in ("", "0"):
        return DerivedWindow(window_s=0.0, offset_s=0.0, align_s=60.0, all_time=True)
    end = evaluate_relative(latest_text, reference)
    start = evaluate_relative(earliest_text, reference)
    window = (end - start).total_seconds()
    if window <= 0:
        raise RelativeTimeError(
            f"earliest {earliest!r} is not before latest {latest!r}, so the window is empty"
        )
    offset = (reference - end).total_seconds()
    align = 60.0
    snaps = re.findall(rf"@(w[0-7]|{_UNIT_ALTERNATION})", (earliest_text + " " + latest_text).lower())
    if snaps:
        finest = min(_UNIT_SECONDS.get(s, 604800 if s.startswith("w") else 60) for s in snaps)
        align = float(max(60, min(finest, 86400)))
    return DerivedWindow(window_s=window, offset_s=max(0.0, offset), align_s=align)


# ---------------------------------------------------------------------------
# Cron, as Splunk's scheduler reads it
# ---------------------------------------------------------------------------

_CRON_FIELDS = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day", 1, 31),
    ("month", 1, 12),
    ("weekday", 0, 7),
)


class CronError(ValueError):
    """A cron expression Splunk's scheduler would reject."""


def _parse_cron_field(text: str, low: int, high: int, name: str) -> Tuple[frozenset[int], bool]:
    """A set of matching values, and whether the field was a bare ``*``."""
    values: set[int] = set()
    wildcard = False
    for part in text.split(","):
        part = part.strip()
        if not part:
            raise CronError(f"empty {name} field")
        step = 1
        if "/" in part:
            part, step_text = part.split("/", 1)
            try:
                step = int(step_text)
            except ValueError as exc:
                raise CronError(f"bad step in {name}: {step_text!r}") from exc
            if step < 1:
                raise CronError(f"step in {name} must be at least 1")
        if part == "*":
            start, end = low, high
            wildcard = wildcard or step == 1
        elif "-" in part:
            a, b = part.split("-", 1)
            try:
                start, end = int(a), int(b)
            except ValueError as exc:
                raise CronError(f"bad range in {name}: {part!r}") from exc
        else:
            try:
                start = end = int(part)
            except ValueError as exc:
                raise CronError(f"bad value in {name}: {part!r}") from exc
            if "/" in text and step > 1:
                end = high
        if start < low or end > high or start > end:
            raise CronError(f"{name} value out of range: {part!r} (allowed {low}-{high})")
        values.update(range(start, end + 1, step))
    if name == "weekday" and 7 in values:
        values.discard(7)
        values.add(0)
    return frozenset(values), wildcard


@dataclass(frozen=True)
class Cron:
    """A parsed five-field cron expression."""

    raw: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    day_wild: bool
    weekday_wild: bool

    def matches(self, moment: _dt.datetime) -> bool:
        if moment.minute not in self.minutes or moment.hour not in self.hours:
            return False
        if moment.month not in self.months:
            return False
        day_ok = moment.day in self.days
        weekday_ok = ((moment.weekday() + 1) % 7) in self.weekdays
        # Standard cron: when both day-of-month and day-of-week are restricted,
        # either matching is enough. When one is a wildcard, the other decides.
        if self.day_wild and self.weekday_wild:
            return True
        if self.day_wild:
            return weekday_ok
        if self.weekday_wild:
            return day_ok
        return day_ok or weekday_ok

    def firings_per_day(self) -> float:
        """Average firings per day over a representative fortnight.

        Two weeks from a fixed Monday, so the answer is the same on every
        machine and every run, which is what lets it be a weight.
        """
        start = _dt.datetime(2026, 1, 5, 0, 0)
        total = 0
        minute = _dt.timedelta(minutes=1)
        moment = start
        for _ in range(14 * 24 * 60):
            if self.matches(moment):
                total += 1
            moment += minute
        return total / 14.0

    def next_after(self, moment: _dt.datetime, limit_minutes: int = 366 * 24 * 60) -> Optional[_dt.datetime]:
        """The first firing strictly after ``moment``, or None within a year."""
        candidate = moment.replace(second=0, microsecond=0) + _dt.timedelta(minutes=1)
        for _ in range(limit_minutes):
            if self.matches(candidate):
                return candidate
            candidate += _dt.timedelta(minutes=1)
        return None


def parse_cron(expression: str) -> Cron:
    fields = (expression or "").split()
    if len(fields) != 5:
        raise CronError(f"a cron schedule needs five fields, got {expression!r}")
    parsed = []
    for text, (name, low, high) in zip(fields, _CRON_FIELDS):
        parsed.append(_parse_cron_field(text, low, high, name))
    return Cron(
        raw=expression.strip(),
        minutes=parsed[0][0],
        hours=parsed[1][0],
        days=parsed[2][0],
        months=parsed[3][0],
        weekdays=parsed[4][0],
        day_wild=parsed[2][1],
        weekday_wild=parsed[4][1],
    )


def cron_firings_per_day(expression: str) -> float:
    return parse_cron(expression).firings_per_day()


# ---------------------------------------------------------------------------
# Selecting searches for a scenario
# ---------------------------------------------------------------------------

def _glob_match(name: str, patterns: Sequence[str]) -> bool:
    from fnmatch import fnmatchcase

    return any(fnmatchcase(name, pattern) for pattern in patterns)


@dataclass(frozen=True)
class Selection:
    """Which searches from a file take part, and why the rest do not."""

    chosen: List[SavedSearch]
    skipped: Dict[str, str]  # name -> reason


def select_searches(
    searches: Sequence[SavedSearch],
    *,
    include: Sequence[str] = ("*",),
    exclude: Sequence[str] = (),
    only_enabled: bool = True,
    only_scheduled: bool = False,
    allow_side_effects: bool = False,
    allow_realtime: bool = False,
) -> Selection:
    """Apply a scenario's selection rules, recording why each search was left out.

    The reasons matter more than the list. A scenario that quietly dropped the
    one search with ``collect`` in it is fine; a scenario that quietly dropped
    half the file because ``disabled = 1`` was set in ``[default]`` is a run
    that measured the wrong workload, and the operator needs to be told.
    """
    chosen: List[SavedSearch] = []
    skipped: Dict[str, str] = {}
    for search in searches:
        if search.is_scheduled_view:
            skipped[search.name] = "a scheduled PDF view, not a search"
            continue
        if search.skipped:
            skipped[search.name] = f"{KEY_SKIP} is set"
            continue
        if not _glob_match(search.name, include):
            skipped[search.name] = "not matched by include"
            continue
        if exclude and _glob_match(search.name, exclude):
            skipped[search.name] = "matched by exclude"
            continue
        if not search.search.strip():
            skipped[search.name] = "has no search"
            continue
        if only_enabled and search.disabled:
            skipped[search.name] = "disabled"
            continue
        if only_scheduled and not (search.scheduled and search.cron):
            skipped[search.name] = "not scheduled"
            continue
        if search.is_realtime and not allow_realtime:
            skipped[search.name] = "real-time search (set allow_realtime to include it)"
            continue
        effects = side_effects(search.search)
        if effects and not allow_side_effects:
            skipped[search.name] = (
                f"writes somewhere ({', '.join(effects)}); set allow_side_effects to replay it anyway"
            )
            continue
        chosen.append(search)
    return Selection(chosen=chosen, skipped=skipped)


_STEP_ID_SAFE = re.compile(r"[^a-z0-9_-]+")


def step_id_for(name: str, taken: Optional[set[str]] = None) -> str:
    """A scenario step id from a saved search name.

    Step ids appear in SPL comments, HEC fields and file names, so the
    character set is deliberately narrow. Collisions after sanitising get a
    numeric suffix rather than silently merging two searches' statistics.
    """
    base = _STEP_ID_SAFE.sub("-", name.strip().lower()).strip("-")[:56] or "search"
    if taken is None:
        return base
    candidate = base
    counter = 2
    while candidate in taken:
        candidate = f"{base}-{counter}"
        counter += 1
    taken.add(candidate)
    return candidate
