"""Parsing a specification into the model every other component works from.

The model is where a specification stops being text, so a fault here is a
fault everywhere downstream and reports itself nowhere: a window parsed to the
wrong number of seconds produces alerts that deploy cleanly, a budget that
looks plausible, and an objective measured over a span nobody chose.
"""

from __future__ import annotations

import unittest

from _support import document, ratio_objective, threshold_objective

import base


class DurationTests(unittest.TestCase):
    def test_each_unit_resolves_to_its_own_seconds(self) -> None:
        self.assertEqual(base.parse_duration_seconds("5m"), 300)
        self.assertEqual(base.parse_duration_seconds("6h"), 21600)
        self.assertEqual(base.parse_duration_seconds("28d"), 2419200)

    def test_a_week_is_not_a_duration_unit(self) -> None:
        # Deliberate: weeks and months are not fixed spans, so an objective
        # measured per month is a calendar window rather than a long rolling
        # one. Accepting "4w" here would let the distinction be written away.
        with self.assertRaises(ValueError):
            base.parse_duration_seconds("4w")

    def test_zero_and_negative_spans_are_refused(self) -> None:
        for text in ("0m", "-1h", "1.5h", "h", "60", ""):
            with self.subTest(text=text), self.assertRaises(ValueError):
                base.parse_duration_seconds(text)

    def test_humanise_changes_unit_at_the_hour_and_the_day(self) -> None:
        self.assertEqual(base.humanise(300), "5m")
        self.assertEqual(base.humanise(3599), "60m")
        self.assertEqual(base.humanise(3600), "1.0h")
        self.assertEqual(base.humanise(86399), "24.0h")
        self.assertEqual(base.humanise(86400), "1.0d")


class WindowTests(unittest.TestCase):
    def test_rolling_window_carries_its_own_literal_span(self) -> None:
        window = base.Window.from_spec({"kind": "rolling", "duration": "28d"})
        self.assertEqual(window.kind, "rolling")
        self.assertEqual(window.seconds, 28 * 86400)
        self.assertFalse(window.nominal)
        self.assertEqual(window.label, "28d")
        self.assertIsNone(window.timezone)

    def test_calendar_window_is_nominal_and_says_so(self) -> None:
        # A month is 28, 29, 30 or 31 days. The model picks one so arithmetic
        # is possible at all, and flags every figure derived from it as
        # nominal so nothing downstream quotes it as measured.
        window = base.Window.from_spec({"kind": "calendar", "period": "month"})
        self.assertEqual(window.seconds, 30 * 86400)
        self.assertTrue(window.nominal)
        self.assertEqual(window.label, "month")

    def test_calendar_window_defaults_to_utc_but_keeps_a_stated_zone(self) -> None:
        # The zone is carried rather than resolved: a boundary is an instant,
        # and a window is a span. Resolving it here would put a clock inside a
        # value object that other components compare for equality.
        self.assertEqual(
            base.Window.from_spec({"kind": "calendar", "period": "week"}).timezone, "UTC")
        self.assertEqual(
            base.Window.from_spec(
                {"kind": "calendar", "period": "week", "timezone": "Europe/Amsterdam"},
            ).timezone,
            "Europe/Amsterdam",
        )

    def test_unknown_calendar_period_is_refused_rather_than_guessed(self) -> None:
        with self.assertRaises(ValueError):
            base.Window.from_spec({"kind": "calendar", "period": "fortnight"})

    def test_windows_with_equal_spans_compare_equal(self) -> None:
        first = base.Window.from_spec({"kind": "rolling", "duration": "1d"})
        second = base.Window.from_spec({"kind": "rolling", "duration": "1d"})
        self.assertEqual(first, second)
        self.assertEqual(len({first, second}), 1)


class TierTests(unittest.TestCase):
    def test_both_windows_are_resolved_to_seconds(self) -> None:
        tier = base.Tier.from_spec(
            {"name": "fast", "burn_rate": 14.4, "long_window": "1h",
             "short_window": "5m", "notify": "page"},
        )
        self.assertEqual(tier.long_seconds, 3600)
        self.assertEqual(tier.short_seconds, 300)
        self.assertEqual(tier.notify, "page")

    def test_an_integer_burn_rate_becomes_a_float(self) -> None:
        # YAML types the literal, and `6` and `6.0` must not produce two tiers
        # that compare unequal while meaning the same thing.
        tier = base.Tier.from_spec(
            {"name": "medium", "burn_rate": 6, "long_window": "6h",
             "short_window": "30m", "notify": "page"},
        )
        self.assertIsInstance(tier.burn_rate, float)
        self.assertEqual(tier.burn_rate, 6.0)

    def test_firing_rate_is_the_burn_rate_times_the_budget(self) -> None:
        tier = base.Tier.from_spec(
            {"name": "fast", "burn_rate": 14.4, "long_window": "1h",
             "short_window": "5m", "notify": "page"},
        )
        self.assertAlmostEqual(tier.fires_at_error_rate(0.005), 0.072)
        # The same tier against a tighter budget fires at a proportionally
        # lower rate: the tier is a multiple, never an absolute threshold.
        self.assertAlmostEqual(tier.fires_at_error_rate(0.001), 0.0144)


class ObjectiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.doc = document()
        self.objective = base.Objective.from_spec(
            self.doc, self.doc["objectives"][0], document="spec.yaml")

    def test_metadata_is_inherited_from_the_document(self) -> None:
        self.assertEqual(self.objective.service, "checkout")
        self.assertEqual(self.objective.owner, "payments-platform")
        self.assertEqual(self.objective.tier_policy, "critical")

    def test_budget_is_everything_the_target_does_not_claim(self) -> None:
        self.assertAlmostEqual(self.objective.allowed, 0.005)

    def test_identity_is_service_and_name_joined_by_a_hyphen(self) -> None:
        # Alert names and stored budget observations are both keyed on this,
        # which is why its length is budgeted rather than left to chance.
        self.assertEqual(self.objective.key, "checkout-availability")
        self.assertLessEqual(len(self.objective.key), base.OBJECTIVE_KEY_BUDGET)

    def test_location_names_the_document_when_one_is_known(self) -> None:
        self.assertEqual(self.objective.where, "spec.yaml :: checkout/availability")
        anonymous = base.Objective.from_spec(self.doc, self.doc["objectives"][0])
        self.assertEqual(anonymous.where, "checkout/availability")

    def test_tiers_are_a_tuple_so_a_parsed_objective_cannot_be_edited(self) -> None:
        self.assertIsInstance(self.objective.tiers, tuple)
        with self.assertRaises(AttributeError):
            self.objective.target = 0.5  # type: ignore[misc]

    def test_sampling_is_optional_and_absent_means_unknown_not_zero(self) -> None:
        self.assertEqual(self.objective.sampling_per_hour(), 120000.0)
        spec = ratio_objective()
        del spec["sli"]["sampling"]
        without = base.Objective.from_spec(document(spec), spec)
        # None, not 0.0: a rate of zero would make every event-count figure
        # downstream come out as zero rather than as unavailable.
        self.assertIsNone(without.sampling_per_hour())

    def test_a_threshold_indicator_parses_with_the_same_shape(self) -> None:
        spec = threshold_objective()
        objective = base.Objective.from_spec(document(spec), spec)
        self.assertEqual(objective.sli["kind"], "threshold")
        self.assertEqual(objective.sli["comparison"], "less_than_or_equal")
        self.assertAlmostEqual(objective.allowed, 0.01)

    def test_objectives_in_reads_every_objective_in_a_document(self) -> None:
        doc = document(ratio_objective(), threshold_objective())
        parsed = list(base.objectives_in(doc, "spec.yaml"))
        self.assertEqual([o.name for o in parsed], ["availability", "latency"])
        self.assertTrue(all(o.document == "spec.yaml" for o in parsed))


class FindingTests(unittest.TestCase):
    def test_severity_is_closed(self) -> None:
        with self.assertRaises(ValueError):
            base.Finding(code="X000", severity="critical", where="here", message="m")

    def test_the_three_constructors_set_the_severity_they_are_named_for(self) -> None:
        self.assertEqual(base.error("E1", "w", "m").severity, "error")
        self.assertEqual(base.warn("W1", "w", "m").severity, "warning")
        self.assertEqual(base.note("N1", "w", "m").severity, "note")

    def test_render_leads_with_the_severity_and_the_code(self) -> None:
        rendered = base.warn("W402", "spec.yaml :: a/b", "too few events").render()
        self.assertIn("W402", rendered)
        self.assertIn("spec.yaml :: a/b", rendered)
        self.assertIn("too few events", rendered)


if __name__ == "__main__":  # pragma: no cover - thin wrapper
    unittest.main()
