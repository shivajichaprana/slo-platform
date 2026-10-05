#!/usr/bin/env python3
"""Turn one objective into the arithmetic a burn-rate alert policy is made of.

The specification says what proportion of events must be good and at what
multiples of the budget's spend rate somebody should be told. It does not say
what those choices buy, and that is the gap this module closes: a burn-rate
tier is a promise about detection, and the promise is arithmetic rather than
opinion.

Four results are computed here, and only the second of them is commonly
written down.

**How long detection takes depends on the incident, not only on the tier.** A
tier fires when the average error rate over its long window reaches
`burn_rate x (1 - objective)`. An incident at a constant error rate `r` pushes
that trailing average up linearly, so the condition is met after
`threshold x long_window / r` seconds. The tier therefore has no single
detection time: it has a curve, fast for a total outage and asymptotically the
whole long window for an incident barely above the threshold. A policy
described by its burn rates alone hides that entirely.

**What the tier costs is NOT a curve.** Substituting the detection time back
into the budget consumed gives `burn_rate x long_window / objective_window`,
with `r` cancelled out: the share of the budget already spent when a tier fires
is the same whether the service is wholly down or only slightly degraded. That
invariant is the one number worth arguing about when a tier is chosen, and it
is the one the specification's two window fields obscure.

**Every tier is blind to short incidents.** The long window's average cannot
reach the threshold before `threshold x long_window` seconds have passed, even
at a 100% error rate, so each tier has a minimum detectable outage duration
below which a total failure is invisible to it. A policy of three tiers has
three such floors, and the smallest of them is the fastest the arrangement can
ever be.

**Every tier is blind to slow burns.** The lowest threshold in the policy is
the lowest sustained error rate anything will ever report. A rate just below it
spends budget steadily and silently, and the time it takes to spend all of it
is `objective_window / lowest_burn_rate` -- which is why a policy whose slowest
burn rate is above 1 has a band of error rates that exhaust the budget with
nothing firing, and a policy whose slowest burn rate is 1 does not.

What is deliberately NOT here: reachability of a tier, the share of the budget
spent at detection exceeding a half, the detection floor implied by sampling,
and the conventional ratio between the two windows. Those are specification
faults, they are reported by `tools/validate-specs.py`, and reporting them
twice in two vocabularies is how two components come to disagree about a
boundary. The one exception is reachability, which is re-derived here because
this module must be safe to call on a document that was never validated -- an
unreachable tier would otherwise be rendered into an alert that cannot fire.
The repository's own checks assert the two derivations agree.

Finding codes:

====  ==========================================================================
G1xx  The objective cannot be planned at all.
G2xx  Identity: a rendered name that would collide or could not exist.
G3xx  The policy's arithmetic: detection, cost, and what it cannot see.
====  ==========================================================================
"""

from __future__ import annotations

import dataclasses
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

# The indicator model lives beside the sources that produce it. This
# repository ships flat module directories rather than an installable package
# on purpose -- every tool here is run from a checkout, against the
# specifications in the same checkout -- so the import path is extended
# explicitly instead of through packaging metadata that does not exist.
_SOURCES = Path(__file__).resolve().parent.parent / "sources"
if str(_SOURCES) not in sys.path:
    sys.path.insert(0, str(_SOURCES))

from base import (  # noqa: E402  (path set above)
    Finding,
    Objective,
    Tier,
    error,
    humanise,
    note,
    warn,
)

LOG = logging.getLogger("slo.generator.burn_rate")

#: Error rates the detection curve is sampled at for the report. 1.0 is a total
#: outage; the others are the shapes an incident usually has. The tier's own
#: threshold is added per tier, because detection there is the long window
#: exactly and that is the slow end of the curve.
REPORTED_ERROR_RATES = (1.0, 0.5, 0.1)

#: Longest name each render target accepts for one alert. These are the limits
#: that actually bound an alert's identity. The 64-character budget in
#: `locals.tf` is an IAM role name, which is a different resource: an alarm name
#: may be 255 characters and a Prometheus alert name is a label value.
NAME_CEILING = {"cloudwatch": 255, "prometheus": 255}

#: Longest description an alarm carries. Exceeded, the create call is rejected,
#: so a generated description is bounded here rather than at the API.
DESCRIPTION_CEILING = 1024

#: A page is answered by a human, so a tier whose fastest possible detection is
#: slower than this is reporting an outage somebody has already been told about
#: by other means. A convention, not a limit -- it is stated as one.
PAGE_DETECTION_CONVENTION_SECONDS = 600


def format_rate(value: float) -> str:
    """Render an error rate without pretending to precision it does not have.

    Public because the renderers quote the same figures into alert text and into
    the budget report, and two formatters would eventually disagree about how
    many digits a threshold has.
    """
    if value >= 0.01:
        return f"{value:.2%}"
    if value >= 0.0001:
        return f"{value:.4%}"
    return f"{value:.3e}"


@dataclass(frozen=True)
class Budget:
    """The error budget, in the three units it gets quoted in.

    `equivalent_outage_seconds` is the one that invites a wrong reading, so it
    is named for what it is: the length of a hypothetical total failure that
    would consume the whole budget. It is not downtime unless the indicator
    happens to count time, and for an event ratio it is a translation offered
    for intuition rather than a measurement.
    """

    allowed: float
    window_seconds: int
    nominal: bool
    window_label: str
    equivalent_outage_seconds: float
    events: float | None

    @classmethod
    def of(cls, objective: Objective) -> "Budget":
        sampling = objective.sampling_per_hour()
        window = objective.window
        events = None
        if sampling:
            events = sampling * window.seconds / 3600 * objective.allowed
        return cls(
            allowed=objective.allowed,
            window_seconds=window.seconds,
            nominal=window.nominal,
            window_label=window.label,
            equivalent_outage_seconds=objective.allowed * window.seconds,
            events=events,
        )

    def exhaustion_seconds_at(self, rate: float) -> float | None:
        """How long a sustained error rate takes to spend the whole budget.

        None when the rate cannot spend it: on a rolling window the budget is
        replenished continuously, so a sustained rate at or below the rate the
        objective permits never exhausts it. That is the same boundary a burn
        rate of 1 sits on, which is why a 1x tier is a budget-tracking signal
        rather than a prediction of exhaustion.
        """
        if rate <= 0:
            return None
        if rate <= self.allowed:
            return None
        return self.allowed * self.window_seconds / rate


@dataclass(frozen=True)
class TierPlan:
    """One burn-rate tier, with what its two windows actually buy."""

    tier: Tier
    threshold: float
    budget_fraction_at_detection: float
    min_detectable_outage_seconds: float
    events_in_short_window: float | None
    failed_events_to_breach_short: float | None
    findings: tuple[Finding, ...] = ()

    @property
    def name(self) -> str:
        return self.tier.name

    def detection_seconds_at(self, rate: float) -> float | None:
        """Seconds from the start of a constant-rate incident to the tier firing.

        None when the rate is below the threshold: the trailing average never
        reaches it, so the tier does not fire late, it does not fire.
        """
        if rate <= 0 or rate < self.threshold:
            return None
        return self.threshold * self.tier.long_seconds / rate

    def release_seconds_at(self, rate: float) -> float:
        """Seconds from the end of the incident to the alert clearing.

        The short window is what sets this. Without it the condition stays true
        until the long window's average falls back under the threshold, which
        takes `long_window x (1 - threshold/rate)` -- nearly the whole long
        window for a severe incident. The short window replaces that with the
        same expression over its own length.
        """
        if rate <= 0:
            return float(self.tier.short_seconds)
        return self.tier.short_seconds * max(0.0, 1.0 - self.threshold / rate)

    def latch_seconds_without_short_window(self, rate: float) -> float:
        if rate <= 0:
            return float(self.tier.long_seconds)
        return self.tier.long_seconds * max(0.0, 1.0 - self.threshold / rate)


@dataclass
class AlertPlan:
    """Everything the renderers need, and everything the arithmetic revealed."""

    objective: Objective
    budget: Budget
    tiers: list[TierPlan] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    def all_findings(self) -> list[Finding]:
        out = list(self.findings)
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
    def plannable(self) -> bool:
        return not self.errors and bool(self.tiers)

    def as_dict(self) -> dict[str, Any]:
        return {
            "objective": self.objective.key,
            "where": self.objective.where,
            "plannable": self.plannable,
            "budget": {
                "allowed": round(self.budget.allowed, 10),
                "window_seconds": self.budget.window_seconds,
                "window_label": self.budget.window_label,
                "nominal": self.budget.nominal,
                "equivalent_outage_seconds": round(self.budget.equivalent_outage_seconds, 3),
                "events": round(self.budget.events, 2) if self.budget.events else None,
            },
            "tiers": [
                {
                    "name": plan.name,
                    "burn_rate": plan.tier.burn_rate,
                    "notify": plan.tier.notify,
                    "long_window_seconds": plan.tier.long_seconds,
                    "short_window_seconds": plan.tier.short_seconds,
                    "fires_at_error_rate": round(plan.threshold, 10),
                    "budget_fraction_at_detection": round(plan.budget_fraction_at_detection, 6),
                    "min_detectable_outage_seconds": round(plan.min_detectable_outage_seconds, 3),
                    "detection_seconds": {
                        f"{rate:g}": (
                            round(value, 3) if (value := plan.detection_seconds_at(rate)) else None
                        )
                        for rate in REPORTED_ERROR_RATES
                    },
                    "release_seconds_at_total_outage": round(plan.release_seconds_at(1.0), 3),
                    "latch_seconds_without_short_window": round(
                        plan.latch_seconds_without_short_window(1.0), 3
                    ),
                    "failed_events_to_breach_short": (
                        round(plan.failed_events_to_breach_short, 2)
                        if plan.failed_events_to_breach_short is not None
                        else None
                    ),
                }
                for plan in self.tiers
            ],
            "findings": [
                {"code": f.code, "severity": f.severity, "where": f.where, "message": f.message}
                for f in self.all_findings()
            ],
        }


def plan_objective(objective: Objective) -> AlertPlan:
    """Compute the alert policy's arithmetic for one objective."""
    budget = Budget.of(objective)
    plan = AlertPlan(objective=objective, budget=budget)
    where = objective.where

    if not objective.tiers:
        plan.findings.append(error(
            "G100", where,
            "the objective declares no burn-rate tier, so there is nothing to generate. An "
            "objective with no alerting policy is measured and never reported on.",
        ))
        return plan

    plan.findings.extend(_duplicate_tier_names(objective))
    rate_per_hour = objective.sampling_per_hour()

    for tier in objective.tiers:
        plan.tiers.append(_plan_tier(objective, tier, budget, rate_per_hour))

    if plan.errors:
        # The policy-wide checks below compare tiers against each other, and a
        # comparison against a tier that cannot fire describes an arrangement
        # that does not exist.
        return plan

    plan.findings.extend(_shadowed_tiers(objective, plan.tiers))
    plan.findings.extend(_coverage_floor(objective, budget, plan.tiers))
    plan.findings.extend(_fastest_tier_reach(objective, plan.tiers))
    plan.findings.append(_budget_note(objective, budget))
    return plan


def _duplicate_tier_names(objective: Objective) -> list[Finding]:
    """Two tiers with one name render to one alert, silently.

    The validator reports the duplicate as a specification fault. This reports
    the consequence, because it is the renderer's problem: the second alert
    overwrites the first on every target here -- a Prometheus rule file with two
    identical alert names is accepted and groups them, and a Terraform resource
    label is unique by definition, so the generated configuration would be
    short one alarm with nothing saying so.
    """
    seen: dict[str, int] = {}
    findings: list[Finding] = []
    for tier in objective.tiers:
        seen[tier.name] = seen.get(tier.name, 0) + 1
    for name, count in seen.items():
        if count > 1:
            findings.append(error(
                "G200", f"{objective.where} :: tier {name}",
                f"{count} tiers share the name {name!r}, so they render to one alert identity. The "
                f"later definition replaces the earlier one on every target, which loses an alert "
                f"without failing: a rule file accepts repeated alert names and a Terraform "
                f"resource label cannot be repeated at all.",
            ))
    return findings


def _plan_tier(
    objective: Objective, tier: Tier, budget: Budget, rate_per_hour: float | None,
) -> TierPlan:
    where = f"{objective.where} :: tier {tier.name}"
    findings: list[Finding] = []
    threshold = tier.fires_at_error_rate(objective.allowed)

    events_in_short = None
    failed_to_breach = None
    if rate_per_hour:
        events_in_short = rate_per_hour * tier.short_seconds / 3600
        failed_to_breach = threshold * events_in_short

    plan = TierPlan(
        tier=tier,
        threshold=threshold,
        budget_fraction_at_detection=tier.burn_rate * tier.long_seconds / budget.window_seconds,
        min_detectable_outage_seconds=threshold * tier.long_seconds,
        events_in_short_window=events_in_short,
        failed_events_to_breach_short=failed_to_breach,
    )

    if threshold > 1.0:
        # Re-derived rather than taken from the validator, because this module
        # has to be safe to call on an unvalidated document: without this the
        # tier renders into an alert whose threshold is above 100% and which is
        # therefore accepted, deployed and permanently silent.
        findings.append(error(
            "G301", where,
            f"the tier fires at an error rate of {format_rate(threshold)} (burn rate "
            f"{tier.burn_rate:g} x a {format_rate(objective.allowed)} budget), which is above 100% "
            f"and cannot occur. Nothing is generated for it: a rendered alert would deploy "
            f"cleanly and never fire. The highest burn rate this objective can express is "
            f"{1 / objective.allowed:.1f}.",
        ))
        return dataclasses.replace(plan, findings=tuple(findings))

    findings.append(note(
        "G300", where,
        f"detection is not a single figure: the trailing average over "
        f"{humanise(tier.long_seconds)} rises in proportion to the error rate, so this tier fires "
        f"after {humanise(plan.detection_seconds_at(1.0) or 0)} of a total outage, "
        f"{humanise(plan.detection_seconds_at(0.5) or 0)} at a 50% error rate, and the whole "
        f"{humanise(tier.long_seconds)} for an incident sitting exactly on its "
        f"{format_rate(threshold)} threshold. The share of the budget spent by then is the same in "
        f"every one of those cases -- {plan.budget_fraction_at_detection:.2%} -- because the "
        f"error rate cancels out of that expression. That figure, not the burn rate, is what "
        f"choosing this tier costs.",
    ))

    findings.append(note(
        "G302", where,
        f"read the other way round, the same {humanise(plan.min_detectable_outage_seconds)} is a "
        f"floor rather than a latency: a total outage that ENDS before it has elapsed never pushes "
        f"the {humanise(tier.long_seconds)} average as far as {format_rate(threshold)}, so this tier "
        f"is blind to it. The two figures are one quantity -- `threshold x long_window` -- and the "
        f"short window does not lower it, because it is a second condition rather than a faster "
        f"one.",
    ))

    findings.append(note(
        "G303", where,
        f"the short window sets the clearing time, not the firing time. For an incident at a "
        f"constant error rate the short window is always above the threshold by the moment the "
        f"long one crosses it, so firing is governed by the long window alone. What the short "
        f"window removes is the latch: after a total outage stops, the long window's average stays "
        f"above the threshold for {humanise(plan.latch_seconds_without_short_window(1.0))}, while "
        f"the pair clears in {humanise(plan.release_seconds_at(1.0))}.",
    ))

    findings.append(note(
        "G310", where,
        f"the cost of requiring the short window is that budget spent in bursts is not reported. A "
        f"burst at a total error rate lasting under "
        f"{humanise(plan.min_detectable_outage_seconds)} cannot satisfy the long condition on its "
        f"own, and several of them inside one {humanise(tier.long_seconds)} window can satisfy it "
        f"between them at a moment when the short window is quiet -- so the long side is true, the "
        f"short side is false, and the tier stays silent while the budget goes. Repeated brief "
        f"failures are therefore the shape of incident this policy is structurally worst at, and "
        f"are better reported as a count of failures than as a burn rate.",
    ))

    if tier.burn_rate <= 1.0:
        findings.append(note(
            "G304", where,
            f"a burn rate of {tier.burn_rate:g} fires at {format_rate(threshold)}, which is at or "
            f"below the rate the objective permits indefinitely. On a rolling window a sustained "
            f"rate there never exhausts the budget, because it is replenished as fast as it is "
            f"spent, and the comparison is 'at or above' -- so a service performing exactly to "
            f"its objective satisfies this tier. It is a budget-tracking signal rather than a "
            f"prediction of exhaustion, which is consistent with notifying by "
            f"{tier.notify!r} and would not be with paging.",
        ))

    if failed_to_breach is not None and failed_to_breach < 5:
        findings.append(warn(
            "G305", where,
            f"the short window holds about {events_in_short:,.0f} events, so breaching "
            f"{format_rate(threshold)} there takes {failed_to_breach:.1f} failed events. A condition "
            f"a handful of events can satisfy is dominated by ordinary variation rather than by "
            f"the service's behaviour, so the confirming window confirms noise. Lengthening the "
            f"short window raises the count in proportion.",
        ))

    return dataclasses.replace(plan, findings=tuple(findings))


def _shadowed_tiers(objective: Objective, plans: list[TierPlan]) -> list[Finding]:
    """A tier that can never be the first thing anyone hears about.

    Tier B is shadowed by tier A when A's threshold is no higher and A's long
    window no longer: A then fires at every error rate B does, and never later.
    A shadowed tier can therefore contribute exactly one thing -- an escalation,
    reaching an audience the earlier notification did not. If it notifies at the
    same urgency or lower, everyone it would tell has already been told, so it
    is reported. If it notifies MORE urgently it is left alone: a page behind a
    ticket is still the first page, however late it is.

    Distinct from the validator's equal-burn-rate check, which compares burn
    rates alone: two tiers with different burn rates can stand in this relation,
    and two with the same burn rate need not when their windows are ordered the
    other way.

    """
    urgency = {"ticket": 0, "page": 1}
    findings: list[Finding] = []
    for shadowed in plans:
        for other in plans:
            if other is shadowed:
                continue
            no_higher = other.threshold <= shadowed.threshold
            no_longer = other.tier.long_seconds <= shadowed.tier.long_seconds
            strictly_better = (
                other.threshold < shadowed.threshold
                or other.tier.long_seconds < shadowed.tier.long_seconds
            )
            # The only thing a shadowed tier can add is an escalation, so it is
            # redundant unless it notifies MORE urgently than the tier that fired
            # first. Equal urgency reaches an audience already told.
            redundant = urgency[shadowed.tier.notify] <= urgency[other.tier.notify]
            if no_higher and no_longer and strictly_better and redundant:
                findings.append(warn(
                    "G306", f"{objective.where} :: tier {shadowed.name}",
                    f"tier {other.name!r} fires at {format_rate(other.threshold)} over "
                    f"{humanise(other.tier.long_seconds)} and notifies by "
                    f"{other.tier.notify!r}, so it is triggered by every incident this tier is "
                    f"triggered by, never later, and reaches an audience at least as wide. "
                    f"This tier ({format_rate(shadowed.threshold)} over "
                    f"{humanise(shadowed.tier.long_seconds)}) can therefore never be the first "
                    f"notification about anything, and notifying no more urgently it cannot be an "
                    f"escalation either. A slower tier earns its place by being more sensitive -- "
                    f"a LOWER threshold over a longer window -- not by being slower at a higher "
                    f"one.",
                ))
                break
    return findings


def _coverage_floor(
    objective: Objective, budget: Budget, plans: list[TierPlan],
) -> list[Finding]:
    """The band of error rates that spends the budget with nothing firing."""
    slowest = min(plans, key=lambda p: p.threshold)
    floor = slowest.threshold
    exhaustion = budget.exhaustion_seconds_at(floor)
    nominal = " nominal" if budget.nominal else ""

    if exhaustion is None:
        return [note(
            "G307", objective.where,
            f"the lowest threshold in the policy is {format_rate(floor)} (tier "
            f"{slowest.name!r}), which is at or below the {format_rate(budget.allowed)} the "
            f"objective permits, so there is no undetected band: every sustained error rate "
            f"capable of exhausting the budget crosses a threshold. This is what a slowest burn "
            f"rate of {slowest.tier.burn_rate:g} buys, and it is the reason to accept that tier "
            f"firing on nominal behaviour.",
        )]

    return [warn(
        "G307", objective.where,
        f"a sustained error rate just below {format_rate(floor)} -- the lowest threshold in the "
        f"policy, tier {slowest.name!r} -- is never reported by any tier, and spends the whole "
        f"budget in about {humanise(exhaustion)} against a {humanise(budget.window_seconds)}"
        f"{nominal} window. The objective is then missed with no alert having fired at any point, "
        f"which is the failure a burn-rate policy is usually assumed to rule out. A tier at a burn "
        f"rate of 1 closes the band, at the cost of firing while the service is meeting its "
        f"objective.",
    )]


def _fastest_tier_reach(objective: Objective, plans: list[TierPlan]) -> list[Finding]:
    """Whether the quickest thing in the policy is quick enough to be a page."""
    paging = [p for p in plans if p.tier.notify == "page"]
    if not paging:
        return []
    fastest = min(paging, key=lambda p: p.min_detectable_outage_seconds)
    if fastest.min_detectable_outage_seconds <= PAGE_DETECTION_CONVENTION_SECONDS:
        return []
    return [warn(
        "G308", f"{objective.where} :: tier {fastest.name}",
        f"the fastest paging tier cannot see a total outage shorter than "
        f"{humanise(fastest.min_detectable_outage_seconds)}, and a page is answered on a scale of "
        f"minutes. Anything briefer is handled by someone who found out another way, so the page "
        f"arrives as confirmation rather than as news. Shortening the long window or raising the "
        f"burn rate both lower that floor; raising the burn rate also raises what the tier costs "
        f"in budget. The {humanise(PAGE_DETECTION_CONVENTION_SECONDS)} comparison is a convention "
        f"about human response, not a limit of any system here.",
    )]


def _budget_note(objective: Objective, budget: Budget) -> Finding:
    equivalent = humanise(budget.equivalent_outage_seconds)
    events = (
        f" and about {budget.events:,.0f} events at the stated rate" if budget.events else ""
    )
    nominal = (
        f" The window is a calendar {budget.window_label}, so {humanise(budget.window_seconds)} "
        f"is nominal and every figure derived from it is too."
        if budget.nominal else ""
    )
    return note(
        "G309", objective.where,
        f"the budget is {format_rate(budget.allowed)} of events over "
        f"{humanise(budget.window_seconds)}{events}. Quoted as a span it is {equivalent}, which is "
        f"the length of a hypothetical total failure that would consume all of it -- not downtime, "
        f"unless the indicator counts time rather than events.{nominal}",
    )


def plan_objectives(objectives: Iterable[Objective]) -> list[AlertPlan]:
    return [plan_objective(objective) for objective in objectives]
