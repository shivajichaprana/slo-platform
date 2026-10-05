#!/usr/bin/env python3
"""Shared model for the indicator sources, and the vocabulary they report in.

An objective specification says what proportion of events must be good. It does
not say how to obtain that proportion, because the answer is a different
sentence in every metric system. A source adapter is the translation: it takes
one objective and produces the request payloads that the backing system would
have to answer, plus a list of findings about what the translation could not
preserve.

Three properties of that job shape everything here.

**An adapter compiles; it never queries.** Nothing in this package opens a
socket, signs a request or reads a credential. The output is a payload a caller
may send, which keeps the translation testable without an account and keeps the
one thing worth reviewing -- the arithmetic -- in a file rather than in a log.

**A translation that loses something must say so.** A query that cannot be
evaluated over the window it was handed, a comparison a backend can only
approximate, a missing data point that reads as success: each is a finding with
a code, not a silent substitution. Every finding here describes a configuration
the backend accepts and reports on, which is why none of them can be left to be
noticed later.

**The two sources are not interchangeable.** They do not accept the same
indicator kinds, they do not reach as far back as each other, and their alert
limits differ by two orders of magnitude. A document is written against one of
them whether or not it says so, so an adapter asked to read a foreign dialect
refuses instead of producing a configuration that deploys cleanly and measures
nothing.

Finding codes are grouped so a report can be read by severity of consequence:

====  ==========================================================================
S1xx  The adapter was handed something it cannot read.
S2xx  Resolution, retention and reach: the query is legal but the answer would
      cover a different span than the objective asks for.
S3xx  Semantics: the backend answers a subtly different question.
S4xx  Service limits that decide whether an alert can exist at all.
====  ==========================================================================
"""

from __future__ import annotations

import abc
import json
import logging
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

try:
    import yaml
except ImportError:  # pragma: no cover - reported, not raised
    print("error: PyYAML is required (pip install pyyaml)", file=sys.stderr)
    raise SystemExit(2)

LOG = logging.getLogger("slo.sources")

DURATION = re.compile(r"^([1-9][0-9]*)(m|h|d)$")
UNIT_SECONDS = {"m": 60, "h": 3600, "d": 86400}

# A calendar period is not a duration -- a month is 28, 29, 30 or 31 days -- so
# budget arithmetic uses a nominal length and labels every figure derived from
# it. This table is the same one the specification validator applies; the two
# are asserted equal by the repository's own checks rather than kept in step by
# hand, because a budget that differs between the report and the alert is a
# disagreement nobody would think to look for.
NOMINAL_CALENDAR_SECONDS = {
    "week": 7 * 86400,
    "month": 30 * 86400,
    "quarter": 91 * 86400,
}

# The deployment prefix, the environment and a tier suffix share a
# 64-character ceiling with an objective's identity, which leaves 32.
OBJECTIVE_KEY_BUDGET = 32

# Scrape or publication interval assumed when the caller does not supply one.
# It is an input rather than a constant because it is a property of the
# deployment and not of the objective, and because the finest window a rate can
# be computed over is a multiple of it.
DEFAULT_SAMPLE_INTERVAL_SECONDS = 60

SEVERITIES = ("error", "warning", "note")


@dataclass(frozen=True)
class Finding:
    """One thing the translation could not carry across, with its arithmetic."""

    code: str
    severity: str
    where: str
    message: str

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise ValueError(f"unknown severity: {self.severity!r}")

    def render(self) -> str:
        return f"{self.severity.upper():7s} {self.code}  {self.where}\n          {self.message}"


def error(code: str, where: str, message: str) -> Finding:
    return Finding(code, "error", where, message)


def warn(code: str, where: str, message: str) -> Finding:
    return Finding(code, "warning", where, message)


def note(code: str, where: str, message: str) -> Finding:
    return Finding(code, "note", where, message)


def parse_duration_seconds(text: str) -> int:
    """Seconds in a `<n><m|h|d>` duration. Raises ValueError on anything else."""
    match = DURATION.match(text)
    if not match:
        raise ValueError(f"not a duration: {text!r}")
    return int(match.group(1)) * UNIT_SECONDS[match.group(2)]


def humanise(seconds: float) -> str:
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


@dataclass(frozen=True)
class Window:
    """The span an objective is measured over, in seconds."""

    kind: str
    seconds: int
    nominal: bool
    label: str

    @classmethod
    def from_spec(cls, window: dict[str, Any]) -> "Window":
        if window["kind"] == "rolling":
            duration = window["duration"]
            return cls("rolling", parse_duration_seconds(duration), False, duration)
        period = window["period"]
        if period not in NOMINAL_CALENDAR_SECONDS:
            raise ValueError(f"unknown calendar period: {period!r}")
        return cls("calendar", NOMINAL_CALENDAR_SECONDS[period], True, period)


@dataclass(frozen=True)
class Tier:
    """One burn-rate tier, resolved to seconds."""

    name: str
    burn_rate: float
    long_seconds: int
    short_seconds: int
    notify: str

    @classmethod
    def from_spec(cls, tier: dict[str, Any]) -> "Tier":
        return cls(
            name=tier["name"],
            burn_rate=float(tier["burn_rate"]),
            long_seconds=parse_duration_seconds(tier["long_window"]),
            short_seconds=parse_duration_seconds(tier["short_window"]),
            notify=tier["notify"],
        )

    def fires_at_error_rate(self, allowed: float) -> float:
        """The error rate this tier fires at: burn rate x the budget."""
        return self.burn_rate * allowed


@dataclass(frozen=True)
class Objective:
    """One objective, with the parts an adapter needs already resolved."""

    service: str
    name: str
    target: float
    sli: dict[str, Any]
    window: Window
    tiers: tuple[Tier, ...]
    tier_policy: str | None = None
    document: str | None = None
    # Prose and accountability. Neither is used to compile a query; both are
    # carried because a generated alert that does not say what the objective
    # was, or who owns the budget decision, is answered by reading the
    # specification -- which is the one thing a responder cannot do quickly.
    title: str = ""
    owner: str = ""

    @classmethod
    def from_spec(cls, doc: dict[str, Any], obj: dict[str, Any], document: str | None = None) -> "Objective":
        return cls(
            service=doc["metadata"]["service"],
            name=obj["name"],
            target=float(obj["objective"]),
            sli=obj["sli"],
            window=Window.from_spec(obj["window"]),
            tiers=tuple(Tier.from_spec(t) for t in obj["alerting"]["tiers"]),
            tier_policy=doc["metadata"].get("tier"),
            document=document,
            title=obj.get("title", ""),
            owner=doc["metadata"].get("owner", ""),
        )

    @property
    def allowed(self) -> float:
        """The error budget as a proportion: everything the objective permits."""
        return 1.0 - self.target

    @property
    def key(self) -> str:
        return f"{self.service}-{self.name}"

    @property
    def where(self) -> str:
        prefix = f"{self.document} :: " if self.document else ""
        return f"{prefix}{self.service}/{self.name}"

    def sampling_per_hour(self) -> float | None:
        sampling = self.sli.get("sampling")
        return float(sampling["expected_events_per_hour"]) if sampling else None


@dataclass(frozen=True)
class Recognition:
    """Whether a source believes an indicator is written in its own dialect.

    Recognition is a heuristic and is reported as one. The specification has no
    field naming the system a query is written for, deliberately: a query string
    couples the objective to a backend already, and a second field saying so can
    disagree with the string. So the adapter reads the strings, and refuses on
    anything it cannot place rather than guessing -- because the failure it is
    guarding against is a whole configuration that applies cleanly, creates
    every alert, and evaluates nothing.
    """

    dialect: str | None
    claimed: bool
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class CompiledQuery:
    """A payload a caller may send, and what was lost producing it.

    `purpose` is either `budget` -- the whole-window figure a report quotes --
    or `tier:<name>:<long|short>`, one of the two spans a burn-rate tier
    compares. The distinction is not cosmetic: the two are compiled by
    different rules on both backends, and the difference is the subject of
    several findings here.
    """

    purpose: str
    dialect: str
    payload: dict[str, Any]
    findings: tuple[Finding, ...] = ()


@dataclass
class CompiledObjective:
    """Everything one adapter could say about one objective."""

    source: str
    key: str
    where: str
    budget: CompiledQuery | None = None
    tiers: list[CompiledQuery] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    def all_findings(self) -> list[Finding]:
        out = list(self.findings)
        if self.budget:
            out.extend(self.budget.findings)
        for tier in self.tiers:
            out.extend(tier.findings)
        return out

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.all_findings() if f.severity == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.all_findings() if f.severity == "warning"]

    @property
    def deployable(self) -> bool:
        """No errors, and something to deploy. An objective that compiled to
        nothing is not deployable even when nothing was wrong with it."""
        return not self.errors and (self.budget is not None or bool(self.tiers))

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "objective": self.key,
            "where": self.where,
            "deployable": self.deployable,
            "budget": asdict(self.budget) if self.budget else None,
            "tiers": [asdict(t) for t in self.tiers],
            "findings": [asdict(f) for f in self.all_findings()],
        }


class SliSource(abc.ABC):
    """Translates one objective into one metric system's requests."""

    #: Short name a caller selects the source by.
    name: str = ""

    #: Dialects this source can read, most specific first.
    dialects: tuple[str, ...] = ()

    @abc.abstractmethod
    def recognise(self, sli: dict[str, Any]) -> Recognition:
        """Decide whether this indicator is written for this source."""

    @abc.abstractmethod
    def compile(self, objective: Objective) -> CompiledObjective:
        """Produce the payloads, and the findings about what they cannot say."""

    # -- helpers shared by every source -------------------------------------

    def _foreign(self, objective: Objective, recognition: Recognition) -> CompiledObjective:
        reasons = "; ".join(recognition.reasons) or "no feature of the query is recognisable"
        return CompiledObjective(
            source=self.name,
            key=objective.key,
            where=objective.where,
            findings=[error(
                "S100", objective.where,
                f"the indicator is not written for {self.name}: {reasons}. Nothing is compiled, "
                f"because the alternative is a configuration that applies cleanly, creates every "
                f"alert and measures nothing -- the queries are accepted as opaque strings by the "
                f"API and fail only as an absence of data, which an alert reads as health. "
                f"Available sources: {', '.join(available_sources())}.",
            )],
        )


_REGISTRY: dict[str, SliSource] = {}


def register(source: SliSource) -> SliSource:
    if not source.name:
        raise ValueError("a source must have a name")
    _REGISTRY[source.name] = source
    return source


def get_source(name: str) -> SliSource:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(f"unknown source {name!r}; available: {', '.join(available_sources())}") from None


def available_sources() -> list[str]:
    return sorted(_REGISTRY)


def point_budget(span_seconds: int, step_seconds: int, cap: int) -> tuple[int, int]:
    """Points a range query would return, and the smallest step that fits `cap`.

    Returned as a pair so a caller can report both the figure that was refused
    and the one that would be accepted, rather than only that something was too
    large.
    """
    if step_seconds <= 0:
        raise ValueError("step must be positive")
    points = -(-span_seconds // step_seconds)  # ceiling
    needed = -(-span_seconds // cap) if cap > 0 else step_seconds
    return points, max(needed, 1)


def load_document(path: Path) -> Any:
    """Read one specification document. Exits 2 on anything unreadable."""
    try:
        with path.open(encoding="utf-8") as handle:
            return yaml.safe_load(handle)
    except OSError as exc:
        print(f"error: cannot read {path}: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    except yaml.YAMLError as exc:
        # Exit 2 rather than 1, for the reason the validator gives: a document
        # that could not be read is a different outcome from one that was read
        # and found wanting, and a pipeline step unable to tell them apart
        # reports a healthy repository as broken specifications.
        print(f"error: {path}: not parseable as YAML: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def objectives_in(doc: Any, label: str) -> Iterable[Objective]:
    """Resolve every objective in one parsed document.

    The document is assumed to have passed the schema already. This is not a
    second validator: an adapter that re-checked shape here would report shape
    faults in the vocabulary of the metric system, which is the wrong place to
    read them.
    """
    if not isinstance(doc, dict) or not isinstance(doc.get("objectives"), list):
        print(f"error: {label}: not an objective document -- validate it first", file=sys.stderr)
        raise SystemExit(2)
    for obj in doc["objectives"]:
        yield Objective.from_spec(doc, obj, document=label)


def discover(target: Path, pattern: str) -> list[Path]:
    if target.is_file():
        return [target]
    if not target.is_dir():
        print(f"error: {target} is neither a file nor a directory", file=sys.stderr)
        raise SystemExit(2)
    return sorted(target.glob(pattern))


def cli_main(source_name: str, argv: Sequence[str] | None = None) -> int:
    """Compile every objective under a path with one source, and report.

    Present so a source can be exercised on a real document without the
    generator that will consume it. Exit status matches the validator's
    convention: 0 clean, 1 findings, 2 usage or input failure.
    """
    import argparse

    parser = argparse.ArgumentParser(
        description=f"Compile objective specifications for the {source_name} source.",
    )
    parser.add_argument("target", nargs="?", default=Path("specs"), type=Path,
                        help="specification file, or directory to search (default: specs)")
    parser.add_argument("--pattern", default="*.yaml", help="glob used when target is a directory")
    parser.add_argument("--json", dest="as_json", action="store_true",
                        help="emit payloads and findings as JSON")
    parser.add_argument("--strict", action="store_true", help="treat warnings as failures")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    source = get_source(source_name)
    paths = discover(args.target, args.pattern)
    if not paths:
        print(f"no specification matched {args.pattern!r} under {args.target} -- nothing was compiled")
        return 0

    compiled: list[CompiledObjective] = []
    for path in paths:
        doc = load_document(path)
        if doc is None:
            print(f"error: {path}: document is empty", file=sys.stderr)
            return 2
        for objective in objectives_in(doc, path.name):
            LOG.debug("compiling %s for %s", objective.key, source_name)
            compiled.append(source.compile(objective))

    if args.as_json:
        print(json.dumps({"source": source_name, "objectives": [c.as_dict() for c in compiled]}, indent=2))
    else:
        for result in compiled:
            state = "deployable" if result.deployable else "NOT DEPLOYABLE"
            print(f"\n{result.where}  [{state}]")
            for finding in result.all_findings():
                print(finding.render())

    errors = sum(len(c.errors) for c in compiled)
    warnings = sum(len(c.warnings) for c in compiled)
    print(f"\n{len(compiled)} objective(s), {errors} error(s), {warnings} warning(s) for {source_name}")
    if errors:
        return 1
    return 1 if (args.strict and warnings) else 0
