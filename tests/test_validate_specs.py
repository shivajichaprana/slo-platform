"""Every finding the validator can emit, fired by a document that earns it.

A finding code that is never exercised is a claim the repository makes about
specifications it has never checked, so the coverage test at the end of this
module asserts in both directions: every code emitted by the tool is reached
here, and every code reached here is one the tool can emit.
"""

from __future__ import annotations

import contextlib
import copy
import io
import tempfile
import unittest
from pathlib import Path

from _support import (
    FAST_TIER,
    ROOT,
    document,
    load_script,
    ratio_objective,
    write_document,
)

validate_specs = load_script("tools/validate-specs.py", "validate_specs")


def findings_for(doc: dict, *more: dict) -> list:
    """Run the whole tool over one or more in-memory documents."""
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory)
        write_document(target, doc, "a-spec.yaml")
        for index, extra in enumerate(more):
            write_document(target, extra, f"z-extra-{index}.yaml")
        return run(target).findings


def run(target: Path) -> "validate_specs.Report":
    """The validator's own pipeline, without the CLI's printing."""
    import json

    from jsonschema import Draft202012Validator

    schema = json.loads((ROOT / "schema" / "slo.schema.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    report = validate_specs.Report()
    seen: dict[str, str] = {}
    for path, doc in validate_specs.load_documents(validate_specs.discover(target, "*.yaml")):
        errors = sorted(validator.iter_errors(doc), key=lambda e: list(e.path))
        for err in errors:
            location = "/".join(str(p) for p in err.absolute_path) or "(document root)"
            report.error("E100", f"{path.name} :: {location}", err.message)
        if errors:
            continue
        validate_specs.check_document(path, doc, report, seen)
    return report


def codes_for(doc: dict, *more: dict) -> set[str]:
    return {f.code for f in findings_for(doc, *more)}


def tier(name: str, burn: float, long_window: str, short_window: str,
         notify: str = "page") -> dict:
    return {"name": name, "burn_rate": burn, "long_window": long_window,
            "short_window": short_window, "notify": notify}


def with_tiers(*tiers: dict, **overrides) -> dict:
    spec = ratio_objective(**overrides)
    spec["alerting"]["tiers"] = [copy.deepcopy(t) for t in tiers]
    return spec


def exercised_codes() -> set[str]:
    """Codes this module asserts on, read from its own source.

    Collected statically rather than accumulated as the tests run: unittest
    orders test classes alphabetically, so a set filled in at run time is
    whatever happened to have executed before the coverage test did, and the
    coverage claim silently becomes a claim about test ordering.
    """
    import re

    source = Path(__file__).read_text(encoding="utf-8")
    return set(re.findall(r'assertFires\(\s*"([EW][0-9]{3})"', source))


class ValidatorCase(unittest.TestCase):
    def assertFires(self, code: str, doc: dict, *more: dict) -> None:
        found = codes_for(doc, *more)
        self.assertIn(code, found, f"expected {code}, got {sorted(found)}")

    def assertSilent(self, code: str, doc: dict, *more: dict) -> None:
        found = codes_for(doc, *more)
        self.assertNotIn(code, found, f"did not expect {code}")


class DurationTests(unittest.TestCase):
    def test_durations_resolve_to_minutes(self) -> None:
        self.assertEqual(validate_specs.parse_duration_minutes("5m"), 5)
        self.assertEqual(validate_specs.parse_duration_minutes("1h"), 60)
        self.assertEqual(validate_specs.parse_duration_minutes("28d"), 40320)

    def test_the_two_duration_parsers_agree(self) -> None:
        # The validator counts minutes and the model counts seconds. They are
        # separate on purpose -- the tool predates the model and is run
        # without it -- so the only thing holding them together is this.
        import base

        for text in ("1m", "45m", "1h", "6h", "1d", "28d", "90d"):
            with self.subTest(text=text):
                self.assertEqual(
                    validate_specs.parse_duration_minutes(text) * 60,
                    base.parse_duration_seconds(text),
                )

    def test_window_minutes_marks_a_calendar_period_nominal(self) -> None:
        self.assertEqual(
            validate_specs.window_minutes({"kind": "rolling", "duration": "1d"}), (1440, False))
        self.assertEqual(
            validate_specs.window_minutes({"kind": "calendar", "period": "month"}),
            (30 * 1440, True),
        )


class StructuralFindingTests(ValidatorCase):
    def test_a_document_the_schema_rejects_is_E100(self) -> None:
        doc = document()
        del doc["objectives"][0]["window"]
        self.assertFires("E100", doc)

    def test_arithmetic_is_not_attempted_on_a_schema_invalid_document(self) -> None:
        # Otherwise the report names faults in fields the author never wrote.
        doc = document(with_tiers(tier("fast", 14.4, "1h", "5m"), objective=0.9))
        del doc["metadata"]["tier"]
        self.assertEqual(codes_for(doc), {"E100"})

    def test_two_objectives_with_one_name_in_a_document_is_E200(self) -> None:
        first = ratio_objective()
        second = ratio_objective(title="A second objective under the same name")
        self.assertFires("E200", document(first, second))

    def test_the_same_identity_in_two_documents_is_E201(self) -> None:
        self.assertFires("E201", document(), document())

    def test_a_repeat_inside_one_document_is_not_also_reported_as_E201(self) -> None:
        # E200 already names it, and E201's message points at another file.
        self.assertSilent("E201", document(ratio_objective(), ratio_objective()))

    def test_an_identity_over_the_character_budget_is_E202(self) -> None:
        self.assertFires(
            "E202",
            document(ratio_objective(name="availability-of-the-payment-path"),
                     service="checkout-api"),
        )

    def test_the_identity_budget_is_one_number_shared_with_the_model(self) -> None:
        import base

        self.assertEqual(validate_specs.OBJECTIVE_KEY_BUDGET, base.OBJECTIVE_KEY_BUDGET)

    def test_two_tiers_with_one_name_is_E303(self) -> None:
        self.assertFires(
            "E303", document(with_tiers(tier("fast", 14.4, "1h", "5m"),
                                        tier("fast", 6, "6h", "30m"))))


class ArithmeticFindingTests(ValidatorCase):
    def test_a_tier_needing_more_than_total_failure_is_E300(self) -> None:
        self.assertFires("E300", document(with_tiers(FAST_TIER, objective=0.9)))

    def test_a_long_window_at_the_objective_window_is_E301(self) -> None:
        # An alert measured over the objective's own window IS the objective,
        # reported once it has already been missed.
        self.assertFires(
            "E301", document(with_tiers(tier("whole", 1, "28d", "1d", "ticket"))))

    def test_a_short_window_that_cannot_release_the_alert_is_E302(self) -> None:
        self.assertFires(
            "E302", document(with_tiers(tier("fast", 14.4, "1h", "2h"))))

    def test_a_short_window_equal_to_the_long_one_is_also_E302(self) -> None:
        # The comparison is "not shorter", not "longer": two equal windows are
        # one condition written twice, and the alert cannot release at all.
        self.assertFires(
            "E302", document(with_tiers(tier("fast", 14.4, "1h", "60m"))))

    def test_a_long_window_equal_to_the_objective_window_is_also_E301(self) -> None:
        self.assertFires(
            "E301",
            document(with_tiers(tier("whole", 1, "28d", "1d", "ticket"),
                                window={"kind": "rolling", "duration": "28d"})))

    def test_a_near_total_outage_detector_is_W400(self) -> None:
        # 14.4 x a 5% budget fires at 72%: reachable, and nothing anybody
        # finds out about from an alert.
        self.assertFires("W400", document(with_tiers(FAST_TIER, objective=0.95)))

    def test_a_tier_firing_past_half_the_budget_is_W401(self) -> None:
        self.assertFires(
            "W401", document(with_tiers(tier("late", 14.4, "1d", "2h"))))

    def test_a_threshold_under_the_sampling_floor_is_W402(self) -> None:
        sparse = with_tiers(tier("fast", 2, "1h", "5m"))
        sparse["sli"]["sampling"]["expected_events_per_hour"] = 60
        self.assertFires("W402", document(sparse))

    def test_an_objective_with_no_sampling_block_is_W403(self) -> None:
        spec = ratio_objective()
        del spec["sli"]["sampling"]
        self.assertFires("W403", document(spec))

    def test_a_slowest_tier_that_pages_is_W404(self) -> None:
        self.assertFires(
            "W404",
            document(with_tiers(tier("fast", 14.4, "1h", "5m"), tier("slow", 1, "3d", "6h"))),
        )

    def test_a_best_effort_service_that_pages_is_W405(self) -> None:
        self.assertFires("W405", document(ratio_objective(), tier="best-effort"))

    def test_two_tiers_at_one_burn_rate_is_W406(self) -> None:
        self.assertFires(
            "W406",
            document(with_tiers(tier("fast", 6, "1h", "5m"), tier("slower", 6, "6h", "30m"))),
        )

    def test_a_blind_spot_claim_of_none_is_W407(self) -> None:
        spec = ratio_objective()
        spec["sli"]["blind_spots"] = ["None identified."]
        self.assertFires("W407", document(spec))

    def test_a_considered_blind_spot_mentioning_none_is_left_alone(self) -> None:
        # The check is a length-bounded dismissal detector, not a word filter:
        # a sentence long enough to be an argument is an answer.
        spec = ratio_objective()
        spec["sli"]["blind_spots"] = [
            "None of the retries are distinguishable from first attempts in this denominator.",
        ]
        self.assertSilent("W407", document(spec))

    def test_a_window_ratio_outside_the_convention_is_W408(self) -> None:
        self.assertFires("W408", document(with_tiers(tier("fast", 14.4, "1h", "30m"))))

    def test_a_budget_smaller_than_one_event_is_W409(self) -> None:
        tiny = with_tiers(tier("fast", 2, "1h", "5m"), objective=0.999)
        tiny["sli"]["sampling"]["expected_events_per_hour"] = 0.001
        self.assertFires("W409", document(tiny))

    def test_paging_on_a_calendar_window_is_W410(self) -> None:
        self.assertFires(
            "W410",
            document(with_tiers(FAST_TIER, window={"kind": "calendar", "period": "month"})),
        )


class CleanDocumentTests(unittest.TestCase):
    def test_the_fixture_used_throughout_the_suite_is_itself_clean(self) -> None:
        # Every test above states its subject by mutating this document, so a
        # finding already present in it would be attributed to the mutation.
        self.assertEqual(findings_for(document()), [])

    def test_the_shipped_example_is_clean_at_every_severity(self) -> None:
        report = run(ROOT / "specs")
        self.assertEqual([f.render() for f in report.errors], [])
        self.assertEqual([f.render() for f in report.warnings], [])

    def test_the_shipped_example_reports_three_objectives(self) -> None:
        facts = run(ROOT / "specs").facts
        self.assertEqual(
            [f["objective"] for f in facts], ["availability", "latency", "refund-settlement"])


class ExitStatusTests(unittest.TestCase):
    """A pipeline reads the status, so the statuses have to mean one thing each."""

    def invoke(self, *argv: str) -> int:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                return validate_specs.main(list(argv))
            except SystemExit as exc:  # the unreadable-input path raises
                return int(exc.code or 0)

    def test_the_shipped_specifications_pass_under_strict(self) -> None:
        self.assertEqual(
            self.invoke(str(ROOT / "specs"), "--schema",
                        str(ROOT / "schema" / "slo.schema.json"), "--strict"), 0)

    def test_a_document_read_and_found_wanting_exits_one(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            write_document(Path(directory), document(with_tiers(FAST_TIER, objective=0.9)))
            self.assertEqual(
                self.invoke(directory, "--schema",
                            str(ROOT / "schema" / "slo.schema.json")), 1)

    def test_warnings_alone_exit_one_only_under_strict(self) -> None:
        spec = ratio_objective()
        del spec["sli"]["sampling"]
        with tempfile.TemporaryDirectory() as directory:
            write_document(Path(directory), document(spec))
            schema = str(ROOT / "schema" / "slo.schema.json")
            self.assertEqual(self.invoke(directory, "--schema", schema), 0)
            self.assertEqual(self.invoke(directory, "--schema", schema, "--strict"), 1)

    def test_an_unparseable_document_exits_two_not_one(self) -> None:
        # The distinction the exit ladder exists for: a step that cannot tell
        # "could not read" from "read and wrong" reports a clean repository as
        # broken specifications.
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "broken.yaml").write_text("objectives: [\n", encoding="utf-8")
            self.assertEqual(
                self.invoke(directory, "--schema",
                            str(ROOT / "schema" / "slo.schema.json")), 2)

    def test_a_missing_target_exits_two(self) -> None:
        self.assertEqual(
            self.invoke(str(ROOT / "no-such-directory"), "--schema",
                        str(ROOT / "schema" / "slo.schema.json")), 2)

    def test_an_unreadable_schema_exits_two(self) -> None:
        self.assertEqual(
            self.invoke(str(ROOT / "specs"), "--schema", str(ROOT / "no-such-schema.json")), 2)

    def test_an_empty_directory_exits_zero_and_says_nothing_was_checked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                status = validate_specs.main(
                    [directory, "--schema", str(ROOT / "schema" / "slo.schema.json")])
            self.assertEqual(status, 0)
            self.assertIn("nothing was checked", out.getvalue())

    def test_json_output_is_parseable_on_its_own(self) -> None:
        import json

        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            validate_specs.main([
                str(ROOT / "specs"), "--schema", str(ROOT / "schema" / "slo.schema.json"),
                "--json",
            ])
        payload = json.loads(out.getvalue())
        self.assertEqual(len(payload["facts"]), 3)
        self.assertEqual(payload["findings"], [])


class CoverageTests(unittest.TestCase):
    """The suite's own claim: no finding code is asserted about and untested."""

    def emitted_codes(self) -> set[str]:
        import re

        source = (ROOT / "tools" / "validate-specs.py").read_text(encoding="utf-8")
        return set(re.findall(r'report\.(?:error|warn)\(\s*"([EW][0-9]{3})"', source))

    def test_every_code_the_validator_can_emit_is_exercised(self) -> None:
        missing = self.emitted_codes() - exercised_codes()
        self.assertEqual(missing, set(), f"never fired by any test: {sorted(missing)}")

    def test_no_test_asserts_a_code_the_validator_cannot_emit(self) -> None:
        unknown = exercised_codes() - self.emitted_codes()
        self.assertEqual(unknown, set(), f"not emitted anywhere: {sorted(unknown)}")

    def test_every_emitted_code_is_documented_in_the_readme(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        undocumented = {code for code in self.emitted_codes() if code not in readme}
        self.assertEqual(undocumented, set(), f"undocumented: {sorted(undocumented)}")


if __name__ == "__main__":  # pragma: no cover - thin wrapper
    unittest.main()
