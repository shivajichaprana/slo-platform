#!/usr/bin/env python3
"""Validate objective specifications: structure first, then arithmetic.

The schema settles shape, vocabulary and per-field bounds. It cannot settle
anything that relates two fields to each other, and nearly everything that
makes an objective wrong in practice is exactly that:

  * a burn-rate tier can be arithmetically unable to fire, because firing
    requires an error rate above 100%;
  * an alert window can be so long a fraction of the objective window that the
    budget is gone before the alert arrives;
  * an objective can be finer than the signal it is measured from, so the
    smallest observable failure already breaches it.

Each finding carries a code, a severity and the arithmetic that produced it,
so a report can be argued with rather than just obeyed.

Exit status: 0 no errors, 1 errors found (or warnings with --strict),
2 usage or input/output failure.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Sequence

try:
    import yaml
except ImportError:  # pragma: no cover - reported, not raised
    print("error: PyYAML is required (pip install pyyaml)", file=sys.stderr)
    raise SystemExit(2)

try:
    from jsonschema import Draft202012Validator
except ImportError:  # pragma: no cover - reported, not raised
    print("error: jsonschema>=4.18 is required (pip install 'jsonschema>=4.18')", file=sys.stderr)
    raise SystemExit(2)

LOG = logging.getLogger("validate-specs")

DURATION = re.compile(r"^([1-9][0-9]*)(m|h|d)$")
UNIT_MINUTES = {"m": 1, "h": 60, "d": 1440}

# A calendar period is not a duration: a month is 28, 29, 30 or 31 days. Budget
# arithmetic needs one number, so a nominal length is used and every figure
# derived from it is reported as nominal rather than as fact.
NOMINAL_CALENDAR_MINUTES = {
    "week": 7 * 1440,
    "month": 30 * 1440,
    "quarter": 91 * 1440,
}

# The deployment's own name prefix and the environment share a 64-character
# ceiling with the objective's identity, which leaves 32. The schema bounds
# `service` and `name` individually; only their sum matters, and only this
# check can see it — the same shape as the name-budget precondition in the
# Terraform configuration, for the same reason.
OBJECTIVE_KEY_BUDGET = 32

SEVERITY_ORDER = {"error": 0, "warning": 1}


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    where: str
    message: str

    def render(self) -> str:
        return f"{self.severity.upper():7s} {self.code}  {self.where}\n          {self.message}"


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    facts: list[dict[str, Any]] = field(default_factory=list)

    def error(self, code: str, where: str, message: str) -> None:
        self.findings.append(Finding(code, "error", where, message))

    def warn(self, code: str, where: str, message: str) -> None:
        self.findings.append(Finding(code, "warning", where, message))

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "warning"]


def parse_duration_minutes(text: str) -> int:
    """Minutes in a `<n><m|h|d>` duration. Raises ValueError on anything else."""
    match = DURATION.match(text)
    if not match:
        raise ValueError(f"not a duration: {text!r}")
    return int(match.group(1)) * UNIT_MINUTES[match.group(2)]


def window_minutes(window: dict[str, Any]) -> tuple[int, bool]:
    """Length of an objective window in minutes, and whether it is nominal."""
    if window["kind"] == "rolling":
        return parse_duration_minutes(window["duration"]), False
    return NOMINAL_CALENDAR_MINUTES[window["period"]], True


def humanise(minutes: float) -> str:
    if minutes < 60:
        return f"{minutes:.0f}m"
    if minutes < 1440:
        return f"{minutes / 60:.1f}h"
    return f"{minutes / 1440:.1f}d"


def check_objective(doc_label: str, service: str, obj: dict[str, Any], report: Report) -> None:
    name = obj["name"]
    where = f"{doc_label} :: {service}/{name}"
    target = float(obj["objective"])
    allowed = 1.0 - target
    win_minutes, nominal = window_minutes(obj["window"])
    sampling = obj["sli"].get("sampling")
    rate_per_hour = float(sampling["expected_events_per_hour"]) if sampling else None

    key = f"{service}-{name}"
    if len(key) > OBJECTIVE_KEY_BUDGET:
        report.error(
            "E202", where,
            f"service and name together are {len(key)} characters ('{key}'), over the "
            f"{OBJECTIVE_KEY_BUDGET}-character identity budget. Every deployed name is derived from "
            f"the deployment prefix and this identity against a 64-character ceiling, "
            f"so the overflow appears only when a resource is created. Shorten the objective name.",
        )

    budget_minutes = allowed * win_minutes
    events_in_window = rate_per_hour * win_minutes / 60 if rate_per_hour else None
    budget_events = allowed * events_in_window if events_in_window else None

    fact: dict[str, Any] = {
        "document": doc_label,
        "service": service,
        "objective": name,
        "target": target,
        "window_minutes": win_minutes,
        "window_nominal": nominal,
        "budget_minutes": round(budget_minutes, 3),
        "budget_events": round(budget_events, 1) if budget_events is not None else None,
        "tiers": [],
    }

    # An objective finer than the signal: a single failure already breaches it.
    if budget_events is not None and budget_events < 1:
        report.warn(
            "W409", where,
            f"the whole budget is {budget_events:.2f} events: at {rate_per_hour:g} events/hour this "
            f"window holds about {events_in_window:,.0f} events, and {allowed:.2%} of that is less than "
            f"one. A single failure breaches the objective, so it is either unachievable or it is "
            f"measuring something other than what it claims.",
        )

    tiers: Sequence[dict[str, Any]] = obj["alerting"]["tiers"]
    seen_names: set[str] = set()
    seen_rates: dict[float, str] = {}

    for tier in tiers:
        t_name = tier["name"]
        t_where = f"{where} :: tier {t_name}"
        if t_name in seen_names:
            report.error("E303", t_where, f"duplicate tier name {t_name!r} within one objective.")
        seen_names.add(t_name)

        burn = float(tier["burn_rate"])
        long_m = parse_duration_minutes(tier["long_window"])
        short_m = parse_duration_minutes(tier["short_window"])
        threshold = burn * allowed  # error rate the tier fires at

        if burn in seen_rates:
            report.warn(
                "W406", t_where,
                f"burn rate {burn:g} is already used by tier {seen_rates[burn]!r}. Two tiers at the "
                f"same threshold differ only in window, so the shorter always fires first and the "
                f"longer one adds nothing but a second notification.",
            )
        seen_rates.setdefault(burn, t_name)

        # Unreachable: firing would need an error rate above 100%.
        if threshold > 1.0:
            report.error(
                "E300", t_where,
                f"unreachable: firing needs an error rate of {threshold:.1%} over {tier['long_window']} "
                f"(burn rate {burn:g} x a {allowed:.2%} budget), and the error rate cannot exceed 100%. "
                f"This tier is accepted everywhere, reports healthy for ever and can never fire. The "
                f"highest reachable burn rate for an objective of {target} is {1 / allowed:.1f}.",
            )
        elif threshold > 0.5:
            report.warn(
                "W400", t_where,
                f"firing needs {threshold:.1%} of all events to fail, so this detects a near-total "
                f"outage rather than a fast burn. Something else will have noticed first.",
            )

        if long_m >= win_minutes:
            report.error(
                "E301", t_where,
                f"long window {tier['long_window']} ({humanise(long_m)}) is not shorter than the "
                f"objective window ({humanise(win_minutes)}{' nominal' if nominal else ''}). An alert "
                f"measured over the objective's own window is the objective, reported once it is "
                f"already missed.",
            )
        if short_m >= long_m:
            report.error(
                "E302", t_where,
                f"short window {tier['short_window']} is not shorter than long window "
                f"{tier['long_window']}. The short window exists to release the alert once the burn "
                f"stops; at this length it cannot.",
            )
        else:
            ratio = short_m / long_m
            if not (1 / 20) <= ratio <= (1 / 6):
                report.warn(
                    "W408", t_where,
                    f"short window is 1/{long_m / short_m:.1f} of the long window; the convention is "
                    f"about 1/12. Shorter than 1/20 makes the tier flap on a brief spike, longer than "
                    f"1/6 keeps it latched well after the burn has stopped.",
                )

        consumed = burn * long_m / win_minutes
        if consumed > 0.5:
            report.warn(
                "W401", t_where,
                f"by the time this tier fires, {consumed:.0%} of the budget is spent "
                f"(burn rate {burn:g} sustained for {tier['long_window']} out of "
                f"{humanise(win_minutes)}). It reports the budget's end, not its burn.",
            )

        floor = None
        if rate_per_hour:
            events_in_short = rate_per_hour * short_m / 60
            floor = 1 / events_in_short if events_in_short else None
            if floor and threshold < floor:
                report.warn(
                    "W402", t_where,
                    f"the threshold is {threshold:.3%} but the short window holds only about "
                    f"{events_in_short:,.0f} events, so the finest error rate it can express is "
                    f"{floor:.3%}. One failed event already exceeds the threshold, which makes every "
                    f"tier above this one decoration: they all fire on the same single failure.",
                )

        if tier["notify"] == "page" and obj["window"]["kind"] == "calendar":
            report.warn(
                "W410", t_where,
                "pages on a calendar-window objective: the budget resets at the period boundary, so a "
                "page can arrive for a budget that is about to be forgiven, or be suppressed by a "
                "reset while the service is still failing.",
            )

        fact["tiers"].append({
            "name": t_name,
            "burn_rate": burn,
            "long_window_minutes": long_m,
            "short_window_minutes": short_m,
            "fires_at_error_rate": round(threshold, 6),
            "budget_consumed_when_firing": round(consumed, 4),
            "detection_floor": round(floor, 6) if floor else None,
        })

    if tiers:
        slowest = min(tiers, key=lambda t: float(t["burn_rate"]))
        if slowest["notify"] == "page":
            report.warn(
                "W404", f"{where} :: tier {slowest['name']}",
                f"the slowest tier (burn rate {float(slowest['burn_rate']):g}) pages. A slow burn is not "
                f"resolved faster by waking someone; this is where alert fatigue usually starts.",
            )

    if not sampling:
        report.warn(
            "W403", where,
            "no sampling block, so the detection floor cannot be computed and W402 is not checked for "
            "any tier. An objective measured from a sparse signal can be arithmetically unobservable "
            "and still look well-specified.",
        )

    for spot in obj["sli"]["blind_spots"]:
        if re.search(r"\bnone\b", spot, re.IGNORECASE) and len(spot) < 40:
            report.warn(
                "W407", where,
                f"blind_spots asserts {spot!r}. The field is required so the claim is visible in review, "
                f"not so it can be dismissed — a denominator with no blind spot at all is rare.",
            )

    report.facts.append(fact)


def check_document(path: Path, doc: dict[str, Any], report: Report, seen_keys: dict[str, str]) -> None:
    label = path.name
    service = doc["metadata"]["service"]
    tier_policy = doc["metadata"]["tier"]
    names: set[str] = set()

    for obj in doc["objectives"]:
        name = obj["name"]
        if name in names:
            report.error("E200", f"{label} :: {service}/{name}",
                         f"duplicate objective name {name!r} in one document.")
        names.add(name)

        key = f"{service}/{name}"
        # Scoped to OTHER documents on purpose: a repeat inside one document is
        # already reported as E200, and reporting it twice under two codes
        # would attach a message about another file to a fault in this one.
        if key in seen_keys and seen_keys[key] != label:
            report.error("E201", f"{label} :: {key}",
                         f"identity {key!r} is already declared in {seen_keys[key]}. Generated alert "
                         f"names are keyed on it, so the two would collide.")
        seen_keys.setdefault(key, label)

        if tier_policy == "best-effort":
            paging = [t["name"] for t in obj["alerting"]["tiers"] if t["notify"] == "page"]
            if paging:
                report.warn("W405", f"{label} :: {key}",
                            f"the service is declared best-effort, but tier(s) {', '.join(paging)} page. "
                            f"A best-effort service that wakes people is not best-effort.")

        check_objective(label, service, obj, report)


def load_documents(paths: Sequence[Path]) -> Iterator[tuple[Path, Any]]:
    for path in paths:
        try:
            with path.open(encoding="utf-8") as handle:
                yield path, yaml.safe_load(handle)
        except OSError as exc:
            print(f"error: cannot read {path}: {exc}", file=sys.stderr)
            raise SystemExit(2) from exc
        except yaml.YAMLError as exc:
            # Exit 2, not 1: an unreadable input is a different outcome from a
            # document that was read and found wanting, and a pipeline step
            # that cannot tell them apart reports a clean repository as broken
            # specifications.
            print(f"error: {path}: not parseable as YAML: {exc}", file=sys.stderr)
            raise SystemExit(2) from exc


def discover(target: Path, pattern: str) -> list[Path]:
    if target.is_file():
        return [target]
    if not target.is_dir():
        print(f"error: {target} is neither a file nor a directory", file=sys.stderr)
        raise SystemExit(2)
    return sorted(target.glob(pattern))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("target", nargs="?", default="specs", type=Path,
                        help="specification file, or directory to search (default: specs)")
    parser.add_argument("--schema", default=Path("schema/slo.schema.json"), type=Path,
                        help="path to the specification schema")
    parser.add_argument("--pattern", default="*.yaml", help="glob used when target is a directory")
    parser.add_argument("--strict", action="store_true", help="treat warnings as failures")
    parser.add_argument("--json", dest="as_json", action="store_true",
                        help="emit findings and computed budget facts as JSON")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    try:
        schema = json.loads(args.schema.read_text(encoding="utf-8"))
    except OSError as exc:
        print(f"error: cannot read schema {args.schema}: {exc}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"error: schema {args.schema} is not valid JSON: {exc}", file=sys.stderr)
        return 2

    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)

    paths = discover(args.target, args.pattern)
    if not paths:
        # Valid, and worth saying out loud: a run over an empty directory
        # otherwise succeeds and is indistinguishable from one that checked
        # something.
        print(f"no specification matched {args.pattern!r} under {args.target} — nothing was checked")
        return 0

    report = Report()
    seen_keys: dict[str, str] = {}

    for path, doc in load_documents(paths):
        LOG.debug("checking %s", path)
        if doc is None:
            report.error("E100", path.name, "document is empty.")
            continue
        schema_errors = sorted(validator.iter_errors(doc), key=lambda e: list(e.path))
        for err in schema_errors:
            location = "/".join(str(p) for p in err.absolute_path) or "(document root)"
            report.error("E100", f"{path.name} :: {location}", err.message)
        if schema_errors:
            # Arithmetic over a document that does not match the schema would
            # report faults in fields the author never wrote.
            LOG.debug("skipping arithmetic for %s: %d schema error(s)", path, len(schema_errors))
            continue
        check_document(path, doc, report, seen_keys)

    if args.as_json:
        print(json.dumps({
            "checked": [str(p) for p in paths],
            "findings": [vars(f) for f in report.findings],
            "facts": report.facts,
        }, indent=2))
    else:
        for finding in sorted(report.findings, key=lambda f: (SEVERITY_ORDER[f.severity], f.code, f.where)):
            print(finding.render())
        for fact in report.facts:
            window = humanise(fact["window_minutes"]) + (" nominal" if fact["window_nominal"] else "")
            budget = humanise(fact["budget_minutes"])
            events = f", {fact['budget_events']:,.0f} events" if fact["budget_events"] is not None else ""
            print(f"\n{fact['service']}/{fact['objective']}  target {fact['target']}  window {window}")
            print(f"  budget: {budget}{events}")
            for tier in fact["tiers"]:
                floor = f", floor {tier['detection_floor']:.3%}" if tier["detection_floor"] else ""
                print(f"  {tier['name']:12s} fires at {tier['fires_at_error_rate']:.3%} error rate, "
                      f"{tier['budget_consumed_when_firing']:.0%} of budget spent{floor}")
        print(f"\n{len(paths)} document(s), {len(report.facts)} objective(s): "
              f"{len(report.errors)} error(s), {len(report.warnings)} warning(s)")

    if report.errors:
        return 1
    if args.strict and report.warnings:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
