#!/usr/bin/env python3
"""Render a burn-rate alert policy into the artifacts a backend will accept.

The arithmetic lives in `burn_rate.py` and the queries come from a source
adapter in `sources/`. This module is the last step: it puts those two together
and writes out something deployable. Three targets, and they are not variations
on one shape.

**Prometheus rules are SERIALISED, not templated.** A rule file's payload is
PromQL -- braces, quoted label values, comparison operators, `or vector(0)` --
and a text template that interpolates one is how a rule file becomes either
invalid or, worse, valid and subtly different. The structure is therefore built
as data and written by a YAML serialiser, which is correct by construction, and
the template contributes only the comment header a serialiser cannot emit.

**CloudWatch alarms are TEMPLATED, because there is no HCL serialiser here.**
So the escaping is explicit instead: every interpolated value passes through
`hcl_string`, which escapes the quote, the backslash and -- the one that
actually bites -- the `${` and `%{` that begin a Terraform interpolation. An
alarm description containing `${` is otherwise read as a reference to something
that does not exist, and the configuration fails to parse at a line nobody
wrote. Terraform interpolations that are MEANT to be interpolations are written
by the template, never by a value, which is what keeps the two
distinguishable. Templates use `@{...}` placeholders for the same reason: `$`
belongs to HCL in these files.

**A tier is one Prometheus rule and three CloudWatch resources.** A rule's
expression can join the long and the short window with `and`; an alarm
evaluates a single period, so the two windows have to be two alarms with a
composite above them requiring both. The children have their actions disabled,
because a tier that notifies three times for one condition is how a policy
designed to reduce paging increases it.

Finding codes:

====  ==========================================================================
G4xx  Rendering: what a target cannot carry, and what it carries differently.
====  ==========================================================================
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from string import Template
from typing import Any, Iterable

_ROOT = Path(__file__).resolve().parent.parent
for _extra in (_ROOT / "sources", _ROOT / "generator"):
    if str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

from base import (  # noqa: E402  (path set above)
    CompiledObjective,
    Finding,
    Objective,
    discover,
    error,
    get_source,
    humanise,
    load_document,
    note,
    objectives_in,
    warn,
)
from burn_rate import (  # noqa: E402  (path set above)
    DESCRIPTION_CEILING,
    NAME_CEILING,
    AlertPlan,
    TierPlan,
    format_rate,
    plan_objective,
)

# Imported for the registration each module performs on import: a source is
# selectable only once something has imported it, and the alternative --
# importing by name from a string -- would turn an unknown source into an
# ImportError rather than into the message `get_source` already writes.
import cloudwatch  # noqa: E402  (imported for its registration side effect)
import prometheus  # noqa: E402  (imported for its registration side effect)

#: The sources those imports registered. Named rather than left implicit so the
#: reason the imports exist is checkable: a linter removing an unused import
#: would otherwise turn a selectable source into an unknown one.
REGISTERED_SOURCES = (cloudwatch.SOURCE.name, prometheus.SOURCE.name)

try:
    import yaml
except ImportError:  # pragma: no cover - reported, not raised
    print("error: PyYAML is required (pip install pyyaml)", file=sys.stderr)
    raise SystemExit(2)

LOG = logging.getLogger("slo.generator.render")


class BlockScalar(str):
    """A string the serialiser must write as a literal block.

    A burn-rate expression is two conditions and an operator, and a rule
    file is read by whoever is deciding whether the alert was right. Left to
    its defaults the serialiser emits a multi-line string as one quoted line
    with escaped newlines, which is valid, identical in meaning, and not
    reviewable -- so the one property worth having is forced here.
    """


def _block_scalar_representer(dumper: Any, data: Any) -> Any:
    return dumper.represent_scalar("tag:yaml.org,2002:str", str(data), style="|")


yaml.add_representer(BlockScalar, _block_scalar_representer, Dumper=yaml.SafeDumper)


TEMPLATE_DIR = _ROOT / "templates"

#: Longest `AlarmRule` a composite alarm accepts. A rule that interpolates two
#: alarm names is nowhere near it; the check exists because the rule grows with
#: the number of children if a future policy joins more than two windows.
ALARM_RULE_CEILING = 10_240

#: How long a rule's condition must hold before it fires. One evaluation
#: interval, deliberately NOT the short window: the windows already provide the
#: smoothing, and a `for` on top of them is a second, undocumented delay added
#: to every figure in the generated report.
DEFAULT_FOR_SECONDS = 60

#: A Prometheus alert name must be a valid metric name, which excludes the
#: hyphen the specification's identifiers are built from. The substitution is
#: reversible because the schema's patterns exclude the character it maps to.
_PROM_NAME_SAFE = re.compile(r"[^A-Za-z0-9_:]")

#: Characters HCL gives meaning to inside a quoted string.
_HCL_ESCAPES = (("\\", "\\\\"), ('"', '\\"'), ("${", "$${"), ("%{", "%%{"))

TARGETS = ("prometheus", "cloudwatch", "report")

#: Which source dialect each target can be rendered from. A target handed a
#: plan compiled by the other source is refused rather than approximated: the
#: payloads are not the same shape and there is no translation between them
#: that preserves the arithmetic.
TARGET_SOURCE = {"prometheus": "prometheus", "cloudwatch": "cloudwatch", "report": None}


class BlockTemplate(Template):
    """`@{name}` placeholders, so `${...}` stays available to HCL."""

    delimiter = "@"


def hcl_string(value: Any) -> str:
    """Escape a value for interpolation into a quoted HCL string."""
    text = str(value)
    for needle, replacement in _HCL_ESCAPES:
        text = text.replace(needle, replacement)
    return text


def comment_text(value: Any, limit: int = 300) -> str:
    """Flatten a value for interpolation into a single-line comment.

    Author-written prose from the specification -- a title, an owner -- is
    interpolated into the comment header of every generated file. A quote or a
    `${` is harmless in a comment, but a NEWLINE is not: it ends the comment and
    puts whatever follows into the file as code, in a language the surrounding
    lines are not. The schema bounds a title's length and says nothing about its
    content, so the flattening happens here rather than being assumed there.
    """
    text = " ".join(str(value).split())
    return text[: limit - 1] + "\u2026" if len(text) > limit else text


def prometheus_alert_name(*parts: str) -> str:
    """Compose a legal Prometheus alert name from specification identifiers."""
    joined = "_".join(part for part in parts if part)
    return _PROM_NAME_SAFE.sub("_", joined)


def terraform_label(*parts: str) -> str:
    return "_".join(part for part in parts if part).replace("-", "_")


def load_template(name: str) -> BlockTemplate:
    path = TEMPLATE_DIR / name
    try:
        return BlockTemplate(path.read_text(encoding="utf-8"))
    except OSError as exc:
        print(f"error: cannot read template {path}: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def _threshold_literal(value: float) -> str:
    """A threshold as a decimal literal, never in exponent form.

    `7.2e-05` is legal HCL and legal JSON. It is also the form in which a
    reviewer stops reading a number, and a threshold is the one figure in a
    generated alarm that has to be checked by eye.
    """
    return f"{value:.10f}".rstrip("0").rstrip(".") or "0"


@dataclass
class Artifact:
    """One rendered file, and what rendering it cost."""

    target: str
    filename: str
    content: str
    identities: tuple[str, ...]
    findings: tuple[Finding, ...] = ()


# -- shared checks ----------------------------------------------------------


def _identity_findings(where: str, target: str, names: Iterable[str]) -> list[Finding]:
    """Both of these are unreachable from a schema-legal document, deliberately.

    The widest identity the schema permits is a 48-character service, a
    32-character objective name and a 16-character tier name, which composes to
    just over a hundred characters against a ceiling of 255; and a repeated
    identity needs two tiers of one name, which `G200` has already refused
    before anything is rendered. They are checked anyway, because what makes
    them unreachable is a set of patterns in another file and a check in a
    third: the guard is here for the edit that widens one of them, and it is
    exercised directly rather than through a document.
    """
    findings: list[Finding] = []
    ceiling = NAME_CEILING[target] if target in NAME_CEILING else NAME_CEILING["cloudwatch"]
    seen: set[str] = set()
    for name in names:
        if len(name) > ceiling:
            findings.append(error(
                "G400", where,
                f"the {target} identity {name!r} is {len(name)} characters against a ceiling of "
                f"{ceiling}. It is composed from the deployment prefix, the service, the "
                f"objective and the tier, so the overflow is invisible to whoever set any one of "
                f"them and surfaces when the resource is created.",
            ))
        if name in seen:
            findings.append(error(
                "G401", where,
                f"the {target} identity {name!r} is produced twice. On this target the second "
                f"definition replaces the first, so the generated configuration would be short "
                f"one alert with nothing reporting it.",
            ))
        seen.add(name)
    return findings


def _tier_queries(compiled: CompiledObjective, tier_name: str) -> dict[str, dict[str, Any]]:
    """The long and short payloads a source compiled for one tier."""
    out: dict[str, dict[str, Any]] = {}
    for query in compiled.tiers:
        parts = query.purpose.split(":")
        if len(parts) == 3 and parts[0] == "tier" and parts[1] == tier_name:
            out[parts[2]] = query.payload
    return out


def _annotations(objective: Objective, plan: AlertPlan, tier: TierPlan) -> dict[str, str]:
    """What a responder needs in order to decide whether to act.

    The budget figures are here rather than in a dashboard because they are the
    part of an alert that cannot be looked up afterwards: by the time anyone
    reads it, the number that mattered is the one at the moment it fired.
    """
    detection = tier.detection_seconds_at(1.0) or 0.0
    annotations = {
        "summary": (
            f"{objective.service}/{objective.name} is burning its error budget at "
            f"{tier.tier.burn_rate:g}x ({tier.name} tier)"
        ),
        "description": (
            f"The error rate over {humanise(tier.tier.long_seconds)} has reached "
            f"{format_rate(tier.threshold)} and the last "
            f"{humanise(tier.tier.short_seconds)} agrees, so the burn is current rather than "
            f"historical. About {tier.budget_fraction_at_detection:.1%} of the "
            f"{format_rate(plan.budget.allowed)} budget for this "
            f"{humanise(plan.budget.window_seconds)} window is already spent at this point, "
            f"whatever the severity of the incident. A total outage reaches this threshold in "
            f"{humanise(detection)}; one sitting on the threshold takes the full "
            f"{humanise(tier.tier.long_seconds)}."
        ),
        "objective": objective.title or objective.name,
        "budget_window": f"{humanise(plan.budget.window_seconds)} "
                         f"({objective.window.kind}"
                         f"{', nominal' if plan.budget.nominal else ''})",
        "budget_spent_at_detection": f"{tier.budget_fraction_at_detection:.1%}",
        "blind_below": humanise(tier.min_detectable_outage_seconds),
    }
    if objective.sli.get("blind_spots"):
        annotations["indicator_blind_spots"] = " | ".join(objective.sli["blind_spots"])
    return annotations


def _labels(objective: Objective, plan: AlertPlan, tier: TierPlan) -> dict[str, str]:
    return {
        "severity": tier.tier.notify,
        "service": objective.service,
        "slo": objective.name,
        "slo_tier": tier.name,
        "burn_rate": f"{tier.tier.burn_rate:g}",
        "service_tier": objective.tier_policy or "unspecified",
    }


def sliding_window_caveat(where: str) -> Finding:
    """The one place the generator's arithmetic does not describe its own output.

    Every detection figure in `burn_rate.py` is derived for a sliding window:
    the trailing average over the long window rises continuously, so the
    condition is met at `threshold x long_window / error_rate`. A CloudWatch
    alarm does not evaluate a sliding window. It compares period-aligned data
    points, so the earliest it can fire is the end of the period in which the
    breach occurred, and where the incident started inside that period decides
    how much later than the computed figure that is. Reported rather than
    corrected, because the correction is not a factor: it depends on the
    incident's phase relative to the period boundary, which is not knowable in
    advance.
    """
    return warn(
        "G411", where,
        "the detection and budget figures generated for this objective are derived for a sliding "
        "window, and an alarm period is tumbling. On this target the alarm cannot fire before the "
        "end of the period in which the breach happened, so the real detection time is between "
        "the computed figure and a full long window depending on when the incident began, and the "
        "share of the budget spent at detection is correspondingly a LOWER bound rather than an "
        "equality. The arithmetic is exact for a query engine evaluating a trailing range and is "
        "an optimistic bound here.",
    )


# -- Prometheus -------------------------------------------------------------


def render_prometheus(
    objective: Objective, plan: AlertPlan, compiled: CompiledObjective,
) -> Artifact:
    where = objective.where
    findings: list[Finding] = []
    rules: list[dict[str, Any]] = []
    identities: list[str] = []

    findings.append(note(
        "G402", where,
        f"each rule's condition must hold for {humanise(DEFAULT_FOR_SECONDS)} before it fires. "
        f"That is one evaluation interval and deliberately not the short window: the two windows "
        f"already smooth the signal, and a `for` on top of them adds a delay to every detection "
        f"figure in the generated report without appearing in any of them.",
    ))
    findings.append(note(
        "G403", where,
        "the two sides of the `and` must produce the same label set or the operator matches "
        "nothing and the rule never fires. Both sides here are derived from one indicator and "
        "aggregate identically, so they agree by construction -- but an indicator query edited to "
        "aggregate `by` something on one side only would break the rule silently, because an "
        "expression that matches nothing is not an error.",
    ))

    for tier in plan.tiers:
        queries = _tier_queries(compiled, tier.name)
        if "long" not in queries or "short" not in queries:
            findings.append(error(
                "G404", f"{where} :: tier {tier.name}",
                f"the source compiled "
                f"{', '.join(sorted(queries)) or 'nothing'} for this tier, and a rule needs both "
                f"the long and the short window. Nothing is rendered for it: half a multi-window "
                f"condition is a single-window alert with a multi-window threshold.",
            ))
            continue

        long_expr = queries["long"]["params"]["query"]
        short_expr = queries["short"]["params"]["query"]
        threshold = _threshold_literal(tier.threshold)
        name = prometheus_alert_name(
            "slo_burn", objective.service, objective.name, tier.name,
        )
        identities.append(name)
        rules.append({
            "alert": name,
            "expr": BlockScalar(
                f"({long_expr}) >= {threshold}\n"
                f"and\n"
                f"({short_expr}) >= {threshold}\n"
            ),
            "for": f"{DEFAULT_FOR_SECONDS // 60}m",
            "labels": _labels(objective, plan, tier),
            "annotations": _annotations(objective, plan, tier),
        })

    staleness = next((q for q in compiled.tiers if q.purpose == "staleness"), None)
    if staleness is not None:
        name = prometheus_alert_name("slo_indicator_stale", objective.service, objective.name)
        identities.append(name)
        window = staleness.payload["window_seconds"]
        rules.append({
            "alert": name,
            "expr": staleness.payload["params"]["query"],
            "for": f"{max(1, window // 60)}m",
            "labels": {
                "severity": "page" if any(t.tier.notify == "page" for t in plan.tiers) else "ticket",
                "service": objective.service,
                "slo": objective.name,
                "slo_tier": "staleness",
            },
            "annotations": {
                "summary": f"{objective.service}/{objective.name} is no longer being measured",
                "description": (
                    "The indicator's denominator has no samples. Every burn-rate rule for this "
                    "objective now evaluates to nothing, and a rule over nothing does not fire, so "
                    "this is the only alert that can report the condition. The objective is not "
                    "being met -- it is not being measured."
                ),
            },
        })
    else:
        findings.append(warn(
            "G405", where,
            "the source compiled no staleness expression, so nothing in the rendered rules "
            "distinguishes an objective that is being met from one that stopped being measured.",
        ))

    findings.extend(_identity_findings(where, "prometheus", identities))

    group = {
        "groups": [{
            "name": f"slo-{objective.service}-{objective.name}",
            "interval": f"{DEFAULT_FOR_SECONDS}s",
            "rules": rules,
        }]
    }
    header = load_template("prometheus-rules.header.yaml.tmpl").substitute(
        _header_values(objective, plan),
    )
    body = yaml.safe_dump(group, sort_keys=False, default_flow_style=False, width=100)
    return Artifact(
        target="prometheus",
        filename=f"{objective.service}-{objective.name}.rules.yaml",
        content=f"{header}\n{body}",
        identities=tuple(identities),
        findings=tuple(findings),
    )


# -- CloudWatch -------------------------------------------------------------


def _metric_query_blocks(metrics: list[dict[str, Any]]) -> str:
    """Render a compiled `Metrics` list as `metric_query` blocks."""
    blocks: list[str] = []
    for entry in metrics:
        lines = ["  metric_query {", f'    id          = "{hcl_string(entry["Id"])}"']
        lines.append(f"    return_data = {str(bool(entry.get('ReturnData'))).lower()}")
        if entry.get("Label"):
            lines.append(f'    label       = "{hcl_string(entry["Label"])}"')
        if entry.get("Expression"):
            lines.append(f'    expression  = "{hcl_string(entry["Expression"])}"')
        stat = entry.get("MetricStat")
        if stat:
            metric = stat["Metric"]
            lines.append("")
            lines.append("    metric {")
            lines.append(f'      namespace   = "{hcl_string(metric["Namespace"])}"')
            lines.append(f'      metric_name = "{hcl_string(metric["MetricName"])}"')
            lines.append(f'      period      = {int(stat["Period"])}')
            lines.append(f'      stat        = "{hcl_string(stat["Stat"])}"')
            dimensions = metric.get("Dimensions") or []
            if dimensions:
                lines.append("")
                lines.append("      dimensions = {")
                for dimension in dimensions:
                    key = hcl_string(dimension["Name"])
                    value = hcl_string(dimension["Value"])
                    lines.append(f'        "{key}" = "{value}"')
                lines.append("      }")
            lines.append("    }")
        lines.append("  }")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def render_cloudwatch(
    objective: Objective, plan: AlertPlan, compiled: CompiledObjective,
) -> Artifact:
    where = objective.where
    findings: list[Finding] = []
    identities: list[str] = []
    blocks: list[str] = []

    metric_template = load_template("cloudwatch-metric-alarm.tf.tmpl")
    composite_template = load_template("cloudwatch-composite-alarm.tf.tmpl")

    findings.append(note(
        "G406", where,
        "each tier renders as two alarms and one composite. An alarm evaluates one period, so a "
        "long and a short window cannot be two conditions of a single alarm; the composite's rule "
        "requires both children to be in ALARM. The children's actions are disabled so that one "
        "tier produces one notification, and the rule interpolates their names from the resources "
        "so Terraform can see the dependency -- a literal name in that string would leave it free "
        "to create the composite first.",
    ))
    findings.append(note(
        "G407", where,
        "values interpolated into these files are escaped for HCL, including the `${` and `%{` "
        "that open a Terraform interpolation. A description containing either would otherwise be "
        "read as a reference to something that does not exist and the configuration would fail to "
        "parse. The interpolations that are meant to be interpolations are written by the "
        "template, never by a value.",
    ))

    for tier in plan.tiers:
        queries = _tier_queries(compiled, tier.name)
        if "long" not in queries or "short" not in queries:
            findings.append(error(
                "G404", f"{where} :: tier {tier.name}",
                f"the source compiled "
                f"{', '.join(sorted(queries)) or 'nothing'} for this tier, and a composite needs "
                f"both windows. Nothing is rendered for it.",
            ))
            continue

        child_labels: dict[str, str] = {}
        for span in ("long", "short"):
            payload = queries[span]
            alarm_name = f"{objective.service}-{objective.name}-{tier.name}-{span}"
            label = terraform_label(objective.service, objective.name, tier.name, span)
            child_labels[span] = label
            identities.append(alarm_name)
            description = str(payload.get("AlarmDescription", ""))
            if len(description) > DESCRIPTION_CEILING:
                findings.append(error(
                    "G408", f"{where} :: tier {tier.name} ({span})",
                    f"the alarm description is {len(description)} characters against a ceiling of "
                    f"{DESCRIPTION_CEILING}; the create call is rejected.",
                ))
            period = payload["Metrics"][0]["MetricStat"]["Period"]
            blocks.append(metric_template.substitute({
                "comment": (
                    f"{tier.name} tier, {span} window ({humanise(period)}): fires at "
                    f"{format_rate(tier.threshold)}, by which point "
                    f"{tier.budget_fraction_at_detection:.1%} of the budget is spent."
                ),
                "label": label,
                "alarm_name": hcl_string(alarm_name),
                "description": hcl_string(description),
                "comparison": hcl_string(payload["ComparisonOperator"]),
                "threshold": _threshold_literal(float(payload["Threshold"])),
                "evaluation_periods": int(payload["EvaluationPeriods"]),
                "datapoints_to_alarm": int(payload.get("DatapointsToAlarm",
                                                       payload["EvaluationPeriods"])),
                "treat_missing_data": hcl_string(payload["TreatMissingData"]),
                "metric_queries": _metric_query_blocks(payload["Metrics"]),
            }))

        composite_name = f"{objective.service}-{objective.name}-{tier.name}"
        identities.append(composite_name)
        rule = (
            f'ALARM("{objective.service}-{objective.name}-{tier.name}-long") AND '
            f'ALARM("{objective.service}-{objective.name}-{tier.name}-short")'
        )
        if len(rule) > ALARM_RULE_CEILING:
            findings.append(error(
                "G409", f"{where} :: tier {tier.name}",
                f"the composite rule is {len(rule)} characters against a ceiling of "
                f"{ALARM_RULE_CEILING}.",
            ))
        blocks.append(composite_template.substitute({
            "comment": (
                f"{tier.name} tier: both windows required, notifying by {tier.tier.notify}. "
                f"Blind to a total outage shorter than "
                f"{humanise(tier.min_detectable_outage_seconds)}."
            ),
            "label": terraform_label(objective.service, objective.name, tier.name),
            "alarm_name": hcl_string(composite_name),
            "description": hcl_string(
                f"{objective.service}/{objective.name} burn-rate tier {tier.name}: error rate at "
                f"or above {format_rate(tier.threshold)} over "
                f"{humanise(tier.tier.long_seconds)} and over "
                f"{humanise(tier.tier.short_seconds)}. "
                f"{tier.budget_fraction_at_detection:.1%} of the budget is spent when this fires."
            ),
            "long_label": child_labels["long"],
            "short_label": child_labels["short"],
            "notify": hcl_string(tier.tier.notify),
        }))

    findings.append(sliding_window_caveat(where))
    findings.append(warn(
        "G410", where,
        "nothing here can confirm that the topics in slo_alert_topic_arns reach anybody. An alarm "
        "with an action pointing at a topic with no confirmed subscription is indistinguishable, "
        "from inside this configuration, from one that pages correctly.",
    ))
    findings.extend(_identity_findings(where, "cloudwatch", identities))

    header = load_template("cloudwatch-alarms.tf.tmpl")
    content = header.substitute({
        **_header_values(objective, plan),
        "alarm_blocks": "\n\n".join(blocks),
    })
    return Artifact(
        target="cloudwatch",
        filename=f"{objective.service}-{objective.name}.tf",
        content=content,
        identities=tuple(identities),
        findings=tuple(findings),
    )


# -- report -----------------------------------------------------------------


def _header_values(objective: Objective, plan: AlertPlan) -> dict[str, str]:
    # Every value here lands in a comment, in both the YAML header and the HCL
    # header, so all of them are flattened rather than only the free-prose ones:
    # a service name is pattern-bounded today and the flattening costs nothing.
    return {
        "service": comment_text(objective.service),
        "objective_name": comment_text(objective.name),
        "title": comment_text(objective.title or objective.name),
        "owner": comment_text(objective.owner or "unstated"),
        "target_rate": f"{objective.target * 100:g}%",
        "window": humanise(plan.budget.window_seconds),
        "window_kind": f"{objective.window.kind} {plan.budget.window_label}",
        "budget_rate": format_rate(plan.budget.allowed),
        "budget_span": humanise(plan.budget.equivalent_outage_seconds),
    }


def render_report(
    objective: Objective, plan: AlertPlan, compiled: CompiledObjective | None,
) -> Artifact:
    rows: list[str] = []
    for tier in plan.tiers:
        def at(rate: float) -> str:
            seconds = tier.detection_seconds_at(rate)
            return humanise(seconds) if seconds is not None else "never"

        rows.append(
            f"| `{tier.name}` | {tier.tier.notify} | {format_rate(tier.threshold)} "
            f"({tier.tier.burn_rate:g}x) | {tier.budget_fraction_at_detection:.1%} | "
            f"{at(1.0)} | {at(0.5)} | {humanise(tier.tier.long_seconds)} | "
            f"{humanise(tier.release_seconds_at(1.0))} |"
        )

    coverage = [f.message for f in plan.findings if f.code == "G307"]
    # The report quotes the sliding-window arithmetic as fact, so where the
    # deployment target does not evaluate a sliding window the report says so
    # rather than leaving the caveat in the alarm file alone.
    extra: list[Finding] = []
    if compiled is not None and compiled.source == "cloudwatch":
        extra.append(sliding_window_caveat(objective.where))
    findings_text = "\n".join(
        f"- **{f.code}** ({f.severity}) — {f.message}"
        for f in plan.all_findings() + extra if f.code != "G307"
    ) or "- None."

    artifact_rows = []
    if compiled is not None:
        artifact_rows.append(
            f"| `{compiled.source}` queries | {compiled.source} | "
            f"{len(compiled.tiers)} compiled expression(s), "
            f"{len(compiled.errors)} error(s), {len(compiled.warnings)} warning(s) |"
        )
    artifact_rows.append(
        f"| Objective identity | `{objective.key}` | "
        f"{len(objective.key)} of the 32 characters the schema allows for `service` and `name` |"
    )

    worst_latch = max(
        (tier.latch_seconds_without_short_window(1.0) for tier in plan.tiers), default=0.0
    )
    values = {
        **_header_values(objective, plan),
        "sli_kind": str(objective.sli.get("kind", "unknown")),
        "nominal_marker": ", nominal" if plan.budget.nominal else "",
        "budget_events": (
            f", about {plan.budget.events:,.0f} events at the stated rate"
            if plan.budget.events else ""
        ),
        "tier_rows": "\n".join(rows) or "| _none_ | | | | | | | |",
        "coverage": "\n\n".join(coverage) or "Every sustained error rate that can exhaust the "
                                             "budget crosses at least one threshold.",
        "findings": findings_text,
        "artifact_rows": "\n".join(artifact_rows),
        "worst_latch": humanise(worst_latch),
    }
    return Artifact(
        target="report",
        filename=f"{objective.service}-{objective.name}.md",
        content=load_template("budget-report.md.tmpl").substitute(values),
        identities=(objective.key,),
        findings=tuple(extra),
    )


RENDERERS = {
    "prometheus": render_prometheus,
    "cloudwatch": render_cloudwatch,
    "report": render_report,
}


def render_objective(objective: Objective, target: str, source_name: str | None) -> tuple[
    AlertPlan, CompiledObjective | None, Artifact | None
]:
    plan = plan_objective(objective)
    compiled: CompiledObjective | None = None
    if source_name:
        compiled = get_source(source_name).compile(objective)
    if not plan.plannable:
        return plan, compiled, None
    if compiled is not None and not compiled.deployable:
        return plan, compiled, None
    if target == "report":
        return plan, compiled, render_report(objective, plan, compiled)
    assert compiled is not None  # a non-report target always resolves a source
    return plan, compiled, RENDERERS[target](objective, plan, compiled)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render burn-rate alerts and budget reports from objective specifications.",
    )
    parser.add_argument("target", nargs="?", default=Path("specs"), type=Path,
                        help="specification file, or directory to search (default: specs)")
    parser.add_argument("--render", choices=TARGETS, default="report",
                        help="artifact to produce (default: report)")
    parser.add_argument("--source", default=None,
                        help="indicator source to compile queries with; defaults to the one the "
                             "chosen artifact requires")
    parser.add_argument("--out", type=Path, default=None,
                        help="directory to write artifacts into; omitted, they go to stdout")
    parser.add_argument("--pattern", default="*.yaml", help="glob used when target is a directory")
    parser.add_argument("--json", dest="as_json", action="store_true",
                        help="emit the computed plan as JSON instead of an artifact")
    parser.add_argument("--strict", action="store_true", help="treat warnings as failures")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    source_name = args.source or TARGET_SOURCE[args.render]
    if args.render != "report" and args.source and args.source != TARGET_SOURCE[args.render]:
        print(
            f"error: the {args.render} artifact cannot be rendered from the {args.source} source: "
            f"the compiled payloads are different shapes and no translation between them preserves "
            f"the arithmetic.",
            file=sys.stderr,
        )
        return 2

    paths = discover(args.target, args.pattern)
    if not paths:
        print(f"no specification matched {args.pattern!r} under {args.target} -- nothing rendered")
        return 0

    plans: list[AlertPlan] = []
    artifacts: list[Artifact] = []
    # Findings from the source adapter are part of this run's outcome: an
    # objective the source refused is an objective that produced no alert, and
    # a summary that counted only the generator's own findings would report a
    # run which rendered nothing as a clean one.
    source_findings: list[Finding] = []
    for path in paths:
        doc = load_document(path)
        if doc is None:
            print(f"error: {path}: document is empty", file=sys.stderr)
            return 2
        for objective in objectives_in(doc, path.name):
            LOG.debug("rendering %s as %s", objective.key, args.render)
            plan, compiled, artifact = render_objective(objective, args.render, source_name)
            plans.append(plan)
            if compiled is not None:
                source_findings.extend(compiled.all_findings())
            if artifact is not None:
                artifacts.append(artifact)
            elif not args.as_json:
                reason = "the plan has errors" if plan.errors else "the source refused the indicator"
                print(f"\n{objective.where}: NOT RENDERED -- {reason}", file=sys.stderr)

    if args.as_json:
        print(json.dumps({"render": args.render, "plans": [p.as_dict() for p in plans]}, indent=2))
    elif args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        for artifact in artifacts:
            destination = args.out / artifact.filename
            destination.write_text(artifact.content, encoding="utf-8")
            print(f"wrote {destination} ({len(artifact.content):,} bytes)")
    else:
        for artifact in artifacts:
            print(f"# ---- {artifact.filename} " + "-" * 40)
            print(artifact.content)

    findings = [f for plan in plans for f in plan.all_findings()]
    findings.extend(source_findings)
    findings.extend(f for artifact in artifacts for f in artifact.findings)
    errors = sum(1 for f in findings if f.severity == "error")
    warnings = sum(1 for f in findings if f.severity == "warning")
    for finding in findings:
        if finding.severity != "note":
            print(finding.render(), file=sys.stderr)
    # The summary goes to stderr, not stdout. With `--json` stdout IS the
    # document, and a status line appended to it makes the output unparseable by
    # the one consumer the flag exists for.
    print(
        f"\n{len(plans)} objective(s), {len(artifacts)} artifact(s), {errors} error(s), "
        f"{warnings} warning(s)",
        file=sys.stderr,
    )
    if errors:
        return 1
    if plans and not artifacts and not args.as_json:
        # Nothing was refused and nothing was produced. Reported rather than
        # returned as success, because a pipeline step that deploys whatever
        # was rendered would deploy an empty directory over a working one.
        print(
            "error: every objective was read and no artifact was produced", file=sys.stderr,
        )
        return 1
    return 1 if (args.strict and warnings) else 0


if __name__ == "__main__":  # pragma: no cover - thin wrapper
    raise SystemExit(main())
