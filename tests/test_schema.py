"""The schema's own claims: what it refuses, and what it refuses to default.

Several of the schema's decisions are deliberate absences -- no percentile
indicator, no default window kind, no objective of 1 -- and an absence is
exactly the kind of thing a later edit restores without anybody noticing. Each
one is asserted here as a refusal rather than left as a comment in the JSON.
"""

from __future__ import annotations

import copy
import unittest

import yaml
from jsonschema import Draft202012Validator

from _support import ROOT, document, ratio_objective, schema, threshold_objective


class SchemaIntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schema = schema()

    def test_the_schema_is_a_valid_draft_2020_12_schema(self) -> None:
        Draft202012Validator.check_schema(self.schema)

    def test_every_object_in_the_schema_closes_its_properties(self) -> None:
        # An open object accepts a misspelled field and ignores it, which is
        # the one failure mode a schema is supposed to remove.
        def walk(node: object, path: str) -> None:
            if isinstance(node, dict):
                if node.get("type") == "object" and "properties" in node:
                    self.assertFalse(
                        node.get("additionalProperties", True),
                        f"{path} accepts unknown properties",
                    )
                for key, value in node.items():
                    walk(value, f"{path}/{key}")
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    walk(value, f"{path}[{index}]")

        walk(self.schema, "#")

    def test_every_internal_reference_resolves(self) -> None:
        defs = set(self.schema.get("$defs", {}))

        def walk(node: object) -> None:
            if isinstance(node, dict):
                ref = node.get("$ref")
                if isinstance(ref, str):
                    self.assertTrue(ref.startswith("#/$defs/"), ref)
                    self.assertIn(ref.rsplit("/", 1)[1], defs)
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(self.schema)

    def test_every_definition_is_referenced(self) -> None:
        text = (ROOT / "schema" / "slo.schema.json").read_text(encoding="utf-8")
        for name in self.schema.get("$defs", {}):
            with self.subTest(definition=name):
                self.assertIn(f'"#/$defs/{name}"', text)


class AcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.validator = Draft202012Validator(schema())

    def errors(self, doc: dict) -> list:
        return list(self.validator.iter_errors(doc))

    def test_the_shipped_example_validates(self) -> None:
        doc = yaml.safe_load((ROOT / "specs" / "example.yaml").read_text(encoding="utf-8"))
        self.assertEqual([e.message for e in self.errors(doc)], [])

    def test_both_indicator_kinds_validate(self) -> None:
        self.assertEqual(self.errors(document(ratio_objective())), [])
        self.assertEqual(self.errors(document(threshold_objective())), [])

    def test_a_misspelled_field_is_refused_rather_than_ignored(self) -> None:
        spec = ratio_objective()
        spec["objetive"] = 0.99
        self.assertTrue(self.errors(document(spec)))


class DeliberateRefusalTests(unittest.TestCase):
    """Each of these is a decision the schema records by refusing something."""

    def setUp(self) -> None:
        self.validator = Draft202012Validator(schema())

    def invalid(self, doc: dict) -> bool:
        return bool(list(self.validator.iter_errors(doc)))

    def test_an_objective_of_one_is_refused(self) -> None:
        # Not an objective: the claim that there is no error budget, which
        # sets every threshold to zero and every burn rate to infinity.
        self.assertTrue(self.invalid(document(ratio_objective(objective=1))))
        self.assertTrue(self.invalid(document(ratio_objective(objective=1.5))))

    def test_an_objective_below_the_typo_guard_is_refused(self) -> None:
        # A misplaced decimal point is far more common than a service allowed
        # to fail half the time.
        self.assertTrue(self.invalid(document(ratio_objective(objective=0.0995))))
        self.assertFalse(self.invalid(document(ratio_objective(objective=0.5))))

    def test_there_is_no_percentile_indicator_kind(self) -> None:
        # A percentile is not a proportion, so no error budget follows from
        # one; and percentiles do not compose, so a percentile objective
        # cannot be evaluated over any window but the one it was measured in.
        spec = ratio_objective()
        spec["sli"] = {
            "kind": "percentile",
            "metric": "http_request_duration_seconds",
            "quantile": 0.95,
            "blind_spots": ["none"],
        }
        self.assertTrue(self.invalid(document(spec)))

    def test_a_window_without_a_kind_is_refused_rather_than_defaulted(self) -> None:
        # Neither kind is safe: rolling keeps charging for an incident for a
        # whole window, calendar forgives one on the last day of the period.
        self.assertTrue(self.invalid(document(ratio_objective(window={"duration": "28d"}))))

    def test_the_two_window_kinds_cannot_be_mixed(self) -> None:
        self.assertTrue(self.invalid(document(ratio_objective(
            window={"kind": "rolling", "duration": "28d", "period": "month"}))))

    def test_a_rolling_window_cannot_be_expressed_in_weeks_or_months(self) -> None:
        for duration in ("4w", "1mo", "1y"):
            with self.subTest(duration=duration):
                self.assertTrue(self.invalid(
                    document(ratio_objective(window={"kind": "rolling", "duration": duration}))))

    def test_blind_spots_are_required_and_cannot_be_empty(self) -> None:
        # The schema can force the question to be answered; it cannot check
        # the answer. The validator's W407 handles the dismissal.
        spec = ratio_objective()
        del spec["sli"]["blind_spots"]
        self.assertTrue(self.invalid(document(spec)))
        spec = ratio_objective()
        spec["sli"]["blind_spots"] = []
        self.assertTrue(self.invalid(document(spec)))

    def test_a_threshold_indicator_must_state_which_side_is_good(self) -> None:
        # Half of all threshold indicators are upper bounds and half are
        # lower bounds, so a default inverts the objective half the time
        # while still producing a plausible number.
        spec = threshold_objective()
        del spec["sli"]["comparison"]
        self.assertTrue(self.invalid(document(spec)))

    def test_an_objective_name_over_the_identity_budget_is_refused(self) -> None:
        self.assertTrue(self.invalid(document(ratio_objective(name="a" * 33))))
        self.assertFalse(self.invalid(document(ratio_objective(name="a" * 32))))

    def test_an_identifier_may_not_start_with_a_digit_or_a_hyphen(self) -> None:
        for name in ("1availability", "-availability", "Availability", "avail ability"):
            with self.subTest(name=name):
                self.assertTrue(self.invalid(document(ratio_objective(name=name))))

    def test_a_burn_rate_of_zero_or_below_is_refused(self) -> None:
        for burn in (0, -1):
            with self.subTest(burn_rate=burn):
                spec = ratio_objective()
                spec["alerting"]["tiers"][0]["burn_rate"] = burn
                self.assertTrue(self.invalid(document(spec)))

    def test_an_objective_must_declare_at_least_one_tier(self) -> None:
        spec = ratio_objective()
        spec["alerting"]["tiers"] = []
        self.assertTrue(self.invalid(document(spec)))

    def test_the_api_version_is_pinned_so_a_consumer_never_reinterprets(self) -> None:
        doc = document()
        doc["apiVersion"] = "slo.platform/v2"
        self.assertTrue(self.invalid(doc))


class ModelAgreementTests(unittest.TestCase):
    """Bounds the schema states and the model enforces must be one number."""

    def test_the_identity_budget_matches_the_name_pattern(self) -> None:
        import base

        pattern = schema()["$defs"]["objective"]["properties"]["name"]["pattern"]
        self.assertIn(str(base.OBJECTIVE_KEY_BUDGET - 1), pattern)

    def test_every_calendar_period_the_schema_allows_has_a_nominal_span(self) -> None:
        import base

        window = schema()["$defs"]["window"]
        periods = next(
            option["properties"]["period"]["enum"]
            for option in window["oneOf"]
            if "period" in option["properties"]
        )
        self.assertEqual(set(periods), set(base.NOMINAL_CALENDAR_SECONDS))

    def test_every_duration_the_schema_allows_is_one_the_model_can_parse(self) -> None:
        import base

        for text in ("1m", "90m", "2h", "36h", "1d", "28d", "365d"):
            with self.subTest(text=text):
                self.assertTrue(self.matches_duration_pattern(text))
                self.assertGreater(base.parse_duration_seconds(text), 0)

    def matches_duration_pattern(self, text: str) -> bool:
        import re

        pattern = schema()["$defs"]["burnTier"]["properties"]["long_window"]["pattern"]
        return re.match(pattern, text) is not None

    def test_every_notify_value_the_schema_allows_is_ranked_by_the_generator(self) -> None:
        # The shadowing check compares urgencies through a lookup table, and
        # a value the schema permits but the table omits raises a KeyError
        # inside the generator rather than being reported.
        import re

        notify = schema()["$defs"]["burnTier"]["properties"]["notify"]["enum"]
        source = (ROOT / "generator" / "burn_rate.py").read_text(encoding="utf-8")
        ranked = re.search(r'urgency = \{([^}]*)\}', source)
        self.assertIsNotNone(ranked)
        for value in notify:
            with self.subTest(notify=value):
                self.assertIn(f'"{value}"', ranked.group(1))


class FixtureIntegrityTests(unittest.TestCase):
    def test_the_suite_fixtures_are_valid_against_the_schema(self) -> None:
        # Every other test in the suite mutates one of these, so a fixture
        # that was already invalid would make each of those a test of the
        # fixture rather than of the mutation.
        validator = Draft202012Validator(schema())
        for name, doc in (
            ("ratio", document(ratio_objective())),
            ("threshold", document(threshold_objective())),
            ("both", document(ratio_objective(), threshold_objective())),
        ):
            with self.subTest(fixture=name):
                self.assertEqual([e.message for e in validator.iter_errors(doc)], [])

    def test_the_builders_hand_out_independent_copies(self) -> None:
        first = ratio_objective()
        first["alerting"]["tiers"][0]["burn_rate"] = 99
        self.assertEqual(ratio_objective()["alerting"]["tiers"][0]["burn_rate"], 14.4)
        doc = document(first)
        doc["objectives"][0]["name"] = "changed"
        self.assertEqual(first["name"], "availability")
        self.assertEqual(copy.deepcopy(first)["name"], "availability")


if __name__ == "__main__":  # pragma: no cover - thin wrapper
    unittest.main()
