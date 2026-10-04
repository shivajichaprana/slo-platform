#!/usr/bin/env python3
"""Compile an objective into PromQL, and report what the language cannot say.

Managed Prometheus answers PromQL over the Prometheus HTTP API -- an instant
query for a single figure, a range query for a series -- so an objective
compiles to query parameters rather than to alarm objects. The translation is
mostly mechanical. Four things about it are not, and each is a configuration
the engine accepts and answers without complaint.

**A query carrying its own range selector is pinned to it.** `rate(x[5m])` is a
five-minute rate wherever it is evaluated, so handing it to a one-hour tier and
a three-day tier produces the same number twice and the multi-window structure
above it is decoration -- the arrangement this repository exists to stop being
decoration. Indicator queries therefore template the window as `$window`, and a
query with a literal range and no placeholder is refused rather than
substituted, because guessing which of several ranges to rewrite is how a
subtly wrong query gets deployed.

**An absent series is not a zero.** `sum()` over a selector that matches
nothing returns no sample at all, so when the exporter stops or a label is
renamed the whole expression is empty, an alerting rule over it fires on
nothing, and the objective reports as met. Division makes it worse rather than
better: zero over zero is NaN, every comparison against NaN is false, and no
traffic is therefore indistinguishable from healthy traffic. The compiled
expressions handle the numerator explicitly and each objective also gets a
staleness expression, because the only honest way to tell "nothing failed" from
"nothing was measured" is to ask separately.

**A rate needs at least two samples in its range.** A window shorter than
twice the scrape interval yields nothing -- not a zero, nothing -- so a tier
built on a short window can be unable to fire for a reason that has nothing to
do with the service. The interval is an input here because it is a property of
the deployment, not of the objective.

**A histogram boundary is a string, and the bucket has to exist.** A latency
objective compiles to a cumulative bucket selector, and `le` is matched as a
label value: `le="0.30"` does not match a bucket published as `0.3`. If the
boundary was never configured, the selector matches nothing, the numerator is
empty, and the objective reads as met. A classic histogram also cannot express
a strict comparison at all -- a bucket counts observations at or below its
boundary -- so `less_than` is compiled as `less_than_or_equal` and said to be.
"""

from __future__ import annotations

import math
import re
from typing import Any

try:
    from .base import (
        DEFAULT_SAMPLE_INTERVAL_SECONDS,
        CompiledObjective,
        CompiledQuery,
        Finding,
        Objective,
        Recognition,
        SliSource,
        cli_main,
        error,
        humanise,
        note,
        point_budget,
        register,
        warn,
    )
except ImportError:  # pragma: no cover - direct execution without the package
    from base import (  # type: ignore[no-redef]
        DEFAULT_SAMPLE_INTERVAL_SECONDS,
        CompiledObjective,
        CompiledQuery,
        Finding,
        Objective,
        Recognition,
        SliSource,
        cli_main,
        error,
        humanise,
        note,
        point_budget,
        register,
        warn,
    )

DIALECT = "promql"

#: The placeholder an indicator query uses where its range selector belongs.
WINDOW_PLACEHOLDER = "$window"

#: Points one range query may return per series before the API refuses it.
MAX_RANGE_POINTS = 11_000

#: Samples a rate needs in its range: two to compute one, and enough more that
#: a single missed scrape does not empty it.
MIN_SAMPLES_FOR_RATE = 2
COMFORTABLE_SAMPLES_FOR_RATE = 4

#: Prometheus convention is base units, so a threshold has to be converted into
#: the unit the histogram was published in before it can name a bucket.
UNIT_TO_BASE = {"seconds": 1.0, "milliseconds": 0.001, "bytes": 1.0, "count": 1.0}
#: Units whose base form the convention actually fixes. A bare count has no
#: base unit, so its boundary is taken verbatim and said to be.
UNITS_WITH_BASE = frozenset({"seconds", "milliseconds", "bytes"})

#: Comparisons a cumulative bucket expresses exactly, and the ones it does not.
BUCKET_EXACT = {"less_than_or_equal", "greater_than"}
BUCKET_APPROXIMATE = {"less_than": "less_than_or_equal", "greater_than_or_equal": "greater_than"}

_WINDOW_TOKEN = re.compile(re.escape(WINDOW_PLACEHOLDER) + r"\b")
_RANGE_SELECTOR = re.compile(r"\[\s*[0-9]+(?:\.[0-9]+)?[smhdwy](?:[0-9]+[smhdwy])*\s*\]")
_SELECT = re.compile(r"^\s*SELECT\s", re.IGNORECASE)
_RATE_FUNCTIONS = re.compile(r"\b(rate|irate|increase|sum_over_time|count_over_time|delta)\s*\(")
_METRIC_NAME = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")


def promql_duration(seconds: int) -> str:
    """Render seconds as the coarsest exact PromQL duration.

    Exact rather than approximate on purpose: a duration rounded here would
    make the evaluated window differ from the one the threshold was computed
    from, which is the same class of fault as a pinned range selector.
    """
    if seconds <= 0:
        raise ValueError("duration must be positive")
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def format_le(value: float) -> str:
    """Render a bucket boundary the way an exposition format would.

    The `le` label is matched as a string, so this is not cosmetic: a boundary
    rendered `0.30` selects no bucket from a histogram that published `0.3`,
    the numerator is empty, and the objective reports as met.
    """
    if value == math.floor(value) and abs(value) < 1e15:
        return str(int(value))
    return f"{value:.10g}"


def substitute_bucket(query: str, metric: str, le: str) -> tuple[str, int]:
    """Swap a histogram's observation count for one of its cumulative buckets.

    Returns the rewritten query and how many series were substituted. The
    substitution is textual and deliberately narrow: it matches the bare
    `<metric>_count` series name and merges `le` into whatever label matcher
    already follows it, so the surrounding query -- its selection, its
    range-vector function, its aggregation -- is carried over untouched. A
    query where no such series appears is reported rather than rewritten.
    """
    pattern = re.compile(re.escape(metric) + r"_count\b(\s*\{(?P<matchers>[^{}]*)\})?")

    def replace(match: "re.Match[str]") -> str:
        existing = (match.group("matchers") or "").strip().rstrip(",").strip()
        matchers = f'le="{le}"' + (f", {existing}" if existing else "")
        return f"{metric}_bucket{{{matchers}}}"

    rewritten, count = pattern.subn(replace, query)
    return rewritten, count


def substitute_window(query: str, seconds: int) -> str:
    return _WINDOW_TOKEN.sub(promql_duration(seconds), query)


class PrometheusSource(SliSource):
    """PromQL against the Prometheus HTTP query API.

    `sample_interval_seconds` is the scrape or remote-write interval of the
    series the indicator reads. It is not in the specification because it is
    not a property of the objective: the same objective is measured from a
    15-second scrape in one environment and a one-minute scrape in another, and
    only the second of those can be alerted on over a one-minute window.
    """

    name = "prometheus"
    dialects = (DIALECT,)

    def __init__(self, sample_interval_seconds: int = DEFAULT_SAMPLE_INTERVAL_SECONDS) -> None:
        if sample_interval_seconds <= 0:
            raise ValueError("sample interval must be positive")
        self.sample_interval_seconds = sample_interval_seconds

    # -- recognition --------------------------------------------------------

    def recognise(self, sli: dict[str, Any]) -> Recognition:
        queries = [q for q in (sli.get("valid_query"), sli.get("good_query")) if q]
        if not queries:
            return Recognition(None, False, ("the indicator carries no query to read",))
        if any(_SELECT.match(q) for q in queries):
            return Recognition(
                None, False,
                ("the query is a SELECT statement, which is a Metrics Insights statement rather "
                 "than PromQL",),
            )

        reasons: list[str] = []
        for query in queries:
            if _WINDOW_TOKEN.search(query):
                reasons.append(f"a query templates its range as {WINDOW_PLACEHOLDER}")
                break
        else:
            if any(_RATE_FUNCTIONS.search(q) for q in queries):
                reasons.append("a query uses a range-vector function")
            elif any(_RANGE_SELECTOR.search(q) for q in queries):
                reasons.append("a query carries a range selector")
            elif any("{" in q for q in queries):
                reasons.append("a query uses label matchers")
        if not reasons:
            return Recognition(
                None, False,
                ("no PromQL construct is recognisable: no range-vector function, no range "
                 "selector, no label matcher and no window placeholder",),
            )
        return Recognition(DIALECT, True, tuple(reasons))

    # -- compilation --------------------------------------------------------

    def compile(self, objective: Objective) -> CompiledObjective:
        kind = objective.sli["kind"]
        result = CompiledObjective(source=self.name, key=objective.key, where=objective.where)

        recognition = self.recognise(objective.sli)
        if not recognition.claimed:
            return self._foreign(objective, recognition)

        if kind == "ratio":
            good = objective.sli["good_query"].strip()
            valid = objective.sli["valid_query"].strip()
            for label, query in (("good_query", good), ("valid_query", valid)):
                result.findings.extend(self._check_templating(objective.where, label, query))
        else:
            # A threshold indicator supplies no good query: the good count is
            # derived from the metric, the bound and the comparison, which is
            # the whole reason the kind exists.
            valid = objective.sli["valid_query"].strip()
            result.findings.extend(self._check_templating(objective.where, "valid_query", valid))
            good, derived = self._bucket_numerator(objective, valid)
            result.findings.extend(derived)

        if result.errors:
            return result

        result.findings.append(note(
            "S302", objective.where,
            "every tier expression defaults its numerator to zero, because a selector that matches "
            "nothing returns no sample: a window in which every event failed would otherwise "
            "produce an empty expression, and an alerting rule over nothing does not fire. No "
            "denominator is defaulted, on purpose -- zero over zero is NaN, every comparison "
            "against NaN is false, and an absent denominator means nothing was measured rather "
            "than nothing failed. The staleness expression is what reports that case.",
        ))
        result.budget = self._budget_query(objective, good, valid)
        for tier in objective.tiers:
            for span_name, span in (("long", tier.long_seconds), ("short", tier.short_seconds)):
                result.tiers.append(self._tier_query(objective, tier, span_name, span, good, valid))
        result.tiers.append(self._staleness_query(objective, valid))
        return result

    def _check_templating(self, where: str, label: str, query: str) -> list[Finding]:
        findings: list[Finding] = []
        templated = _WINDOW_TOKEN.search(query) is not None
        stripped = _WINDOW_TOKEN.sub("", query)
        literal_ranges = _RANGE_SELECTOR.findall(stripped)

        if literal_ranges and not templated:
            findings.append(error(
                "S300", where,
                f"{label} pins its own range to {', '.join(sorted(set(literal_ranges)))}, so it "
                f"evaluates to the same figure for every window it is handed: the budget window, "
                f"the long window of each tier and the short window of each tier would all read "
                f"one rate. Every threshold above the fastest tier then fires on the same "
                f"condition and the multi-window policy is decoration. Write the range as "
                f"{WINDOW_PLACEHOLDER} -- the window is supplied per evaluation, and rewriting one "
                f"of several literal ranges automatically would be a guess about which of them is "
                f"the indicator's.",
            ))
        elif literal_ranges and templated:
            findings.append(warn(
                "S300", where,
                f"{label} templates its range and ALSO carries the literal range(s) "
                f"{', '.join(sorted(set(literal_ranges)))}, which stay fixed while the placeholder "
                f"moves. A subquery or a nested rate may want that; if it does not, the two parts "
                f"of the query disagree about which window the objective is measured over.",
            ))
        elif not templated and _RATE_FUNCTIONS.search(query):
            findings.append(error(
                "S300", where,
                f"{label} applies a range-vector function with no range at all, which is not a "
                f"valid query. Write the range as {WINDOW_PLACEHOLDER}.",
            ))
        elif not templated:
            findings.append(warn(
                "S301", where,
                f"{label} has no {WINDOW_PLACEHOLDER} placeholder, so the same instant value is "
                f"compared at every window. An indicator is a count over a window; a gauge read at "
                f"an instant cannot produce an error budget, because there is nothing to integrate.",
            ))
        return findings

    def _bucket_numerator(self, objective: Objective, valid: str) -> tuple[str, list[Finding]]:
        """Derive the good-event count for a threshold indicator.

        The numerator is produced by REWRITING the denominator -- swapping the
        histogram's observation count for one of its cumulative buckets --
        rather than by composing a fresh query from the metric name. That is
        the only construction that guarantees the two halves count the same
        population: a numerator written independently inherits none of the
        denominator's label selection, so it counts the histogram across every
        service while the denominator counts one, and the resulting proportion
        can sit anywhere including above 1 without ever looking implausible.
        Writing it this way also keeps the two halves on the same range-vector
        function, which matters more than it looks: a rate over a bucket
        divided by an increase over a count differs from the intended ratio by
        the length of the window, so the tier fires permanently and the
        arithmetic that produced its threshold is never consulted again.
        """
        sli = objective.sli
        where = objective.where
        findings: list[Finding] = []
        metric = str(sli["metric"]).strip()
        unit = sli["unit"]
        comparison = sli["comparison"]
        threshold = float(sli["threshold"])

        if not _METRIC_NAME.match(metric):
            findings.append(error(
                "S101", where,
                f"metric {metric!r} is not a bare Prometheus metric name, and the bucket series is "
                f"derived from it by substitution into valid_query. Give the metric name alone and "
                f"put any selection in valid_query.",
            ))
            return "", findings

        boundary = threshold * UNIT_TO_BASE[unit]
        le = format_le(boundary)
        if unit not in UNITS_WITH_BASE:
            findings.append(note(
                "S205", where,
                f"the unit is {unit!r}, which has no base form in the metric naming convention, so "
                f"the boundary is used verbatim as {le}. For a duration or a size the convention "
                f"fixes the base unit and the conversion is unambiguous; for a bare count it is the "
                f"publisher's choice.",
            ))
        elif unit == "milliseconds":
            findings.append(note(
                "S205", where,
                f"the threshold is {threshold:g} milliseconds and the convention publishes "
                f"durations in seconds, so the bucket boundary is le=\"{le}\".",
            ))

        at_or_below, substitutions = substitute_bucket(valid, metric, le)
        if not substitutions:
            findings.append(error(
                "S309", where,
                f"valid_query does not read {metric}_count, so the good-event count cannot be "
                f"derived from it. The numerator is the denominator with the observation count "
                f"swapped for a cumulative bucket, which is what makes the two halves count the "
                f"same population and use the same range-vector function. A numerator composed "
                f"independently inherits none of this query's label selection and would count the "
                f"histogram across every series it has.",
            ))
            return "", findings

        if comparison in BUCKET_APPROXIMATE:
            findings.append(warn(
                "S304", where,
                f"the comparison is {comparison!r} and a cumulative bucket counts observations AT "
                f"OR BELOW its boundary, so no selector expresses a strict comparison at the "
                f"boundary itself. It is compiled as {BUCKET_APPROXIMATE[comparison]!r}: events "
                f"measured at exactly {le} are counted as good. The difference is the mass sitting "
                f"precisely on the boundary, which is usually negligible and is not always -- a "
                f"client-side timeout set to the same value piles observations there.",
            ))

        findings.append(note(
            "S306", where,
            f"the numerator selects le=\"{le}\", matched as a LABEL VALUE and not as a number. A "
            f"histogram that published this boundary in another form, or that was configured "
            f"without it, matches nothing: the numerator is then empty, the ratio is absent, and "
            f"the objective reads as met. Confirm the bucket exists before the objective is "
            f"trusted; nothing in the specification can.",
        ))

        if comparison in ("greater_than", "greater_than_or_equal"):
            findings.append(note(
                "S307", where,
                f"good means above the boundary, which a cumulative bucket reaches only by "
                f"subtracting it from the observation count, so the numerator is the denominator "
                f"minus the le=\"{le}\" bucket. Both halves therefore come from one histogram and "
                f"one query, which is the only way the subtraction is guaranteed to be of "
                f"comparable counts.",
            ))
            return f"(({valid}) - ({at_or_below}))", findings
        return at_or_below, findings

    def _budget_query(self, objective: Objective, good: str, valid: str) -> CompiledQuery:
        window = objective.window.seconds
        duration = promql_duration(window)
        findings: list[Finding] = []

        good_total = substitute_window(_as_total(good), window)
        valid_total = substitute_window(_as_total(valid), window)
        expression = f"({good_total}) / ({valid_total})"

        findings.append(note(
            "S206", objective.where,
            f"the window total is taken with a {duration} range selector, so one evaluation reads "
            f"every sample in the window. That is correct and expensive; a recorded ratio "
            f"evaluated continuously and aggregated is the same figure at a fraction of the cost, "
            f"and is what a dashboard should read.",
        ))
        findings.append(note(
            "S207", objective.where,
            "an increase over a range is extrapolated to the range edges, so the counts are not "
            "integers and the budget in events is an estimate rather than a tally. The proportion "
            "is barely affected, because both halves are extrapolated the same way; a budget "
            "quoted as a number of events is.",
        ))
        if objective.window.nominal:
            findings.append(note(
                "S204", objective.where,
                f"the window is a calendar {objective.window.label}, so the {duration} range is "
                f"nominal. A figure reported against the real period boundary has to be asked for "
                f"between the boundaries themselves rather than over a trailing span.",
            ))

        step = self.sample_interval_seconds
        points, needed = point_budget(window, step, MAX_RANGE_POINTS)
        if points > MAX_RANGE_POINTS:
            findings.append(note(
                "S201", objective.where,
                f"as a range query at a {step}s step this window is {points:,} points per series "
                f"and the API refuses more than {MAX_RANGE_POINTS:,}; the smallest step that fits "
                f"is {needed}s ({humanise(needed)}). The instant query below is unaffected -- it "
                f"returns one point -- but a chart over the same window is not.",
            ))

        return CompiledQuery(
            "budget", DIALECT,
            {
                "path": "/api/v1/query",
                "method": "POST",
                "params": {"query": expression},
                "range_query_step_seconds": max(step, needed),
                "service": "aps",
                "iam_action": "aps:QueryMetrics",
            },
            tuple(findings),
        )

    def _tier_query(
        self, objective: Objective, tier: Any, span_name: str, span: int, good: str, valid: str,
    ) -> CompiledQuery:
        where = f"{objective.where} :: tier {tier.name} ({span_name})"
        findings: list[Finding] = []
        threshold = tier.fires_at_error_rate(objective.allowed)

        samples = span / self.sample_interval_seconds
        if samples < MIN_SAMPLES_FOR_RATE:
            findings.append(error(
                "S203", where,
                f"the {span_name} window is {humanise(span)} and the sample interval is "
                f"{self.sample_interval_seconds}s, so the range holds about {samples:.1f} samples. "
                f"A rate needs at least {MIN_SAMPLES_FOR_RATE}; below that the expression returns "
                f"no samples at all rather than zero, so the tier can never fire and nothing "
                f"reports that it cannot.",
            ))
        elif samples < COMFORTABLE_SAMPLES_FOR_RATE:
            findings.append(warn(
                "S203", where,
                f"the {span_name} window holds about {samples:.1f} samples at a "
                f"{self.sample_interval_seconds}s interval. One missed scrape drops it below the "
                f"two a rate requires, and the tier then stops evaluating rather than evaluating "
                f"to zero.",
            ))

        good_rate = substitute_window(good, span)
        valid_rate = substitute_window(valid, span)
        # The numerator is defaulted, the denominator deliberately is not: an
        # absent denominator means nothing was measured, and substituting zero
        # there would turn that into a division by zero reported as healthy.
        expression = f"1 - (({good_rate}) or vector(0)) / ({valid_rate})"

        return CompiledQuery(
            f"tier:{tier.name}:{span_name}", DIALECT,
            {
                "path": "/api/v1/query",
                "method": "POST",
                "params": {"query": expression},
                "alert_condition": f"{expression} >= {threshold:.10g}",
                "threshold": round(threshold, 10),
                "window_seconds": span,
                "notify": tier.notify,
                "service": "aps",
                "iam_action": "aps:QueryMetrics",
            },
            tuple(findings),
        )

    def _staleness_query(self, objective: Objective, valid: str) -> CompiledQuery:
        """Reports that the indicator stopped being measured.

        Separate from the tiers because it answers a different question, and
        required because no burn-rate expression can answer it: an objective
        whose denominator has vanished reports perfect health through every
        tier it has.
        """
        shortest = min(t.short_seconds for t in objective.tiers) if objective.tiers else 300
        expression = f"absent(({substitute_window(valid, shortest)}))"
        return CompiledQuery(
            "staleness", DIALECT,
            {
                "path": "/api/v1/query",
                "method": "POST",
                "params": {"query": expression},
                "alert_condition": f"{expression} == 1",
                "window_seconds": shortest,
                "service": "aps",
                "iam_action": "aps:QueryMetrics",
            },
            (note(
                "S308", f"{objective.where} :: staleness",
                f"no burn-rate tier can report a missing denominator, because every one of them "
                f"evaluates to nothing when the series is gone and an alerting rule over nothing "
                f"does not fire. This expression is the only part of the objective that "
                f"distinguishes a measured success from an unmeasured one; it reads the shortest "
                f"window any tier uses ({humanise(shortest)}) so it reports before the fastest "
                f"tier would have.",
            ),),
        )


def _as_total(query: str) -> str:
    """Turn a rate expression into a count over the window.

    A budget is a count, and a rate is not one. `rate` and `irate` are rewritten
    to `increase`, which is the same measurement integrated over the range;
    anything else is left exactly as written, because rewriting an expression
    whose shape is unknown is how a plausible wrong query reaches production.
    """
    return re.sub(r"\b(?:rate|irate)\s*\(", "increase(", query)


SOURCE = register(PrometheusSource())

if __name__ == "__main__":  # pragma: no cover - thin wrapper
    raise SystemExit(cli_main(SOURCE.name))
