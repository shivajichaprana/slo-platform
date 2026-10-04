#!/usr/bin/env python3
"""Compile an objective into CloudWatch requests, and report what is lost.

CloudWatch can answer "what proportion of events were good" two ways, and they
are not variants of one another -- they differ in how far back they reach, in
what an alarm may evaluate, and in whether the figure can be a single number.
Both are accepted here, each with the limits that decide where it may be used.

**Metric math over metric statistics** (`cloudwatch-metric-math`) reads stored
metrics at a period, so it reaches the full retention of the data and an alarm
may evaluate it over as much as seven days. Indicators are written as compact
metric references::

    AWS/ApplicationELB/RequestCount:Sum[LoadBalancer=app/checkout/0123456789abcdef]

**Metrics Insights** (`cloudwatch-metrics-insights`) reads a SQL statement, so
it can select across metrics without naming each one -- and it reaches two
weeks of history for a chart, but only the most recent three hours for an alarm
condition. A slow-burn tier therefore cannot be an alarm on a Metrics Insights
query at all, which is the single most consequential fact in this module.

Four findings here describe configurations CloudWatch accepts and reports on,
so none of them can be left to be noticed in production:

*The alarm cannot be given the aggregate it is quoted from.* The natural way to
write a burn-rate alert is to sum good events over the window, sum valid
events, and divide. Metric math will do that -- `SUM(m1)` over one time series
returns a scalar -- and CloudWatch's own guidance is not to put that in an
alarm, because an evaluating alarm retrieves more data points than its
evaluation periods ask for and a scalar aggregate "acts differently when extra
data is requested". So the window aggregate is compiled for the report and the
alarm is compiled as per-period arithmetic, and a scalar aggregate reaching an
alarm expression is refused rather than deployed.

*A total outage looks like missing data.* A period in which every request
failed leaves the success metric with no data point at all, so `good/valid` is
absent rather than zero -- and an alarm's `TreatMissingData` defaults to
`missing`, which holds the previous state. The alarm expression therefore fills
the numerator with zero, and because a fill cannot invent a series that was
never reported at all, every tier also carries the missing-data treatment that
makes the remaining case fail closed.

*A long window is a tumbling period, not a sliding one.* An alarm compares
period-aligned data points, so a one-hour long window is each clock hour and
not the trailing hour. A burn that starts at the half hour is split across two
periods and can breach neither, which is the one failure a multi-window policy
is built to catch.

*The period must suit the window's age, or part of the window returns nothing.*
One-minute data is kept for 15 days, five-minute data for 63 days, one-hour
data for 455. A 28-day budget asked for at one-minute resolution is answered
for the recent fortnight and silently unanswered before it -- a ratio over the
part that had data, labelled as the whole window.
"""

from __future__ import annotations

import re
from typing import Any

try:
    from .base import (
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
        register,
        warn,
    )
except ImportError:  # pragma: no cover - direct execution without the package
    from base import (  # type: ignore[no-redef]
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
        register,
        warn,
    )

DIALECT_MATH = "cloudwatch-metric-math"
DIALECT_INSIGHTS = "cloudwatch-metrics-insights"

# Retention, as published: data points of a given period are available for a
# given span and are aggregated upward afterwards. Read as "a window reaching
# further back than this needs at least that period".
RETENTION_SECONDS_BY_PERIOD = ((60, 15 * 86400), (300, 63 * 86400), (3600, 455 * 86400))

#: Default of the API when the caller omits it; also its maximum.
MAX_DATAPOINTS_PER_REQUEST = 100_800

#: An alarm's period multiplied by its evaluation periods.
MAX_ALARM_EVALUATION_SECONDS = 604_800
#: The tighter ceiling that applies when the period is under an hour.
MAX_ALARM_EVALUATION_SECONDS_SUB_HOURLY = 86_400
SUB_HOURLY_PERIOD_SECONDS = 3_600

#: History a Metrics Insights query reaches for a chart...
INSIGHTS_MAX_HISTORY_SECONDS = 14 * 86400
#: ...and for an alarm condition, which is the limit that matters here.
INSIGHTS_MAX_ALARM_RANGE_SECONDS = 3 * 3600
#: Metrics one statement will process, and series it will return.
INSIGHTS_MAX_METRICS = 10_000
INSIGHTS_MAX_SERIES = 500
#: Metrics Insights does not read high-resolution data; output is per minute.
INSIGHTS_RESOLUTION_SECONDS = 60

#: Statistics that count events. Everything else answers a different question.
COUNTING_STATISTICS = frozenset({"Sum", "SampleCount"})

#: Metric math functions that collapse a series to one value. Fine in a report,
#: refused in an alarm expression for the reason in the module docstring.
SCALAR_FUNCTIONS = ("AVG", "MAX", "MIN", "STDDEV", "SUM", "PERIOD", "METRIC_COUNT", "DATAPOINT_COUNT")

#: Documented maximum dimensions on one metric.
MAX_DIMENSIONS = 30

_REFERENCE = re.compile(
    r"""^
    (?P<path>[^:\[\]]+)          # namespace and metric name, slash separated
    :(?P<stat>[A-Za-z0-9.]+)     # statistic
    (?:\[(?P<dims>[^\[\]]*)\])?  # optional dimensions
    $""",
    re.VERBOSE,
)
_SELECT = re.compile(r"^\s*SELECT\s", re.IGNORECASE)
_PROMQL_TELLS = (
    re.compile(r"\brate\s*\(", re.IGNORECASE),
    re.compile(r"\bincrease\s*\(", re.IGNORECASE),
    re.compile(r"\bsum_over_time\s*\(", re.IGNORECASE),
    re.compile(r"\[[0-9]+[smhdwy]\]"),
    re.compile(r"\{[a-zA-Z_][a-zA-Z0-9_]*\s*(=|!=|=~|!~)"),
)


class MetricReferenceError(ValueError):
    """A compact metric reference that cannot be read."""


def parse_metric_reference(text: str) -> dict[str, Any]:
    """Read `Namespace/MetricName:Statistic[Dim=Value,...]` into a MetricStat.

    The grammar exists because the specification's query fields are strings and
    a metric identity is a structure. It is deliberately not a general
    expression language: anything richer belongs in a Metrics Insights
    statement, where it is the backend's own syntax rather than this
    repository's invention.
    """
    match = _REFERENCE.match(text.strip())
    if not match:
        raise MetricReferenceError(
            f"{text!r} is not a metric reference. Expected "
            f"'Namespace/MetricName:Statistic' with optional '[Dim=Value,...]'."
        )

    path = match.group("path").strip()
    if "/" not in path:
        raise MetricReferenceError(
            f"{text!r} names no namespace: expected 'Namespace/MetricName', and a namespace is "
            f"required because a metric name alone is not unique."
        )
    namespace, _, metric_name = path.rpartition("/")
    namespace, metric_name = namespace.strip(), metric_name.strip()
    if not namespace or not metric_name:
        raise MetricReferenceError(f"{text!r} has an empty namespace or metric name")

    statistic = match.group("stat")
    dimensions: list[dict[str, str]] = []
    raw_dims = match.group("dims")
    if raw_dims is not None and raw_dims.strip():
        for part in raw_dims.split(","):
            if "=" not in part:
                raise MetricReferenceError(
                    f"dimension {part.strip()!r} in {text!r} is not 'Name=Value'"
                )
            key, _, value = part.partition("=")
            key, value = key.strip(), value.strip()
            if not key or not value:
                raise MetricReferenceError(
                    f"dimension {part.strip()!r} in {text!r} has an empty name or value"
                )
            dimensions.append({"Name": key, "Value": value})

    return {
        "Namespace": namespace,
        "MetricName": metric_name,
        "Statistic": statistic,
        "Dimensions": dimensions,
    }


def minimum_period_for(span_seconds: int) -> int:
    """The finest period that still has data as far back as `span_seconds`.

    Asking for a finer one is accepted by the API and answered only for the
    part of the range that still holds data at that resolution, which is the
    failure this function exists to prevent.
    """
    for period, retained in RETENTION_SECONDS_BY_PERIOD:
        if span_seconds <= retained:
            return period
    return RETENTION_SECONDS_BY_PERIOD[-1][0]


def scalar_functions_in(expression: str) -> list[str]:
    """Scalar-collapsing metric math functions used in an expression."""
    found = []
    for name in SCALAR_FUNCTIONS:
        if re.search(rf"\b{name}\s*\(", expression):
            found.append(name)
    return found


class CloudWatchSource(SliSource):
    """CloudWatch metric math and Metrics Insights."""

    name = "cloudwatch"
    dialects = (DIALECT_INSIGHTS, DIALECT_MATH)

    def recognise(self, sli: dict[str, Any]) -> Recognition:
        queries = [q for q in (sli.get("valid_query"), sli.get("good_query")) if q]
        if not queries:
            return Recognition(None, False, ("the indicator carries no query to read",))

        promql = [p.pattern for q in queries for p in _PROMQL_TELLS if p.search(q)]
        if promql:
            return Recognition(
                None, False,
                (f"the query uses PromQL constructs ({len(promql)} match(es)), "
                 f"which CloudWatch does not evaluate",),
            )
        if all(_SELECT.match(q) for q in queries):
            return Recognition(DIALECT_INSIGHTS, True, ("every query is a SELECT statement",))
        if any(_SELECT.match(q) for q in queries):
            return Recognition(
                None, False,
                ("one query is a SELECT statement and the other is not; a ratio cannot mix a "
                 "Metrics Insights statement with a metric reference, because the two are "
                 "evaluated by different engines with different reach",),
            )
        for query in queries:
            try:
                parse_metric_reference(query)
            except MetricReferenceError as exc:
                return Recognition(None, False, (str(exc),))
        return Recognition(DIALECT_MATH, True, ("every query is a metric reference",))

    def compile(self, objective: Objective) -> CompiledObjective:
        recognition = self.recognise(objective.sli)
        if not recognition.claimed:
            return self._foreign(objective, recognition)

        result = CompiledObjective(
            source=self.name, key=objective.key, where=objective.where,
        )

        if objective.sli["kind"] == "threshold":
            # Not a gap in this adapter: CloudWatch has no per-event comparison
            # to offer. Metric math compares period aggregates, so `IF(m1 <
            # 300, 1, 0)` counts periods whose average was fast, not requests
            # that were fast. The two move together, differ by an amount that
            # depends on the shape of the distribution, and are never
            # distinguished by anything downstream -- so the figure would be
            # wrong in a way nothing reports. A percentile statistic is not an
            # answer either: a percentile is not a proportion and no budget can
            # be computed from one, which is why the specification has no
            # percentile kind.
            result.findings.append(error(
                "S305", objective.where,
                "a threshold indicator cannot be compiled for CloudWatch. Metric math compares "
                "period AGGREGATES, not events, so a comparison against the threshold would count "
                "the periods whose aggregate was good rather than the events that were good -- a "
                "different quantity that tracks the right one closely enough never to be "
                "questioned. Express the objective as a ratio against a metric that already counts "
                "good events, or measure it where per-event comparison exists.",
            ))
            return result

        if recognition.dialect == DIALECT_INSIGHTS:
            self._compile_insights(objective, result)
        else:
            self._compile_metric_math(objective, result)
        return result

    # -- metric math --------------------------------------------------------

    def _compile_metric_math(self, objective: Objective, result: CompiledObjective) -> None:
        try:
            good = parse_metric_reference(objective.sli["good_query"])
            valid = parse_metric_reference(objective.sli["valid_query"])
        except MetricReferenceError as exc:
            result.findings.append(error("S101", objective.where, str(exc)))
            return

        for label, metric in (("good_query", good), ("valid_query", valid)):
            if metric["Statistic"] not in COUNTING_STATISTICS:
                result.findings.append(error(
                    "S303", objective.where,
                    f"{label} uses the {metric['Statistic']!r} statistic, which does not count "
                    f"events. An indicator is a count over a count, and an average or a maximum "
                    f"aggregated over a window is neither -- summing per-period averages produces a "
                    f"number with no unit that still rises and falls plausibly. Use "
                    f"{' or '.join(sorted(COUNTING_STATISTICS))}.",
                ))
            if len(metric["Dimensions"]) > MAX_DIMENSIONS:
                result.findings.append(error(
                    "S102", objective.where,
                    f"{label} names {len(metric['Dimensions'])} dimensions; a metric is identified "
                    f"by at most {MAX_DIMENSIONS}.",
                ))
        if result.errors:
            return

        result.findings.append(note(
            "S302", objective.where,
            "a period in which every event failed leaves the good-event metric with no data point, "
            "so the raw ratio is absent rather than zero, and an alarm treats missing data by "
            "holding its previous state unless told otherwise. Every tier expression therefore "
            "fills its numerator with zero, and because a fill cannot create a series that was "
            "never reported at all, every alarm is also given a breaching treatment for missing "
            "data so the remaining case fails closed.",
        ))
        result.budget = self._budget_query(objective, good, valid, result)
        for tier in objective.tiers:
            for span_name, span in (("long", tier.long_seconds), ("short", tier.short_seconds)):
                result.tiers.append(self._tier_query(objective, tier, span_name, span, good, valid))

    def _budget_query(
        self, objective: Objective, good: dict[str, Any], valid: dict[str, Any],
        result: CompiledObjective,
    ) -> CompiledQuery:
        """The whole-window figure, as one number, for a report to quote."""
        window = objective.window.seconds
        period = minimum_period_for(window)
        findings: list[Finding] = []

        requested = 60
        if period > requested:
            findings.append(note(
                "S200", objective.where,
                f"the budget window reaches back {humanise(window)}, so the period is {period}s "
                f"rather than {requested}s: one-minute data is kept for 15 days and five-minute "
                f"data for 63, and a finer period is accepted for an older range and answered only "
                f"for the part that still holds data at that resolution. The figure would then "
                f"cover the recent part of the window and be labelled as the whole of it.",
            ))

        # Three series are returned rather than one. The ratio alone is a bare
        # number nobody can argue with; with its two components beside it the
        # figure can be re-derived, which matters because this is the number a
        # policy decision is taken from.
        points_per_series = -(-window // period)
        if points_per_series * 3 > MAX_DATAPOINTS_PER_REQUEST:
            findings.append(warn(
                "S201", objective.where,
                f"the component series hold about {points_per_series:,} points each and a request "
                f"returns at most {MAX_DATAPOINTS_PER_REQUEST:,}; the window must be paginated by "
                f"timestamp rather than asked for at once.",
            ))

        if objective.window.nominal:
            findings.append(note(
                "S204", objective.where,
                f"the window is a calendar {objective.window.label}, which is not a duration, so "
                f"{humanise(window)} is nominal. The request is a fixed span; a figure reported "
                f"against the real period boundary has to be asked for between the boundaries "
                f"themselves.",
            ))

        payload = {
            "MetricDataQueries": [
                {"Id": "good", "ReturnData": True,
                 "MetricStat": {"Metric": _metric_block(good), "Period": period,
                                "Stat": good["Statistic"]}},
                {"Id": "valid", "ReturnData": True,
                 "MetricStat": {"Metric": _metric_block(valid), "Period": period,
                                "Stat": valid["Statistic"]}},
                # SUM over one time series returns a scalar: the window total.
                # Gaps are not filled here and must not be -- a total is the sum
                # of what was reported, and filling absent periods with zero
                # would add zeros to both halves of a ratio and flatter it.
                {"Id": "ratio", "ReturnData": True, "Label": f"{objective.key} good proportion",
                 "Expression": "SUM(good)/SUM(valid)"},
            ],
            "ScanBy": "TimestampDescending",
            "MaxDatapoints": MAX_DATAPOINTS_PER_REQUEST,
        }
        return CompiledQuery("budget", DIALECT_MATH, payload, tuple(findings))

    def _tier_query(
        self, objective: Objective, tier: Any, span_name: str, span: int,
        good: dict[str, Any], valid: dict[str, Any],
    ) -> CompiledQuery:
        """One side of a burn-rate tier, as an alarm would evaluate it."""
        where = f"{objective.where} :: tier {tier.name} ({span_name})"
        findings: list[Finding] = []
        threshold = tier.fires_at_error_rate(objective.allowed)

        period = span
        evaluation_periods = 1
        total = period * evaluation_periods

        if period % 60:
            findings.append(error(
                "S402", where,
                f"an alarm period must be 10, 20, 30 or a multiple of 60 seconds; {period}s is not.",
            ))
        if total > MAX_ALARM_EVALUATION_SECONDS:
            findings.append(error(
                "S400", where,
                f"the {span_name} window is {humanise(span)}, and an alarm's period multiplied by "
                f"its evaluation periods cannot exceed {MAX_ALARM_EVALUATION_SECONDS:,}s "
                f"({humanise(MAX_ALARM_EVALUATION_SECONDS)}). This tier cannot be a single alarm.",
            ))
        elif period < SUB_HOURLY_PERIOD_SECONDS and total > MAX_ALARM_EVALUATION_SECONDS_SUB_HOURLY:
            findings.append(error(
                "S400", where,
                f"a period under an hour caps the total evaluation span at "
                f"{MAX_ALARM_EVALUATION_SECONDS_SUB_HOURLY:,}s; this one asks for {total:,}s.",
            ))

        retention_period = minimum_period_for(span)
        if retention_period > period:
            findings.append(error(
                "S200", where,
                f"the {span_name} window reaches back {humanise(span)}, where data is only retained "
                f"at {retention_period}s resolution, but the alarm would read it at {period}s. The "
                f"alarm would evaluate a metric with no data points and sit in insufficient-data "
                f"rather than alerting.",
            ))

        if span_name == "long":
            findings.append(warn(
                "S304", where,
                f"an alarm compares period-aligned data points, so this window is the tumbling "
                f"{humanise(span)} rather than the trailing one. A burn beginning mid-period is "
                f"split across two periods and can breach neither, which is exactly the case the "
                f"short window is paired with it to catch -- and the short window is aligned the "
                f"same way. Evaluating {humanise(span)} as N shorter periods with all of them "
                f"required restores the sliding behaviour, at the cost of refusing to fire unless "
                f"every sub-period breaches, which is a stricter condition than the aggregate.",
            ))

        expression = "1-FILL(good,0)/valid"
        offending = scalar_functions_in(expression)
        if offending:
            # Unreachable for the expression above, and checked anyway: this is
            # the guard that matters if the expression is ever edited, because
            # the published guidance is not to use a scalar-returning function
            # in an alarm -- an evaluating alarm requests more data points than
            # its evaluation periods, and a scalar aggregate behaves
            # differently when it is given them.
            findings.append(error(
                "S301", where,
                f"the alarm expression uses {', '.join(offending)}, which collapses a series to a "
                f"scalar. An evaluating alarm retrieves more data points than its evaluation "
                f"periods ask for, and a scalar aggregate does not answer the same way when it is "
                f"given them, so the alarm would compare a figure computed over a span nobody "
                f"chose. Scalar aggregates belong in the report, not in the alarm.",
            ))

        payload = {
            "AlarmDescription": (
                f"{objective.key} burn-rate tier {tier.name} ({span_name} window): error rate over "
                f"{humanise(span)} at or above {threshold:.4%} "
                f"(burn rate {tier.burn_rate:g} x a {objective.allowed:.3%} budget)"
            ),
            "ComparisonOperator": "GreaterThanOrEqualToThreshold",
            "Threshold": round(threshold, 10),
            "EvaluationPeriods": evaluation_periods,
            "DatapointsToAlarm": evaluation_periods,
            "TreatMissingData": "breaching",
            "Metrics": [
                {"Id": "good", "ReturnData": False,
                 "MetricStat": {"Metric": _metric_block(good), "Period": period,
                                "Stat": good["Statistic"]}},
                {"Id": "valid", "ReturnData": False,
                 "MetricStat": {"Metric": _metric_block(valid), "Period": period,
                                "Stat": valid["Statistic"]}},
                {"Id": "error_rate", "ReturnData": True, "Expression": expression,
                 "Label": f"{objective.key} error rate over {humanise(span)}"},
            ],
        }
        return CompiledQuery(f"tier:{tier.name}:{span_name}", DIALECT_MATH, payload, tuple(findings))

    # -- Metrics Insights ---------------------------------------------------

    def _compile_insights(self, objective: Objective, result: CompiledObjective) -> None:
        good_sql = objective.sli["good_query"].strip()
        valid_sql = objective.sli["valid_query"].strip()

        for label, sql in (("good_query", good_sql), ("valid_query", valid_sql)):
            if re.search(r"\bGROUP\s+BY\b", sql, re.IGNORECASE):
                result.findings.append(error(
                    "S102", objective.where,
                    f"{label} carries a GROUP BY, so the statement returns one series per group. A "
                    f"ratio over an unknown number of series is undefined, and the grouped result "
                    f"cannot be referenced by a math expression. Aggregate the statement, or hold "
                    f"one objective per group.",
                ))
            if not re.search(r"\bSELECT\s+(SUM|COUNT)\s*\(", sql, re.IGNORECASE):
                result.findings.append(error(
                    "S303", objective.where,
                    f"{label} does not select SUM or COUNT. An indicator is a count over a count; "
                    f"an AVG or MAX selection answers a different question in the same shape.",
                ))
        if result.errors:
            return

        window = objective.window.seconds
        budget_findings: list[Finding] = []
        if window > INSIGHTS_MAX_HISTORY_SECONDS:
            budget_findings.append(error(
                "S202", objective.where,
                f"the budget window is {humanise(window)} and a Metrics Insights statement reaches "
                f"{humanise(INSIGHTS_MAX_HISTORY_SECONDS)} of history. The window cannot be "
                f"evaluated this way at all -- not partially, and not by paginating, because the "
                f"older part of the range is outside what the engine will read. Express the "
                f"indicator as metric references, which reach the full retention of the data.",
            ))
        budget_findings.append(note(
            "S203", objective.where,
            f"Metrics Insights does not read high-resolution data: output is aggregated to "
            f"{INSIGHTS_RESOLUTION_SECONDS}s, so a sub-minute publication rate is summarised before "
            f"the ratio is taken.",
        ))
        budget_findings.append(note(
            "S402", objective.where,
            f"one statement processes at most {INSIGHTS_MAX_METRICS:,} metrics and returns at most "
            f"{INSIGHTS_MAX_SERIES} series. Past the first of those the statement still succeeds and "
            f"answers from the metrics it did match, so a selection that grows past the limit "
            f"reports a shrinking proportion of reality with no error.",
        ))

        result.budget = CompiledQuery(
            "budget", DIALECT_INSIGHTS,
            {
                "MetricDataQueries": [
                    {"Id": "good", "ReturnData": True, "Expression": good_sql,
                     "Period": INSIGHTS_RESOLUTION_SECONDS},
                    {"Id": "valid", "ReturnData": True, "Expression": valid_sql,
                     "Period": INSIGHTS_RESOLUTION_SECONDS},
                ],
                "ScanBy": "TimestampDescending",
                "MaxDatapoints": MAX_DATAPOINTS_PER_REQUEST,
                "Note": "the ratio is taken by the caller: a math expression cannot reference a "
                        "statement whose series count is not known in advance",
            },
            tuple(budget_findings),
        )

        for tier in objective.tiers:
            for span_name, span in (("long", tier.long_seconds), ("short", tier.short_seconds)):
                where = f"{objective.where} :: tier {tier.name} ({span_name})"
                findings: list[Finding] = []
                if span > INSIGHTS_MAX_ALARM_RANGE_SECONDS:
                    findings.append(error(
                        "S401", where,
                        f"an alarm on a Metrics Insights query evaluates only the most recent "
                        f"{humanise(INSIGHTS_MAX_ALARM_RANGE_SECONDS)} of data, and this "
                        f"{span_name} window is {humanise(span)}. The alarm can be created and it "
                        f"can never see the span its threshold was computed for. Any tier slower "
                        f"than {humanise(INSIGHTS_MAX_ALARM_RANGE_SECONDS)} has to be built on "
                        f"metric references instead, which is why the two dialects are not "
                        f"alternatives for the same objective.",
                    ))
                result.tiers.append(CompiledQuery(
                    f"tier:{tier.name}:{span_name}", DIALECT_INSIGHTS,
                    {
                        "ComparisonOperator": "GreaterThanOrEqualToThreshold",
                        "Threshold": round(tier.fires_at_error_rate(objective.allowed), 10),
                        "EvaluationPeriods": 1,
                        "TreatMissingData": "breaching",
                        "Period": span,
                        "Expressions": {"good": good_sql, "valid": valid_sql},
                    },
                    tuple(findings),
                ))


def _metric_block(metric: dict[str, Any]) -> dict[str, Any]:
    block: dict[str, Any] = {"Namespace": metric["Namespace"], "MetricName": metric["MetricName"]}
    if metric["Dimensions"]:
        block["Dimensions"] = metric["Dimensions"]
    return block


SOURCE = register(CloudWatchSource())

if __name__ == "__main__":  # pragma: no cover - thin wrapper
    raise SystemExit(cli_main(SOURCE.name))
