#!/usr/bin/env python3
"""Decide what an error budget's state permits, and record the decision where a gate reads it.

A burn-rate alert says something is wrong now. An error-budget policy says what
CHANGES while the budget is in the state it is in, which is a different kind of
statement: it is about a month rather than about a minute, it is acted on by a
pipeline rather than by a person, and it is wrong in ways an alert is not.

This module is the enforcement half of the repository. It reads a policy
document, reads the objectives the policy refers to, reads observed budget
consumption supplied by whatever computes it, and produces one decision per
deployable unit. Like the source adapters, it never queries: consumption is an
input, not something fetched here, which keeps the arithmetic reviewable
offline and keeps a credential out of the component that is allowed to stop a
release.

Six results shape it, and most of them contradict the way a budget policy is
usually written down.

**A threshold on the remaining budget is a lagging control.** Remaining budget
is a level; what decides whether stopping deployments helps is the rate. A
service sitting at 30% remaining with no burn at all will never exhaust its
budget, and freezing it achieves nothing. A service at 60% remaining burning at
10x exhausts in a few days, and is not frozen by a rule written about 30%. The
projection is the primary condition and the level is a floor beneath it:

    time to exhaustion = remaining_fraction x objective_window / burn_rate

**The projection's error has a direction, and it is stated.** On a rolling
window, spend ages out as the window advances, so the expression above ignores
replenishment and therefore over-estimates consumption: it is conservative, and
fires earlier than strictly necessary. A projection whose bias is unknown is not
usable as a gate, which is why the direction matters more than the magnitude.

**A freeze is never ended by a fix.** On a rolling window the spend that caused
it leaves the window at a time set by WHEN it was spent, not by what was done
about it; the freeze therefore clears on its own, and nothing shortens it. On a
calendar window nothing leaves at all until the period boundary, so a freeze
entered on the second of the month lasts the rest of the month. Either way,
"ship the fix and we're unblocked" is not an exit condition, and a policy that
states one is unimplementable. The exits that exist are the window advancing,
an enumerated exemption, or changing the objective.

**A gate with one threshold flaps, and every flap is a pipeline state change.**
Consumption computed over a trailing window is a noisy series with a quantum:
one event moves the remaining fraction by `1 / budget_events`. A gate whose
enter and exit conditions are the same figure crosses it repeatedly, blocking
and unblocking deployments on single events. Enter and exit are therefore
separate fields, and the band between them is checked against the quantum --
below it, the hysteresis is narrower than the smallest step the measurement can
take and provides nothing.

**A stale figure widens both thresholds.** A decision made from a figure
computed `age` ago is a decision about `age` ago. At burn rate `B` the remaining
fraction moves `B x age / window` in that time, so the staleness allowance fuzzes
each threshold by that much. When it approaches the hysteresis band, the two
thresholds are indistinguishable in practice and the band buys nothing, however
carefully it was chosen.

**The direction a gate fails in has no safe default.** If the budget figure
cannot be read, the gate either allows everything or blocks everything. Closed
stops every deployment including the fix for whatever made the figure
unreadable; open silently disables the policy during precisely the outage the
policy exists for. The field is required and has no default, for the same reason
the specification's window kind has none.

Finding codes:

====  ==========================================================================
P1xx  The policy document cannot be read, or enforces nothing.
P2xx  Identity and coverage: which budgets a gate acts on, and which act on
      nothing.
P3xx  The control's arithmetic: lag, hysteresis, staleness, and the exit.
P4xx  Enforcement: the failure direction, exemptions, and the gate's authority.
====  ==========================================================================

Decisions are a separate vocabulary from findings. A finding is about the
policy; a decision is about one unit at one instant, and is the document a
deployment pipeline reads.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

_ROOT = Path(__file__).resolve().parent.parent
for _extra in (_ROOT / "sources", _ROOT / "generator"):
    if str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

from base import (  # noqa: E402  (path set above)
    Finding,
    Objective,
    discover,
    error,
    humanise,
    load_document,
    note,
    objectives_in,
    parse_duration_seconds,
    warn,
)
from burn_rate import Budget, format_rate  # noqa: E402  (path set above)

LOG = logging.getLogger("slo.policy.budget")

#: The actions a policy may take, ordered by how much they restrict. `allow` is
#: not spelt in a document -- it is the absence of a triggered rule -- but it is
#: in the order because a decision is the most restrictive action that applied.
ACTION_ORDER = ("allow", "notify", "review", "freeze")

#: Actions that stop a deployment. `notify` deliberately does not: a policy whose
#: strongest action is a notification is advisory, and this is the list that says
#: so rather than a comment claiming it.
BLOCKING_ACTIONS = ("review", "freeze")

#: What a gate does when the budget figure cannot be read. No default: see the
#: module docstring.
FAIL_DIRECTIONS = ("open", "closed")

#: How several objectives on one gate are combined.
COMBINE_MODES = ("any", "all")

#: A unit name becomes a path segment in the parameter the gate is published to,
#: and a Terraform resource key. The grammar is the intersection of what both
#: accept, so a name that passes here is deployable on either.
UNIT_NAME = re.compile(r"^[a-z][a-z0-9-]{1,31}$")

#: Characters left for a unit name by `locals.tf`. Asserted equal to the
#: Terraform reserve by the repository's own checks rather than kept in step by
#: hand -- a name budget that differs between the generator and the
#: configuration is a disagreement nobody would look for.
UNIT_NAME_BUDGET = 32

#: Objective reference, as `service/name`. One string rather than a nested pair
#: because it is also the key the budget arithmetic is reported under, and two
#: spellings of one identity is how a gate comes to reference an objective that
#: exists under a different name.
OBJECTIVE_REF = re.compile(r"^[a-z0-9][a-z0-9-]*/[a-z0-9][a-z0-9-]*$")

#: A hysteresis band narrower than this many events of budget is reported. One
#: event is the quantum; a band of a few events is still dominated by ordinary
#: variation, which is the same argument the generator makes about a short
#: window holding too few events.
MIN_HYSTERESIS_EVENTS = 5.0

#: Share of the hysteresis band the staleness allowance may consume before the
#: band stops distinguishing the two thresholds. A convention, stated as one.
STALENESS_BAND_SHARE = 0.25


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------

def parse_instant(text: str, where: str) -> datetime:
    """Read an ISO-8601 instant, requiring an offset.

    A naive timestamp is refused rather than assumed to be UTC. Every figure
    downstream is an age, and an age computed from a timestamp whose zone was
    guessed is wrong by the offset -- silently, and by exactly the amount that
    makes a stale figure look fresh.
    """
    candidate = text.strip()
    if candidate.endswith(("Z", "z")):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError(f"{where}: not an ISO-8601 instant: {text!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(
            f"{where}: {text!r} carries no UTC offset. An age computed from it would be wrong "
            f"by the offset, which is the error that makes a stale budget figure look fresh."
        )
    return parsed.astimezone(timezone.utc)


def _zone(name: str) -> tuple[Any, str | None]:
    """The named zone, or UTC and the reason it was substituted."""
    if name.upper() == "UTC":
        return timezone.utc, None
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    except ImportError:  # pragma: no cover - stdlib since 3.9
        return timezone.utc, "the zoneinfo module is unavailable"
    try:
        return ZoneInfo(name), None
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc, f"the zone {name!r} is not installed on this machine"


def period_end(now: datetime, period: str, zone_name: str) -> tuple[datetime, str | None]:
    """When the current calendar period ends, and why the zone may have been substituted.

    The boundary is a wall-clock instant in the objective's own zone, so it is
    computed there and converted back. Returned with the substitution reason
    rather than silently falling back, because a boundary computed in the wrong
    zone moves the budget reset by up to a day and the figure still looks
    plausible.
    """
    tzinfo, reason = _zone(zone_name)
    local = now.astimezone(tzinfo)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        end = midnight + timedelta(days=7 - local.weekday())
    elif period == "month":
        end = (
            midnight.replace(year=local.year + 1, month=1, day=1)
            if local.month == 12
            else midnight.replace(month=local.month + 1, day=1)
        )
    elif period == "quarter":
        first_of_next = local.month - (local.month - 1) % 3 + 3
        end = (
            midnight.replace(year=local.year + 1, month=1, day=1)
            if first_of_next > 12
            else midnight.replace(month=first_of_next, day=1)
        )
    else:
        raise ValueError(f"unknown calendar period: {period!r}")
    return end.astimezone(timezone.utc), reason


# ---------------------------------------------------------------------------
# The policy document
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Exemption:
    """A class of change a freeze does not block."""

    name: str
    approver: str
    max_duration_seconds: int | None
    description: str = ""


@dataclass(frozen=True)
class Rule:
    """One condition, and what it does when it holds.

    `remaining_below` and `exhaustion_within_seconds` are the two trigger
    shapes; a rule may carry either or both, and with both it triggers when
    either holds. `clear_above` is the exit, and it is always a LEVEL even for a
    projection trigger: a projection recovers the instant the burn stops, so a
    projection-shaped exit would unblock a pipeline while the budget was still
    nearly gone.
    """

    name: str
    action: str
    remaining_below: float | None
    exhaustion_within_seconds: int | None
    clear_above: float | None
    reason: str = ""

    @property
    def blocking(self) -> bool:
        return self.action in BLOCKING_ACTIONS


@dataclass(frozen=True)
class Gate:
    """One deployable unit, the budgets that govern it, and the rules applied."""

    unit: str
    objectives: tuple[str, ...]
    combine: str
    on_unreadable_budget: str
    max_budget_age_seconds: int
    rules: tuple[Rule, ...]
    exemptions: tuple[Exemption, ...]
    description: str = ""

    @property
    def strongest_action(self) -> str:
        if not self.rules:
            return "allow"
        return max((r.action for r in self.rules), key=ACTION_ORDER.index)


@dataclass(frozen=True)
class Policy:
    """A parsed policy document."""

    name: str
    owner: str
    gates: tuple[Gate, ...]
    description: str = ""
    source: str = ""


class PolicyDocumentError(Exception):
    """The document could not be read as a policy at all."""


def _require(mapping: Any, key: str, where: str) -> Any:
    if not isinstance(mapping, dict) or key not in mapping:
        raise PolicyDocumentError(f"{where}: required field {key!r} is missing")
    return mapping[key]


def _duration(value: Any, where: str) -> int:
    if not isinstance(value, str):
        raise PolicyDocumentError(f"{where}: expected a duration string such as \"30m\", got {value!r}")
    try:
        return parse_duration_seconds(value)
    except ValueError as exc:
        raise PolicyDocumentError(f"{where}: {exc}") from exc


def _fraction(value: Any, where: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise PolicyDocumentError(f"{where}: expected a number between 0 and 1, got {value!r}")
    number = float(value)
    if not 0.0 < number <= 1.0:
        raise PolicyDocumentError(
            f"{where}: {number} is not a remaining-budget fraction. 0 is a budget already gone, "
            f"which no threshold can be crossed from, and above 1 is more budget than exists."
        )
    return number


def parse_policy(doc: Any, source: str) -> Policy:
    """Read a policy document, refusing anything whose meaning would be guessed.

    This is not a second schema for the objectives -- those are validated by
    `tools/validate-specs.py`, and a policy that re-checked their shape would
    report specification faults in the vocabulary of a release gate. What is
    checked here is only the policy's own structure, and only to the depth the
    evaluation depends on.
    """
    if not isinstance(doc, dict):
        raise PolicyDocumentError(f"{source}: the document is not a mapping")
    kind = doc.get("kind")
    if kind != "ErrorBudgetPolicy":
        raise PolicyDocumentError(
            f"{source}: kind is {kind!r}, not \"ErrorBudgetPolicy\". An objective specification "
            f"and a budget policy are different documents and are not interchangeable."
        )
    metadata = _require(doc, "metadata", source)
    defaults = doc.get("defaults") or {}
    default_age = _duration(defaults.get("max_budget_age", "15m"), f"{source} :: defaults.max_budget_age")
    default_exemption = defaults.get("exemption_max_duration")

    gates: list[Gate] = []
    raw_gates = doc.get("gates")
    if raw_gates is not None and not isinstance(raw_gates, list):
        raise PolicyDocumentError(f"{source}: gates must be a list")
    for index, raw in enumerate(raw_gates or []):
        where = f"{source} :: gates[{index}]"
        unit = _require(raw, "unit", where)
        if not isinstance(unit, str) or not UNIT_NAME.match(unit):
            raise PolicyDocumentError(
                f"{where}: unit {unit!r} is not a deployable-unit name. It becomes a path segment "
                f"in the parameter the decision is published to and a resource key in the "
                f"configuration, so it is 2-32 characters of lowercase letters, digits and "
                f"hyphens, starting with a letter."
            )
        where = f"{source} :: gate {unit}"
        direction = raw.get("on_unreadable_budget")
        if direction not in FAIL_DIRECTIONS:
            # Refused at parse time rather than reported as a finding: without
            # it there is no defined behaviour for an unreadable figure, so
            # there is nothing to evaluate rather than something to warn about.
            raise PolicyDocumentError(
                f"{where}: on_unreadable_budget is {direction!r}, which must be one of "
                f"{', '.join(FAIL_DIRECTIONS)}. There is no default: \"closed\" stops every "
                f"deployment including the fix for whatever made the budget unreadable, and "
                f"\"open\" disables this gate during exactly the outage it exists for."
            )
        combine = raw.get("combine", "any")
        if combine not in COMBINE_MODES:
            raise PolicyDocumentError(
                f"{where}: combine is {combine!r}, which must be one of {', '.join(COMBINE_MODES)}"
            )
        refs = raw.get("objectives") or []
        if not isinstance(refs, list) or not all(isinstance(r, str) for r in refs):
            raise PolicyDocumentError(f"{where}: objectives must be a list of \"service/name\" strings")
        for ref in refs:
            if not OBJECTIVE_REF.match(ref):
                raise PolicyDocumentError(
                    f"{where}: {ref!r} is not an objective reference. Write it as "
                    f"\"service/name\", exactly as the specification spells both."
                )
        rules = tuple(
            _parse_rule(r, f"{where} :: rules[{i}]") for i, r in enumerate(raw.get("rules") or [])
        )
        exemptions = tuple(
            _parse_exemption(e, f"{where} :: exemptions[{i}]", default_exemption)
            for i, e in enumerate(raw.get("exemptions") or [])
        )
        gates.append(Gate(
            unit=unit,
            objectives=tuple(refs),
            combine=combine,
            on_unreadable_budget=direction,
            max_budget_age_seconds=_duration(
                raw.get("max_budget_age", defaults.get("max_budget_age", "15m")),
                f"{where}.max_budget_age",
            ) if "max_budget_age" in raw or "max_budget_age" in defaults else default_age,
            rules=rules,
            exemptions=exemptions,
            description=str(raw.get("description", "")),
        ))

    return Policy(
        name=str(_require(metadata, "name", f"{source} :: metadata")),
        owner=str(metadata.get("owner", "")),
        gates=tuple(gates),
        description=str(metadata.get("description", "")),
        source=source,
    )


def _parse_rule(raw: Any, where: str) -> Rule:
    name = _require(raw, "name", where)
    action = _require(raw, "action", where)
    if action not in ACTION_ORDER or action == "allow":
        raise PolicyDocumentError(
            f"{where}: action is {action!r}, which must be one of "
            f"{', '.join(a for a in ACTION_ORDER if a != 'allow')}. \"allow\" is the absence of a "
            f"triggered rule and is not written down."
        )
    when = raw.get("when") or {}
    if not isinstance(when, dict):
        raise PolicyDocumentError(f"{where}: when must be a mapping")
    remaining = _fraction(when["remaining_below"], f"{where}.when.remaining_below") \
        if "remaining_below" in when else None
    horizon = _duration(when["exhaustion_within"], f"{where}.when.exhaustion_within") \
        if "exhaustion_within" in when else None
    if remaining is None and horizon is None:
        raise PolicyDocumentError(
            f"{where}: the rule has no trigger. A rule with no condition either never applies or "
            f"always does, and which of those it is cannot be read off the document."
        )
    clear = _fraction(raw["clear_above"], f"{where}.clear_above") if "clear_above" in raw else None
    return Rule(
        name=str(name),
        action=str(action),
        remaining_below=remaining,
        exhaustion_within_seconds=horizon,
        clear_above=clear,
        reason=str(raw.get("reason", "")),
    )


def _parse_exemption(raw: Any, where: str, default_duration: Any) -> Exemption:
    name = _require(raw, "class", where)
    approver = _require(raw, "approver", where)
    raw_duration = raw.get("max_duration", default_duration)
    duration = _duration(raw_duration, f"{where}.max_duration") if raw_duration is not None else None
    return Exemption(
        name=str(name),
        approver=str(approver),
        max_duration_seconds=duration,
        description=str(raw.get("description", "")),
    )


# ---------------------------------------------------------------------------
# Observations
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Observation:
    """What something else measured about one objective's budget.

    `burn_rate` is in multiples of the budget's own spend rate, which is the
    only unit comparable across objectives with different targets: an error rate
    of 1% is a tenth of a 10% budget and ten times a 0.1% one. It may be given
    directly or derived from an error rate, and the derivation needs the
    objective, which is why it happens at resolution time rather than here.
    """

    service: str
    name: str
    consumed_fraction: float
    computed_at: datetime
    burn_rate: float | None = None
    error_rate: float | None = None

    @property
    def key(self) -> str:
        return f"{self.service}/{self.name}"

    @property
    def remaining_fraction(self) -> float:
        # Clamped at zero: a budget cannot be less than gone, and a negative
        # remainder propagated into the projection would give a negative time
        # to exhaustion, which reads as a date in the past rather than as
        # "already".
        return max(0.0, 1.0 - self.consumed_fraction)


def parse_observations(doc: Any, source: str) -> list[Observation]:
    if isinstance(doc, dict):
        doc = doc.get("observations", doc)
    if not isinstance(doc, list):
        raise PolicyDocumentError(
            f"{source}: expected a list of observations, or a mapping with an \"observations\" key"
        )
    out: list[Observation] = []
    for index, raw in enumerate(doc):
        where = f"{source} :: observations[{index}]"
        if not isinstance(raw, dict):
            raise PolicyDocumentError(f"{where}: not a mapping")
        objective = raw.get("objective")
        if isinstance(objective, str) and "/" in objective:
            service, _, name = objective.partition("/")
        else:
            service = str(_require(raw, "service", where))
            name = str(_require(raw, "name", where))
        consumed = _require(raw, "consumed_fraction", where)
        if not isinstance(consumed, (int, float)) or isinstance(consumed, bool) or consumed < 0:
            raise PolicyDocumentError(f"{where}: consumed_fraction must be a number at or above 0")
        try:
            computed_at = parse_instant(str(_require(raw, "computed_at", where)), where)
        except ValueError as exc:
            raise PolicyDocumentError(str(exc)) from exc
        out.append(Observation(
            service=service,
            name=name,
            consumed_fraction=float(consumed),
            computed_at=computed_at,
            burn_rate=float(raw["burn_rate"]) if raw.get("burn_rate") is not None else None,
            error_rate=float(raw["error_rate"]) if raw.get("error_rate") is not None else None,
        ))
    return out


# ---------------------------------------------------------------------------
# The arithmetic
# ---------------------------------------------------------------------------

def time_to_exhaustion_seconds(
    remaining_fraction: float, burn_rate: float, window_seconds: int,
) -> float | None:
    """When a sustained burn rate spends what is left. None when it never does.

    `remaining x window / burn_rate`, which is the budget-consumption identity
    read for time rather than for budget. On a rolling window it IGNORES
    replenishment -- spend that ages out of the window during the projection is
    not subtracted -- so the figure is an under-estimate of the time available
    and the control it feeds is conservative. The direction is the point: a
    projection whose bias is unknown cannot be used to stop a release.
    """
    if burn_rate <= 0:
        return None
    if remaining_fraction <= 0:
        return 0.0
    return remaining_fraction * window_seconds / burn_rate


def budget_quantum(budget: Budget) -> float | None:
    """The smallest change in remaining fraction one event can make.

    None when the objective states no event rate, in which case nothing here can
    say how noisy the series is. The validator already refuses a budget smaller
    than a single event; this is the same quantity used for a different purpose.
    """
    if not budget.events or budget.events <= 0:
        return None
    return 1.0 / budget.events


def resolve_burn_rate(observation: Observation, objective: Objective) -> float | None:
    """The observation's burn rate, in multiples of the budget's spend rate."""
    if observation.burn_rate is not None:
        return observation.burn_rate
    if observation.error_rate is None:
        return None
    allowed = objective.allowed
    return observation.error_rate / allowed if allowed > 0 else None


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ObjectiveVerdict:
    """What one objective's budget said, for one gate, at one instant."""

    ref: str
    readable: bool
    action: str
    rule: str | None
    detail: str
    remaining_fraction: float | None = None
    burn_rate: float | None = None
    exhaustion_seconds: float | None = None
    age_seconds: float | None = None


@dataclass
class Decision:
    """The document a deployment pipeline reads, and the one a gate publishes.

    It is also the input to the NEXT evaluation: hysteresis needs the state the
    gate is in, and a stateless evaluator cannot invent it. That is why the
    published parameter is read as well as written, and why Terraform must not
    own its value.
    """

    unit: str
    state: str
    reason: str
    verdicts: list[ObjectiveVerdict] = field(default_factory=list)
    evaluated_at: datetime | None = None
    as_of: datetime | None = None
    sequence: int = 1
    hysteresis_applied: bool = False
    fail_direction: str = ""
    exemptions: tuple[Exemption, ...] = ()
    findings: list[Finding] = field(default_factory=list)

    @property
    def permits_deployment(self) -> bool:
        return self.state not in BLOCKING_ACTIONS

    def as_dict(self) -> dict[str, Any]:
        return {
            "unit": self.unit,
            "state": self.state,
            "permits_deployment": self.permits_deployment,
            "reason": self.reason,
            "sequence": self.sequence,
            "hysteresis_applied": self.hysteresis_applied,
            "fail_direction": self.fail_direction,
            "evaluated_at": self.evaluated_at.isoformat() if self.evaluated_at else None,
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "exemptions": [
                {
                    "class": e.name,
                    "approver": e.approver,
                    "max_duration_seconds": e.max_duration_seconds,
                }
                for e in self.exemptions
            ],
            "objectives": [
                {
                    "objective": v.ref,
                    "readable": v.readable,
                    "action": v.action,
                    "rule": v.rule,
                    "detail": v.detail,
                    "remaining_fraction": round(v.remaining_fraction, 6)
                    if v.remaining_fraction is not None else None,
                    "burn_rate": round(v.burn_rate, 6) if v.burn_rate is not None else None,
                    "exhaustion_seconds": round(v.exhaustion_seconds, 1)
                    if v.exhaustion_seconds is not None else None,
                    "age_seconds": round(v.age_seconds, 1) if v.age_seconds is not None else None,
                }
                for v in self.verdicts
            ],
        }


def _worst(actions: Iterable[str]) -> str:
    collected = list(actions)
    if not collected:
        return "allow"
    return max(collected, key=ACTION_ORDER.index)


def _rule_triggers(
    rule: Rule, remaining: float, exhaustion: float | None,
) -> tuple[bool, str]:
    """Whether a rule's enter condition holds, and the figure that decided it."""
    if rule.remaining_below is not None and remaining < rule.remaining_below:
        return True, (
            f"{remaining:.1%} of the budget remains, below the {rule.remaining_below:.1%} this "
            f"rule triggers at"
        )
    if rule.exhaustion_within_seconds is not None and exhaustion is not None:
        if exhaustion <= rule.exhaustion_within_seconds:
            return True, (
                f"at the observed burn rate the budget is spent in {humanise(exhaustion)}, within "
                f"the {humanise(rule.exhaustion_within_seconds)} this rule triggers at"
            )
    return False, ""


def evaluate_gate(
    gate: Gate,
    objectives: dict[str, Objective],
    observations: dict[str, Observation],
    now: datetime,
    previous: dict[str, Any] | None = None,
) -> Decision:
    """One gate's decision: the most restrictive action any rule reached.

    Hysteresis is applied against `previous`, which is the decision document the
    gate currently publishes. Without it the exit thresholds cannot be used at
    all -- there is no state to stay in -- so the enter thresholds are applied
    alone and the decision records that, rather than reporting a band it did not
    have.
    """
    previous_state = str((previous or {}).get("state", "")) or None
    sequence = int((previous or {}).get("sequence", 0)) + 1
    was_blocking = previous_state in BLOCKING_ACTIONS
    decision = Decision(
        unit=gate.unit,
        state="allow",
        reason="",
        sequence=sequence,
        hysteresis_applied=previous is not None,
        fail_direction=gate.on_unreadable_budget,
        exemptions=gate.exemptions,
        evaluated_at=now,
    )

    ages: list[float] = []
    for ref in gate.objectives:
        objective = objectives.get(ref)
        observation = observations.get(ref)
        if objective is None:
            decision.verdicts.append(ObjectiveVerdict(
                ref=ref, readable=False, action=_unreadable_action(gate), rule=None,
                detail=f"no specification defines {ref}, so this gate's condition cannot be "
                       f"evaluated at all and its failure direction decides every deployment",
            ))
            continue
        if observation is None:
            decision.verdicts.append(ObjectiveVerdict(
                ref=ref, readable=False, action=_unreadable_action(gate), rule=None,
                detail="no budget observation was supplied for this objective",
            ))
            continue
        age = (now - observation.computed_at).total_seconds()
        ages.append(age)
        if age > gate.max_budget_age_seconds:
            decision.verdicts.append(ObjectiveVerdict(
                ref=ref, readable=False, action=_unreadable_action(gate), rule=None,
                detail=f"the figure was computed {humanise(age)} ago, beyond the "
                       f"{humanise(gate.max_budget_age_seconds)} this gate accepts; a decision "
                       f"made from it would be a decision about {humanise(age)} ago",
                age_seconds=age,
            ))
            continue
        if age < 0:
            decision.verdicts.append(ObjectiveVerdict(
                ref=ref, readable=False, action=_unreadable_action(gate), rule=None,
                detail=f"the figure is dated {humanise(-age)} in the future, so either the clock "
                       f"or the figure is wrong and neither can be used",
                age_seconds=age,
            ))
            continue
        decision.verdicts.append(_verdict(
            gate, ref, objective, observation, age, was_blocking,
        ))

    if not gate.objectives:
        decision.state = "allow"
        decision.reason = (
            "this gate governs no objective, so nothing can ever change its state and it permits "
            "every deployment"
        )
        return decision

    # Readability and `combine` are independent. `combine` says how the budgets
    # that WERE read are put together; the failure direction says what an
    # unreadable one means on its own. Folding the second into the first is how a
    # deliberately lenient gate becomes strict for a reason that has nothing to
    # do with its objectives -- so the unreadable verdicts are a floor applied
    # over the combination rather than members of it.
    readable = [v for v in decision.verdicts if v.readable]
    unreadable = [v for v in decision.verdicts if not v.readable]
    if not readable:
        combined = "allow"
    elif gate.combine == "all":
        # Every governed objective must reach an action before the gate takes
        # it, so the decision is the WEAKEST of them: one exhausted budget among
        # several blocks nothing. The audit reports that as P204.
        combined = min((v.action for v in readable), key=ACTION_ORDER.index)
    else:
        combined = _worst(v.action for v in readable)
    decision.state = _worst([combined, *(v.action for v in unreadable)])

    driving = [v for v in decision.verdicts if v.action == decision.state and v.detail]
    decision.reason = "; ".join(f"{v.ref}: {v.detail}" for v in driving) or (
        "no rule triggered on any governed objective"
    )
    if ages:
        decision.as_of = now - timedelta(seconds=max(ages))
    return decision


def _unreadable_action(gate: Gate) -> str:
    """What an unreadable figure means for this gate.

    Closed is the gate's strongest action rather than a freeze unconditionally:
    a gate whose rules only ever notify cannot block, and promoting an
    unreadable figure to a freeze would make the failure path stricter than
    anything the policy actually says.
    """
    return gate.strongest_action if gate.on_unreadable_budget == "closed" else "allow"


def _verdict(
    gate: Gate,
    ref: str,
    objective: Objective,
    observation: Observation,
    age: float,
    was_blocking: bool,
) -> ObjectiveVerdict:
    budget = Budget.of(objective)
    remaining = observation.remaining_fraction
    burn = resolve_burn_rate(observation, objective)
    exhaustion = (
        time_to_exhaustion_seconds(remaining, burn, budget.window_seconds)
        if burn is not None else None
    )

    triggered: list[tuple[Rule, str]] = []
    for rule in gate.rules:
        holds, why = _rule_triggers(rule, remaining, exhaustion)
        if holds:
            triggered.append((rule, why))
            continue
        # Hysteresis: a rule that is not newly triggered stays in force while
        # the gate is already blocking and the exit level has not been reached.
        # Without a previous state there is nothing to stay in, so the branch is
        # skipped rather than guessed.
        #
        # The latch is held per RULE against a gate-level "was blocking", which
        # makes release a staircase rather than a switch: as the remaining
        # fraction rises it passes each blocking rule's exit in turn, so a frozen
        # gate de-escalates to the gentler action before clearing. The
        # alternative -- one exit for the whole gate -- would jump from freeze to
        # allow on a single crossing, which is the shape that unblocks a pipeline
        # at the moment the budget is least able to absorb a bad release.
        if (
            was_blocking
            and rule.blocking
            and rule.clear_above is not None
            and remaining <= rule.clear_above
        ):
            triggered.append((rule, (
                f"{remaining:.1%} remains, which has not reached the {rule.clear_above:.1%} this "
                f"rule clears above, so the gate stays in the state it was already in"
            )))

    if not triggered:
        return ObjectiveVerdict(
            ref=ref, readable=True, action="allow", rule=None,
            detail=(
                f"{remaining:.1%} of the budget remains"
                + (f" and the observed burn rate is {burn:g}x" if burn is not None else "")
                + (f", spending what is left in {humanise(exhaustion)}" if exhaustion else "")
            ),
            remaining_fraction=remaining, burn_rate=burn,
            exhaustion_seconds=exhaustion, age_seconds=age,
        )

    rule, why = max(triggered, key=lambda pair: ACTION_ORDER.index(pair[0].action))
    return ObjectiveVerdict(
        ref=ref, readable=True, action=rule.action, rule=rule.name, detail=why,
        remaining_fraction=remaining, burn_rate=burn,
        exhaustion_seconds=exhaustion, age_seconds=age,
    )


# ---------------------------------------------------------------------------
# Auditing the policy itself
# ---------------------------------------------------------------------------

#: Share of the budget that must still be spendable before the period resets for
#: a freeze on a calendar window to be worth entering. A convention.
REMAINING_AUTHORITY_SHARE = 0.25


def audit_policy(
    policy: Policy, objectives: dict[str, Objective], now: datetime,
) -> list[Finding]:
    """Everything that can be said about a policy without observing anything.

    This is the half of the module that runs in review rather than in a
    pipeline: a policy's faults are properties of the document, and all but one
    of them produce a gate that deploys, reads as configured, and does something
    other than what it says.
    """
    findings: list[Finding] = []
    where = policy.source or policy.name

    if not policy.gates:
        return [error(
            "P100", where,
            "the policy declares no gate, so it enforces nothing. A budget policy with no gate is "
            "a document that reads as a control and is one: every deployment proceeds exactly as "
            "it would with no policy at all, which is the state this file exists to change.",
        )]

    findings.extend(_duplicate_units(policy))
    covered: set[str] = set()

    for gate in policy.gates:
        gate_where = f"{where} :: gate {gate.unit}"
        covered.update(gate.objectives)

        if len(gate.unit) > UNIT_NAME_BUDGET:
            # Unreachable from a document the parser accepted -- the unit
            # grammar caps the length. Kept because the two bounds are set in
            # different places and the next edit to either is where they part.
            findings.append(error(
                "P203", gate_where,
                f"the unit name is {len(gate.unit)} characters against the {UNIT_NAME_BUDGET} "
                f"reserved for it by the configuration's name budget, so the parameter this gate "
                f"publishes to cannot be created.",
            ))

        if not gate.objectives:
            findings.append(error(
                "P102", gate_where,
                "the gate governs no objective, so no observation can ever change its state and it "
                "permits every deployment. A gate in this shape is worse than an absent one: it "
                "appears in the configuration, publishes a parameter, and is read by a pipeline "
                "that concludes the budget is healthy.",
            ))
        if not gate.rules:
            findings.append(error(
                "P101", gate_where,
                "the gate declares no rule, so there is no condition under which it does anything. "
                "The budget is computed, the parameter is published, and the value never leaves the "
                "state it was created in.",
            ))

        findings.extend(_unresolved_references(gate, gate_where, objectives))
        findings.extend(_fail_direction(gate, gate_where))
        findings.extend(_exemptions(gate, gate_where, policy, objectives))
        findings.extend(_rule_ladder(gate, gate_where))
        findings.extend(_hysteresis(gate, gate_where, objectives))
        findings.extend(_combine_note(gate, gate_where))
        findings.extend(_horizons_and_exits(gate, gate_where, objectives, now))
        findings.extend(_lagging_control(gate, gate_where))
        findings.extend(_staleness(gate, gate_where, objectives))

    for ref in sorted(set(objectives) - covered):
        findings.append(warn(
            "P202", f"{where} :: {ref}",
            f"{ref} has a budget and no gate acts on it. The budget is computed, the alerts are "
            f"generated, and exhausting it changes nothing about what ships -- which is the state "
            f"an objective is in when it is a measurement rather than a commitment.",
        ))

    return findings


def _duplicate_units(policy: Policy) -> list[Finding]:
    counts: dict[str, int] = {}
    for gate in policy.gates:
        counts[gate.unit] = counts.get(gate.unit, 0) + 1
    return [
        error(
            "P200", f"{policy.source or policy.name} :: gate {unit}",
            f"{count} gates declare the unit {unit!r}. They publish to one parameter and one "
            f"configuration resource key, so the later definition replaces the earlier one: the "
            f"objectives and exemptions of the first are silently not in force.",
        )
        for unit, count in counts.items() if count > 1
    ]


def _unresolved_references(
    gate: Gate, where: str, objectives: dict[str, Objective],
) -> list[Finding]:
    findings: list[Finding] = []
    for ref in gate.objectives:
        if ref in objectives:
            continue
        consequence = (
            "every deployment is blocked by this gate for as long as the reference stays wrong"
            if gate.on_unreadable_budget == "closed"
            else "this gate permits every deployment for as long as the reference stays wrong"
        )
        findings.append(error(
            "P201", where,
            f"the gate governs {ref!r} and no specification defines it. The condition can never be "
            f"evaluated, so the failure direction decides permanently: {consequence}. A mistyped "
            f"objective name is therefore not a dormant fault -- it is the gate's whole behaviour, "
            f"with the document still reading as configured.",
        ))
    return findings


def _fail_direction(gate: Gate, where: str) -> list[Finding]:
    findings: list[Finding] = []
    if gate.on_unreadable_budget == "closed":
        findings.append(note(
            "P400", where,
            f"an unreadable budget figure blocks this gate at {gate.strongest_action!r}, the "
            f"strongest action its rules declare. The cost is stated rather than discovered: a "
            f"failure in whatever computes consumption -- the metric backend, the query, the job "
            f"-- stops deployments including the change that would fix it, and the gate cannot "
            f"tell that case apart from a healthy service nobody measured. The staleness allowance "
            f"of {humanise(gate.max_budget_age_seconds)} is how long that takes to bite.",
        ))
    else:
        findings.append(note(
            "P400", where,
            "an unreadable budget figure leaves this gate open. The cost is that the policy is "
            "disabled by exactly the outage it exists for: if consumption stops being computed "
            "during an incident, the gate reports that deployments are permitted and gives the "
            "same answer it gives when the budget is healthy. Nothing downstream can distinguish "
            "them, which is why the decision document carries the readability of every objective "
            "rather than only the state.",
        ))
    if gate.strongest_action not in BLOCKING_ACTIONS and gate.rules:
        findings.append(warn(
            "P401", where,
            f"the strongest action any rule here takes is {gate.strongest_action!r}, which stops "
            f"nothing. The gate is advisory, so its failure direction "
            f"({gate.on_unreadable_budget!r}) has no effect on any deployment although it reads as "
            f"a decision about one. A policy in this shape is a reporting mechanism, which is a "
            f"legitimate thing to have and a different thing from the one the document's vocabulary "
            f"suggests.",
        ))
    return findings


def _exemptions(
    gate: Gate, where: str, policy: Policy, objectives: dict[str, Objective],
) -> list[Finding]:
    findings: list[Finding] = []
    seen: dict[str, int] = {}
    for exemption in gate.exemptions:
        seen[exemption.name] = seen.get(exemption.name, 0) + 1
    for name, count in seen.items():
        if count > 1:
            findings.append(error(
                "P405", f"{where} :: exemption {name}",
                f"the class {name!r} is declared {count} times, with no rule saying which approver "
                f"and which duration apply. Whichever the reader happens to take depends on "
                f"document order, and the two are not interchangeable because one of them is the "
                f"longer.",
            ))

    governed_window = max(
        (objectives[ref].window.seconds for ref in gate.objectives if ref in objectives),
        default=None,
    )

    for exemption in gate.exemptions:
        exemption_where = f"{where} :: exemption {exemption.name}"
        if exemption.max_duration_seconds is None:
            findings.append(error(
                "P402", exemption_where,
                "the exemption has no maximum duration. An exemption is granted while an incident "
                "is in progress and revoked by somebody remembering to revoke it, so one without "
                "an expiry is the normal way a gate ends up permanently off with its document "
                "still saying it is on. Set a duration even if it is generous: a dated exemption "
                "is reviewed, an undated one is forgotten.",
            ))
        elif governed_window is not None and exemption.max_duration_seconds >= governed_window:
            findings.append(warn(
                "P403", exemption_where,
                f"the exemption may run for {humanise(exemption.max_duration_seconds)}, which is "
                f"at least the {humanise(governed_window)} window of the objective it exempts "
                f"from. It therefore outlives any freeze it is granted against -- by the time it "
                f"expires the spend that caused the freeze has left the window anyway -- so it is "
                f"a permanent disablement of this gate with a date written on it.",
            ))
        if policy.owner and exemption.approver == policy.owner:
            findings.append(note(
                "P404", exemption_where,
                f"the approver is {exemption.approver!r}, the team whose deployments this gate "
                f"stops. Self-approval is often the correct arrangement -- the team holds the "
                f"budget and the consequences -- but it means the exemption is a record of a "
                f"decision rather than a check on one, and nothing here or downstream treats it as "
                f"more than that.",
            ))

    if not gate.exemptions and gate.strongest_action == "freeze":
        findings.append(warn(
            "P406", where,
            "the gate can freeze and declares no exempt class, so once it is frozen nothing ships "
            "-- including the change that would stop the budget being spent. The exit conditions "
            "that remain are the window advancing and an edit to this document, and an edit made "
            "under incident pressure is the least reviewed change in the system.",
        ))
    return findings


def _rule_ladder(gate: Gate, where: str) -> list[Finding]:
    """A weaker action that can never be the gate's state, because a stronger one gets there first.

    The same shape as the generator's shadowed tier, in a different place: a
    rule is only reachable if there is a region of the budget where it is the
    most restrictive thing that applies. Only like triggers are compared --
    level against level, horizon against horizon -- because a level and a
    horizon are not ordered with respect to each other without a burn rate, and
    inventing one to make the comparison work would report a fault that depends
    on the invented figure.
    """
    findings: list[Finding] = []
    for weaker in gate.rules:
        for stronger in gate.rules:
            if stronger is weaker:
                continue
            if ACTION_ORDER.index(stronger.action) <= ACTION_ORDER.index(weaker.action):
                continue
            shadowed_on = None
            if weaker.remaining_below is not None and stronger.remaining_below is not None:
                if stronger.remaining_below >= weaker.remaining_below:
                    shadowed_on = (
                        f"{stronger.name!r} triggers at {stronger.remaining_below:.1%} remaining, "
                        f"at or before this rule's {weaker.remaining_below:.1%}"
                    )
            if (
                shadowed_on is None
                and weaker.exhaustion_within_seconds is not None
                and stronger.exhaustion_within_seconds is not None
                and stronger.exhaustion_within_seconds >= weaker.exhaustion_within_seconds
            ):
                shadowed_on = (
                    f"{stronger.name!r} triggers at an exhaustion horizon of "
                    f"{humanise(stronger.exhaustion_within_seconds)}, at or before this rule's "
                    f"{humanise(weaker.exhaustion_within_seconds)}"
                )
            if shadowed_on is None:
                continue
            findings.append(warn(
                "P305", f"{where} :: rule {weaker.name}",
                f"{shadowed_on}, and takes the stronger action ({stronger.action!r} against "
                f"{weaker.action!r}). A decision is the most restrictive action that applies, so "
                f"there is no budget state in which this rule is the gate's state: it is never "
                f"read by anything. A ladder works when the gentler action triggers EARLIER -- at "
                f"a higher remaining fraction or a longer horizon -- not later at a weaker one.",
            ))
            break
    return findings


def _hysteresis(gate: Gate, where: str, objectives: dict[str, Objective]) -> list[Finding]:
    findings: list[Finding] = []
    quanta = [
        q for q in (
            budget_quantum(Budget.of(objectives[ref]))
            for ref in gate.objectives if ref in objectives
        ) if q is not None
    ]
    quantum = max(quanta) if quanta else None

    for rule in gate.rules:
        rule_where = f"{where} :: rule {rule.name}"
        if not rule.blocking:
            continue
        if rule.clear_above is None:
            findings.append(error(
                "P302", rule_where,
                "the rule blocks deployments and declares no clear_above, so it has no exit "
                "distinct from its entry. Consumption computed over a trailing window is a noisy "
                "series, so a single threshold is crossed repeatedly in both directions, and every "
                "crossing here blocks or unblocks a pipeline. Set clear_above above the trigger: "
                "the gap is what stops the gate flapping on individual events.",
            ))
            continue
        if rule.remaining_below is None:
            findings.append(note(
                "P309", rule_where,
                f"the rule enters on a projection and leaves on a level ({rule.clear_above:.1%} "
                f"remaining), which are deliberately different units. A projection recovers the "
                f"instant the burn stops, so a projection-shaped exit would unblock deployments "
                f"while the budget was still nearly gone -- the burn has ended, the spend has not "
                f"been returned. The level is the quantity that actually recovers, and it "
                f"recovers only as the window advances or resets.",
            ))
            continue
        if rule.clear_above <= rule.remaining_below:
            findings.append(error(
                "P302", rule_where,
                f"clear_above ({rule.clear_above:.1%}) is at or below the trigger "
                f"({rule.remaining_below:.1%}), so the rule re-triggers at the moment it clears. "
                f"The gate does not hold a state, it oscillates between two on the same figure.",
            ))
            continue
        band = rule.clear_above - rule.remaining_below
        if quantum is not None and band < MIN_HYSTERESIS_EVENTS * quantum:
            findings.append(warn(
                "P302", rule_where,
                f"the band between the trigger and the exit is {band:.2%} of the budget, which is "
                f"{band / quantum:.1f} events at the stated event rate. One event moves the "
                f"remaining fraction by {format_rate(quantum)}, so a band this narrow is crossed by "
                f"ordinary variation rather than by the service's behaviour, and each crossing is "
                f"a pipeline that stops or starts. Widening the band or lengthening the "
                f"consumption window both fix it; only the first is in this document.",
            ))
    return findings


def _combine_note(gate: Gate, where: str) -> list[Finding]:
    if len(gate.objectives) < 2:
        return []
    if gate.combine == "any":
        return [note(
            "P204", where,
            f"{len(gate.objectives)} objectives, combined with \"any\": the gate takes the most "
            f"restrictive action any one of them reaches, so adding an objective can only make it "
            f"stricter. The consequence worth knowing is that an objective which IMPROVES during "
            f"an incident can still be the one holding the gate shut -- a latency objective "
            f"measured only over served requests gets better when requests fail -- so read the "
            f"decision document's per-objective verdicts rather than its state when a freeze looks "
            f"wrong.",
        )]
    return [warn(
        "P204", where,
        f"{len(gate.objectives)} objectives, combined with \"all\": every one of them must reach "
        f"an action before the gate takes it, so the decision is the WEAKEST of them and a single "
        f"exhausted budget blocks nothing. That is a deliberate choice in a gate covering "
        f"objectives of different importance, and a silent disabling of the strictest objective in "
        f"one covering objectives of the same importance.",
    )]


def _horizons_and_exits(
    gate: Gate, where: str, objectives: dict[str, Objective], now: datetime,
) -> list[Finding]:
    findings: list[Finding] = []
    for ref in gate.objectives:
        objective = objectives.get(ref)
        if objective is None:
            continue
        budget = Budget.of(objective)
        window = objective.window
        ref_where = f"{where} :: {ref}"

        if window.kind == "rolling":
            bias = (
                "The expression ignores replenishment: spend that ages out of the rolling window "
                "during the projection is not subtracted, so the figure UNDER-estimates the time "
                "available and the control it feeds triggers earlier than strictly necessary."
            )
        else:
            bias = (
                "Nothing ages out of a calendar window, so the consumption the expression assumes "
                "is exact -- but the window ENDS, and the projection does not know that. Read it "
                "against the time left in the period: a projection reaching past the reset "
                "describes a budget that will no longer exist."
            )
        findings.append(note(
            "P300", ref_where,
            f"a sustained burn rate B spends what is left in "
            f"`remaining x {humanise(window.seconds)} / B`. {bias} The direction of the error is "
            f"what makes the figure usable -- a projection whose bias is unknown cannot be the "
            f"reason a release is stopped.",
        ))

        quantum = budget_quantum(budget)
        if quantum is not None:
            findings.append(note(
                "P308", ref_where,
                f"the budget is about {budget.events:,.0f} events, so one failed event moves the "
                f"remaining fraction by {format_rate(quantum)}. That is the resolution of every "
                f"threshold "
                f"in this gate: a figure quoted to more precision than that is quoting the "
                f"estimator rather than the service.",
            ))

        for rule in gate.rules:
            if rule.exhaustion_within_seconds is None:
                continue
            if rule.exhaustion_within_seconds > window.seconds:
                findings.append(warn(
                    "P306", f"{where} :: rule {rule.name}",
                    f"the horizon is {humanise(rule.exhaustion_within_seconds)} against a "
                    f"{humanise(window.seconds)} objective window. On a rolling window the spend "
                    f"driving the projection has left the window before the horizon ends, so the "
                    f"projection extends past its own evidence; on a calendar window the horizon "
                    f"reaches past the reset, where the budget this rule is protecting no longer "
                    f"exists. A horizon is only meaningful inside the window it is computed from.",
                ))

        if window.kind == "rolling":
            findings.append(note(
                "P304", ref_where,
                f"the freeze this gate can enter is ended by the window advancing, not by a fix. "
                f"Spend leaves a rolling window {humanise(window.seconds)} after it happened, so "
                f"the exit date is set by WHEN the budget was spent and nothing done afterwards "
                f"moves it. Two consequences: the freeze clears on its own, which is the humane "
                f"property of a rolling window; and 'we have shipped the fix, please unblock us' "
                f"is not an exit condition, because repairing the service does not return spent "
                f"budget.",
            ))
            continue

        remaining_period, zone_reason = period_end(now, window.label, window.timezone or "UTC")
        seconds_left = max(0.0, (remaining_period - now).total_seconds())
        if zone_reason:
            findings.append(warn(
                "P307", ref_where,
                f"the period boundary was computed in UTC because {zone_reason}. A calendar budget "
                f"resets at a wall-clock instant, so a boundary in the wrong zone moves the reset "
                f"by up to a day and every figure derived from it stays plausible. Install the "
                f"zone data or state the objective's window in UTC.",
            ))
        spendable = min(1.0, max(0.0, seconds_left / window.seconds))
        detail = (
            f"{humanise(seconds_left)} of the calendar {window.label} remain, so at most "
            f"{spendable:.0%} of the budget can still be spent before it resets"
        )
        if spendable < REMAINING_AUTHORITY_SHARE:
            findings.append(warn(
                "P307", ref_where,
                f"{detail}. A freeze entered now therefore protects very little and is lifted by "
                f"the reset rather than by anything anyone decides, which is the point in a "
                f"calendar period where a budget policy has the least authority and the most "
                f"appearance of it. The other end of the same property is worse: an incident on the "
                f"first of the period can freeze the whole of it.",
            ))
        else:
            findings.append(note(
                "P304", ref_where,
                f"the freeze this gate can enter is ended by the period boundary, not by a fix: a "
                f"calendar window returns no budget until it resets. {detail}. An incident early "
                f"in the period can therefore hold the gate shut for the rest of it, and the only "
                f"exits that exist are the boundary, an enumerated exemption, or changing the "
                f"objective.",
            ))
    return findings


def _lagging_control(gate: Gate, where: str) -> list[Finding]:
    """A gate that only ever reads the level, and what it gets wrong in both directions."""
    level_only = [r for r in gate.rules if r.exhaustion_within_seconds is None]
    if not gate.rules or len(level_only) != len(gate.rules):
        return []
    blocking = [r for r in level_only if r.blocking]
    if not blocking:
        return []
    strictest = min(blocking, key=lambda r: r.remaining_below or 1.0)
    level = strictest.remaining_below or 0.0
    return [warn(
        "P301", where,
        f"every rule here triggers on the remaining level alone, which makes this a lagging "
        f"control and wrong in both directions. A service holding steady at {level:.0%} remaining "
        f"with no burn at all is blocked by {strictest.name!r} although it will never exhaust its "
        f"budget; a service at twice that level burning fast is permitted although it will exhaust "
        f"it within days. The level is a floor worth having -- it is the one figure that cannot be "
        f"argued with -- but the condition that decides whether stopping deployments helps is the "
        f"projection, so a gate wants at least one rule carrying `exhaustion_within`.",
    )]


def _staleness(gate: Gate, where: str, objectives: dict[str, Objective]) -> list[Finding]:
    """What the staleness allowance does to the hysteresis band.

    A gate reads the most recent figure it is given, and that figure may be as
    much as `max_budget_age` old. During that interval the remaining fraction
    moves `burn_rate x age / window`, so every threshold in the gate is fuzzed by
    that much in both directions. The band between a trigger and its exit is
    therefore only a band if it is wide compared with the fuzz; below that, the
    two thresholds are one threshold with extra arithmetic in front of it.

    The burn rate used is the highest the governed objectives' own alert tiers
    anticipate. It is taken from the specification rather than invented here,
    because the fastest burn a team has written an alert for is the fastest burn
    they have said they expect, and using a figure of this module's own choosing
    would report a fault that depends on the choice.
    """
    rates = [
        tier.burn_rate
        for ref in gate.objectives if ref in objectives
        for tier in objectives[ref].tiers
    ]
    windows = [objectives[ref].window.seconds for ref in gate.objectives if ref in objectives]
    if not rates or not windows:
        return []
    fastest = max(rates)
    window = min(windows)
    movement = fastest * gate.max_budget_age_seconds / window
    bands = [
        (rule, rule.clear_above - rule.remaining_below)
        for rule in gate.rules
        if rule.blocking and rule.clear_above is not None and rule.remaining_below is not None
        and rule.clear_above > rule.remaining_below
    ]
    if not bands:
        return []

    findings: list[Finding] = []
    for rule, band in bands:
        if movement > STALENESS_BAND_SHARE * band:
            findings.append(warn(
                "P303", f"{where} :: rule {rule.name}",
                f"the gate accepts a figure up to {humanise(gate.max_budget_age_seconds)} old, and "
                f"at the fastest burn rate these objectives alert on ({fastest:g}x) the remaining "
                f"fraction moves {movement:.2%} in that time. The band between this rule's trigger "
                f"and its exit is {band:.2%}, so the staleness allowance consumes "
                f"{movement / band:.0%} of it and the two thresholds are not distinguishable in "
                f"practice. Shorten max_budget_age or widen the band; the hysteresis is only worth "
                f"what the freshness of the figure allows.",
            ))
    if findings:
        return findings
    narrowest = min(band for _, band in bands)
    return [note(
        "P303", where,
        f"a figure up to {humanise(gate.max_budget_age_seconds)} old moves the remaining fraction "
        f"by {movement:.2%} at the fastest burn rate these objectives alert on ({fastest:g}x), "
        f"against a narrowest hysteresis band of {narrowest:.2%}. The allowance is "
        f"{movement / narrowest:.0%} of the band, so the band still separates the trigger from the "
        f"exit -- which is the condition under which hysteresis does anything at all.",
    )]


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def collect_objectives(target: Path, pattern: str) -> dict[str, Objective]:
    objectives: dict[str, Objective] = {}
    for path in discover(target, pattern):
        doc = load_document(path)
        if doc is None:
            print(f"error: {path}: document is empty", file=sys.stderr)
            raise SystemExit(2)
        for objective in objectives_in(doc, path.name):
            objectives[f"{objective.service}/{objective.name}"] = objective
    return objectives


def read_policy(path: Path) -> Policy:
    doc = load_document(path)
    if doc is None:
        print(f"error: {path}: document is empty", file=sys.stderr)
        raise SystemExit(2)
    try:
        return parse_policy(doc, path.name)
    except PolicyDocumentError as exc:
        # Exit 2, not 1: a document that could not be read is a different
        # outcome from one that was read and found wanting, and a pipeline step
        # unable to tell them apart reports a healthy policy as a broken one.
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def read_observations(path: Path) -> dict[str, Observation]:
    doc = load_document(path)
    if doc is None:
        print(f"error: {path}: document is empty", file=sys.stderr)
        raise SystemExit(2)
    try:
        parsed = parse_observations(doc, path.name)
    except PolicyDocumentError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    out: dict[str, Observation] = {}
    for observation in parsed:
        if observation.key in out:
            print(
                f"error: {path.name}: two observations for {observation.key}; which one is current "
                f"cannot be read off the document",
                file=sys.stderr,
            )
            raise SystemExit(2)
        out[observation.key] = observation
    return out


def render_decision(decision: Decision) -> str:
    lines = [
        f"{decision.unit}: {decision.state.upper()}"
        f"  ({'deployments permitted' if decision.permits_deployment else 'deployments blocked'})",
        f"  reason      {decision.reason}",
        f"  sequence    {decision.sequence}"
        f"{'' if decision.hysteresis_applied else '  (no previous state: exit thresholds not applied)'}",
        f"  fail        {decision.fail_direction} on an unreadable budget",
    ]
    for verdict in decision.verdicts:
        state = verdict.action if verdict.readable else f"{verdict.action} (budget unreadable)"
        lines.append(f"  {verdict.ref:40s} {state}")
        lines.append(f"    {verdict.detail}")
    if decision.exemptions:
        classes = ", ".join(
            f"{e.name} (approver {e.approver}, "
            f"{humanise(e.max_duration_seconds) if e.max_duration_seconds else 'no expiry'})"
            for e in decision.exemptions
        )
        lines.append(f"  exempt      {classes}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit an error-budget policy, and decide what it permits.",
        epilog="Exit status: 0 clean, 1 findings, 2 input could not be read, 3 the gate named by "
               "--enforce blocks this deployment. 3 is distinct on purpose: a gate that returns the "
               "same status when it blocks and when it breaks is a gate that fails open the first "
               "time somebody appends `|| true`.",
    )
    parser.add_argument("target", nargs="?", default=Path("specs"), type=Path,
                        help="specification file, or directory to search (default: specs)")
    parser.add_argument("--rules", type=Path, default=Path("policy/rules.yaml"),
                        help="policy document (default: policy/rules.yaml)")
    parser.add_argument("--pattern", default="*.yaml", help="glob used when target is a directory")
    parser.add_argument("--observations", type=Path, default=None,
                        help="observed budget consumption; omitted, only the policy is audited")
    parser.add_argument("--previous", type=Path, default=None,
                        help="the decision document a gate currently publishes, so exit thresholds "
                             "can be applied; omitted, only entry thresholds are")
    parser.add_argument("--now", default=None,
                        help="evaluate as at this ISO-8601 instant instead of the current time")
    parser.add_argument("--gate", default=None, help="restrict output to one unit")
    parser.add_argument("--enforce", default=None, metavar="UNIT",
                        help="exit 3 unless UNIT's gate permits a deployment")
    parser.add_argument("--exemption", default=None, metavar="CLASS",
                        help="claim an exempt class for --enforce")
    parser.add_argument("--approved-by", default=None, metavar="WHO",
                        help="who approved the claimed exemption; recorded, not authenticated")
    parser.add_argument("--json", dest="as_json", action="store_true",
                        help="emit the decision documents as JSON")
    parser.add_argument("--strict", action="store_true", help="treat warnings as failures")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    try:
        now = parse_instant(args.now, "--now") if args.now else datetime.now(timezone.utc)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    objectives = collect_objectives(args.target, args.pattern)
    policy = read_policy(args.rules)
    observations = read_observations(args.observations) if args.observations else {}
    previous: dict[str, Any] | None = None
    if args.previous:
        loaded = load_document(args.previous)
        if not isinstance(loaded, dict):
            print(f"error: {args.previous}: not a decision document", file=sys.stderr)
            return 2
        previous = loaded

    findings = audit_policy(policy, objectives, now)
    gates = [g for g in policy.gates if args.gate in (None, g.unit)]
    if args.gate and not gates:
        print(f"error: no gate named {args.gate!r} in {args.rules}", file=sys.stderr)
        return 2

    decisions: list[Decision] = []
    if args.observations:
        for gate in gates:
            gate_previous = previous if (previous or {}).get("unit") == gate.unit else None
            decisions.append(evaluate_gate(gate, objectives, observations, now, gate_previous))

    if args.as_json:
        print(json.dumps({
            "policy": policy.name,
            "evaluated_at": now.isoformat(),
            "decisions": [d.as_dict() for d in decisions],
            "findings": [
                {"code": f.code, "severity": f.severity, "where": f.where, "message": f.message}
                for f in findings
            ],
        }, indent=2))
    else:
        for decision in decisions:
            print(render_decision(decision))
            print()
        if not args.observations:
            print(
                f"{len(gates)} gate(s) audited against {len(objectives)} objective(s). No "
                f"observations were supplied, so no decision was made: the policy was checked, not "
                f"applied."
            )

    for finding in findings:
        if finding.severity != "note" or args.verbose:
            print(finding.render(), file=sys.stderr)

    errors = sum(1 for f in findings if f.severity == "error")
    warnings = sum(1 for f in findings if f.severity == "warning")
    print(
        f"\n{len(policy.gates)} gate(s), {errors} error(s), {warnings} warning(s)",
        file=sys.stderr,
    )

    if args.enforce:
        blocked = _enforce(args, decisions, errors)
        if blocked is not None:
            return blocked
    if errors:
        return 1
    return 1 if (args.strict and warnings) else 0


def _enforce(args: argparse.Namespace, decisions: list[Decision], errors: int) -> int | None:
    """Resolve `--enforce` into an exit status, or None to fall through."""
    matching = [d for d in decisions if d.unit == args.enforce]
    if not matching:
        # No decision for the named unit means no observation reached it, which
        # is the unreadable case and not a reason to allow the deployment. The
        # gate's own failure direction cannot be consulted here because the gate
        # was never evaluated, so this refuses and says why.
        print(
            f"error: --enforce {args.enforce!r} produced no decision. Supply --observations, and "
            f"check the unit is declared in the policy; a deployment is not permitted on the "
            f"strength of a gate that was not evaluated.",
            file=sys.stderr,
        )
        return 2
    decision = matching[0]
    if decision.permits_deployment:
        print(f"{decision.unit}: {decision.state} — deployment permitted", file=sys.stderr)
        return None if not errors else 1
    if args.exemption:
        claimed = {e.name: e for e in decision.exemptions}
        exemption = claimed.get(args.exemption)
        if exemption is None:
            print(
                f"error: {args.exemption!r} is not an exempt class on {decision.unit}; declared: "
                f"{', '.join(sorted(claimed)) or 'none'}",
                file=sys.stderr,
            )
            return 3
        if not args.approved_by:
            print(
                f"error: the {exemption.name!r} exemption requires --approved-by. The approver of "
                f"record is {exemption.approver!r}.",
                file=sys.stderr,
            )
            return 3
        if args.approved_by != exemption.approver:
            print(
                f"error: {args.approved_by!r} is not the approver of record for "
                f"{exemption.name!r}, which is {exemption.approver!r}.",
                file=sys.stderr,
            )
            return 3
        # Recorded, not verified. Nothing here can authenticate the name on the
        # command line, and pretending otherwise would be the more dangerous of
        # the two options: a gate that claims to check an approval is trusted
        # further than one that states it only records the claim.
        limit = (
            humanise(exemption.max_duration_seconds)
            if exemption.max_duration_seconds else "an unbounded period"
        )
        print(
            f"{decision.unit}: {decision.state} — permitted under the {exemption.name!r} "
            f"exemption, approved by {args.approved_by} for at most {limit}. This tool records the "
            f"claim and does not authenticate it; authentication belongs to whatever runs the "
            f"pipeline.",
            file=sys.stderr,
        )
        return None if not errors else 1
    print(
        f"{decision.unit}: {decision.state} — deployment blocked. {decision.reason}",
        file=sys.stderr,
    )
    return 3


if __name__ == "__main__":  # pragma: no cover - thin wrapper
    raise SystemExit(main())
