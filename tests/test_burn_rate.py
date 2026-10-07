"""The arithmetic a burn-rate tier promises, and the promises it cannot keep.

Most of these are closed-form identities rather than recorded outputs. A test
that pins a number only says the code still does what it did; an identity says
what the number means, and fails when an edit changes the meaning while
keeping the shape.
"""

from __future__ import annotations

import copy
import json
import unittest

from _support import FAST_TIER, document, ratio_objective

import base
import burn_rate


def objective_from(spec: dict) -> base.Objective:
    doc = document(spec)
    return base.Objective.from_spec(doc, doc["objectives"][0], document="spec.yaml")


def tier(name: str, burn: float, long_window: str, short_window: str,
         notify: str = "page") -> dict:
    return {"name": name, "burn_rate": burn, "long_window": long_window,
            "short_window": short_window, "notify": notify}


def with_tiers(*tiers: dict, **overrides) -> base.Objective:
    spec = ratio_objective(**overrides)
    spec["alerting"]["tiers"] = [copy.deepcopy(t) for t in tiers]
    return objective_from(spec)


class BudgetTests(unittest.TestCase):
    def test_budget_is_the_complement_of_the_target_over_the_window(self) -> None:
        budget = burn_rate.Budget.of(objective_from(ratio_objective()))
        self.assertAlmostEqual(budget.allowed, 0.005)
        self.assertEqual(budget.window_seconds, 28 * 86400)
        # 0.5% of four weeks, quoted as the length of a total failure that
        # would spend all of it. Not downtime -- the indicator counts events.
        self.assertAlmostEqual(budget.equivalent_outage_seconds, 0.005 * 28 * 86400)
        self.assertFalse(budget.nominal)

    def test_event_count_follows_the_stated_sampling_rate(self) -> None:
        budget = burn_rate.Budget.of(objective_from(ratio_objective()))
        self.assertAlmostEqual(budget.events, 120000 * 28 * 24 * 0.005)

    def test_an_unsampled_objective_has_an_unknown_event_count_not_a_zero_one(self) -> None:
        spec = ratio_objective()
        del spec["sli"]["sampling"]
        self.assertIsNone(burn_rate.Budget.of(objective_from(spec)).events)

    def test_a_calendar_budget_is_marked_nominal(self) -> None:
        spec = ratio_objective(window={"kind": "calendar", "period": "month"})
        budget = burn_rate.Budget.of(objective_from(spec))
        self.assertTrue(budget.nominal)
        self.assertEqual(budget.window_label, "month")

    def test_exhaustion_is_the_budget_divided_by_the_rate_spending_it(self) -> None:
        budget = burn_rate.Budget.of(objective_from(ratio_objective()))
        for rate in (1.0, 0.5, 0.05, 0.0051):
            with self.subTest(rate=rate):
                self.assertAlmostEqual(
                    budget.exhaustion_seconds_at(rate),
                    budget.allowed * budget.window_seconds / rate,
                )

    def test_a_rate_the_objective_permits_never_exhausts_a_rolling_budget(self) -> None:
        # The boundary a 1x tier sits on: at or below the permitted rate the
        # budget is replenished as fast as it is spent, so the answer is "not
        # at all" rather than a very large number of seconds.
        budget = burn_rate.Budget.of(objective_from(ratio_objective()))
        self.assertIsNone(budget.exhaustion_seconds_at(budget.allowed))
        self.assertIsNone(budget.exhaustion_seconds_at(budget.allowed / 2))
        self.assertIsNone(budget.exhaustion_seconds_at(0.0))
        self.assertIsNotNone(budget.exhaustion_seconds_at(budget.allowed * 1.001))


class DetectionCurveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = burn_rate.plan_objective(with_tiers(FAST_TIER))
        self.tier = self.plan.tiers[0]

    def test_detection_time_is_inversely_proportional_to_the_error_rate(self) -> None:
        for rate in (1.0, 0.5, 0.25, 0.1):
            with self.subTest(rate=rate):
                self.assertAlmostEqual(
                    self.tier.detection_seconds_at(rate),
                    self.tier.threshold * self.tier.tier.long_seconds / rate,
                )

    def test_an_incident_sitting_on_the_threshold_takes_the_whole_long_window(self) -> None:
        self.assertAlmostEqual(
            self.tier.detection_seconds_at(self.tier.threshold),
            self.tier.tier.long_seconds,
        )

    def test_below_the_threshold_the_tier_does_not_fire_late_it_does_not_fire(self) -> None:
        self.assertIsNone(self.tier.detection_seconds_at(self.tier.threshold * 0.999))
        self.assertIsNone(self.tier.detection_seconds_at(0.0))

    def test_the_budget_spent_at_detection_does_not_depend_on_the_error_rate(self) -> None:
        # The invariant the whole module exists to surface: substituting the
        # detection time into the budget consumed cancels the error rate out,
        # so what a tier costs is fixed even though when it fires is not.
        budget = self.plan.budget
        for rate in (1.0, 0.5, 0.1, self.tier.threshold):
            with self.subTest(rate=rate):
                spent = rate * self.tier.detection_seconds_at(rate)
                fraction = spent / (budget.allowed * budget.window_seconds)
                self.assertAlmostEqual(fraction, self.tier.budget_fraction_at_detection)

    def test_the_cost_is_also_the_burn_rate_times_the_window_ratio(self) -> None:
        self.assertAlmostEqual(
            self.tier.budget_fraction_at_detection,
            self.tier.tier.burn_rate * self.tier.tier.long_seconds / (28 * 86400),
        )

    def test_the_blind_floor_and_the_total_outage_detection_time_are_one_quantity(self) -> None:
        # Read one way it is how fast the tier sees a total outage; read the
        # other it is the shortest total outage it can see at all. Reporting
        # them as two independent facts is how a policy gets described as
        # faster than it is.
        self.assertAlmostEqual(
            self.tier.min_detectable_outage_seconds,
            self.tier.detection_seconds_at(1.0),
        )

    def test_the_short_window_sets_clearing_and_the_long_window_sets_latching(self) -> None:
        for rate in (1.0, 0.5, 0.2):
            with self.subTest(rate=rate):
                ratio = max(0.0, 1.0 - self.tier.threshold / rate)
                self.assertAlmostEqual(
                    self.tier.release_seconds_at(rate), self.tier.tier.short_seconds * ratio)
                self.assertAlmostEqual(
                    self.tier.latch_seconds_without_short_window(rate),
                    self.tier.tier.long_seconds * ratio,
                )

    def test_clearing_is_faster_than_latching_by_the_ratio_of_the_two_windows(self) -> None:
        self.assertLess(self.tier.release_seconds_at(1.0),
                        self.tier.latch_seconds_without_short_window(1.0))

    def test_an_incident_exactly_on_the_threshold_clears_immediately(self) -> None:
        self.assertAlmostEqual(self.tier.release_seconds_at(self.tier.threshold), 0.0)

    def test_the_short_side_is_already_true_when_the_long_side_crosses(self) -> None:
        # Which is why the short window governs clearing and not firing. For a
        # constant-rate incident the short average reaches the threshold
        # strictly earlier, so requiring both changes nothing about when the
        # tier fires.
        for rate in (1.0, 0.5, 0.1):
            with self.subTest(rate=rate):
                long_crossing = self.tier.threshold * self.tier.tier.long_seconds / rate
                short_crossing = self.tier.threshold * self.tier.tier.short_seconds / rate
                self.assertLess(short_crossing, long_crossing)


class ReachabilityTests(unittest.TestCase):
    def test_a_tier_that_would_need_more_than_total_failure_is_refused(self) -> None:
        # 14.4 x a 10% budget is a 144% error rate. Rendered, it deploys, is
        # accepted, reports healthy for ever and never fires.
        plan = burn_rate.plan_objective(with_tiers(FAST_TIER, objective=0.9))
        self.assertIn("G301", [f.code for f in plan.errors])
        self.assertFalse(plan.plannable)

    def test_the_reachability_boundary_is_one_minus_one_over_the_burn_rate(self) -> None:
        # The same boundary `tools/validate-specs.py` reports as E300. Two
        # derivations of one number is how two components come to disagree
        # about where a tier stops being legal, so it is asserted here.
        #
        # The boundary is a floor on the OBJECTIVE, not a ceiling: a weaker
        # objective is a larger budget, and a large enough budget pushes the
        # tier's threshold past 100%. A 14.4x tier therefore needs an
        # objective of at least 93.06% to be able to fire at all.
        burn = 14.4
        boundary = 1 - 1 / burn
        self.assertAlmostEqual(boundary, 0.930555, places=5)
        reachable = burn_rate.plan_objective(
            with_tiers(tier("t", burn, "1h", "5m"), objective=round(boundary + 0.001, 6)))
        unreachable = burn_rate.plan_objective(
            with_tiers(tier("t", burn, "1h", "5m"), objective=round(boundary - 0.001, 6)))
        self.assertNotIn("G301", [f.code for f in reachable.errors])
        self.assertIn("G301", [f.code for f in unreachable.errors])

    def test_an_unreachable_tier_suppresses_the_policy_wide_comparisons(self) -> None:
        # Comparing tiers against one that cannot fire describes an
        # arrangement that does not exist.
        plan = burn_rate.plan_objective(
            with_tiers(tier("a", 14.4, "1h", "5m"), tier("b", 1, "3d", "6h", "ticket"),
                       objective=0.9))
        self.assertNotIn("G306", [f.code for f in plan.all_findings()])
        self.assertNotIn("G307", [f.code for f in plan.all_findings()])


class PolicyShapeTests(unittest.TestCase):
    def test_an_objective_with_no_tier_is_measured_and_never_reported_on(self) -> None:
        spec = ratio_objective()
        spec["alerting"]["tiers"] = []
        plan = burn_rate.plan_objective(objective_from(spec))
        self.assertEqual([f.code for f in plan.errors], ["G100"])
        self.assertEqual(plan.tiers, [])

    def test_two_tiers_sharing_a_name_render_to_one_alert(self) -> None:
        plan = burn_rate.plan_objective(
            with_tiers(tier("fast", 14.4, "1h", "5m"), tier("fast", 6, "6h", "30m")))
        self.assertIn("G200", [f.code for f in plan.errors])

    def test_a_slower_tier_at_a_higher_threshold_can_never_fire_first(self) -> None:
        # Shadowing: the earlier tier is triggered by every incident the later
        # one is, and never later, so the later one adds a second notification
        # and nothing else.
        plan = burn_rate.plan_objective(
            with_tiers(tier("fast", 6, "1h", "5m"), tier("slow", 14.4, "6h", "30m")))
        shadowed = [f for f in plan.warnings if f.code == "G306"]
        self.assertEqual(len(shadowed), 1)
        self.assertIn("tier slow", shadowed[0].where)

    def test_a_shadowed_tier_that_escalates_is_left_alone(self) -> None:
        # A page behind a ticket is still the first page. The check compares
        # urgency as well as coverage, and the direction of that comparison is
        # the whole finding: inverted, it flags the legitimate escalation and
        # passes the pure duplicate.
        escalating = burn_rate.plan_objective(
            with_tiers(tier("first", 6, "1h", "5m", "ticket"),
                       tier("second", 14.4, "6h", "30m", "page")))
        self.assertNotIn("G306", [f.code for f in escalating.warnings])

        duplicate = burn_rate.plan_objective(
            with_tiers(tier("first", 6, "1h", "5m", "page"),
                       tier("second", 14.4, "6h", "30m", "page")))
        self.assertIn("G306", [f.code for f in duplicate.warnings])

    def test_a_band_of_slow_burns_below_the_lowest_threshold_is_reported(self) -> None:
        plan = burn_rate.plan_objective(with_tiers(tier("fast", 14.4, "1h", "5m")))
        band = [f for f in plan.warnings if f.code == "G307"]
        self.assertEqual(len(band), 1)

    def test_a_tier_at_a_burn_rate_of_one_closes_the_band(self) -> None:
        # The honest defence of a tier that fires while the service is meeting
        # its objective: below it there is no sustained rate that spends the
        # budget unreported. It becomes a note rather than a warning.
        plan = burn_rate.plan_objective(
            with_tiers(tier("fast", 14.4, "1h", "5m"), tier("slow", 1, "3d", "6h", "ticket")))
        band = [f for f in plan.all_findings() if f.code == "G307"]
        self.assertEqual([f.severity for f in band], ["note"])

    def test_a_one_times_tier_is_a_budget_signal_rather_than_a_prediction(self) -> None:
        plan = burn_rate.plan_objective(
            with_tiers(tier("fast", 14.4, "1h", "5m"), tier("slow", 1, "3d", "6h", "ticket")))
        self.assertIn("G304", [f.code for f in plan.all_findings()])

    def test_a_paging_tier_too_slow_to_be_news_is_reported(self) -> None:
        # Floor = threshold x long window. At 1x a 0.5% budget over 3 days
        # that is about 22 minutes, well past the scale a page is answered on.
        plan = burn_rate.plan_objective(with_tiers(tier("slow", 1, "3d", "6h", "page")))
        self.assertIn("G308", [f.code for f in plan.warnings])

    def test_a_ticketing_policy_is_not_judged_against_the_paging_convention(self) -> None:
        plan = burn_rate.plan_objective(with_tiers(tier("slow", 1, "3d", "6h", "ticket")))
        self.assertNotIn("G308", [f.code for f in plan.warnings])

    def test_a_short_window_a_few_events_can_breach_is_reported(self) -> None:
        # 12 events/hour over 5 minutes is one event; a 7.2% threshold is then
        # met by a fraction of a failure, so the confirming window confirms
        # ordinary variation.
        sparse = ratio_objective()
        sparse["sli"]["sampling"]["expected_events_per_hour"] = 12
        sparse["alerting"]["tiers"] = [copy.deepcopy(FAST_TIER)]
        plan = burn_rate.plan_objective(objective_from(sparse))
        self.assertIn("G305", [f.code for f in plan.warnings])

    def test_a_well_sampled_short_window_is_not_reported(self) -> None:
        plan = burn_rate.plan_objective(with_tiers(FAST_TIER))
        self.assertNotIn("G305", [f.code for f in plan.warnings])

    def test_the_sparse_window_boundary_is_five_failed_events(self) -> None:
        # Pinned from both sides, because the figure is a judgement about when
        # a condition stops describing the service and starts describing
        # ordinary variation -- and a judgement left untested drifts.
        def failed_events_at(rate_per_hour: float) -> float:
            sparse = ratio_objective()
            sparse["sli"]["sampling"]["expected_events_per_hour"] = rate_per_hour
            sparse["alerting"]["tiers"] = [copy.deepcopy(FAST_TIER)]
            plan = burn_rate.plan_objective(objective_from(sparse))
            return plan.tiers[0].failed_events_to_breach_short

        self.assertAlmostEqual(failed_events_at(800), 4.8)
        self.assertAlmostEqual(failed_events_at(900), 5.4)
        self.assertIn("G305", [f.code for f in burn_rate.plan_objective(
            with_tiers(FAST_TIER, sli=dict(ratio_objective()["sli"],
                                           sampling={"expected_events_per_hour": 800}),
                       )).warnings])
        self.assertNotIn("G305", [f.code for f in burn_rate.plan_objective(
            with_tiers(FAST_TIER, sli=dict(ratio_objective()["sli"],
                                           sampling={"expected_events_per_hour": 900}),
                       )).warnings])

    def test_the_budget_note_is_always_present_on_a_plannable_objective(self) -> None:
        plan = burn_rate.plan_objective(with_tiers(FAST_TIER))
        self.assertIn("G309", [f.code for f in plan.all_findings()])


class ShippedSpecificationTests(unittest.TestCase):
    """The worked example is part of the repository's claims about itself."""

    def test_every_objective_in_the_example_can_be_planned(self) -> None:
        import yaml

        from _support import ROOT

        doc = yaml.safe_load((ROOT / "specs" / "example.yaml").read_text(encoding="utf-8"))
        plans = burn_rate.plan_objectives(base.objectives_in(doc, "example.yaml"))
        self.assertEqual(len(plans), 3)
        for plan in plans:
            with self.subTest(objective=plan.objective.key):
                self.assertTrue(plan.plannable, [f.render() for f in plan.errors])


class SerialisationTests(unittest.TestCase):
    def test_the_plan_document_is_json_and_carries_every_tier(self) -> None:
        plan = burn_rate.plan_objective(
            with_tiers(tier("fast", 14.4, "1h", "5m"), tier("slow", 1, "3d", "6h", "ticket")))
        document_text = json.dumps(plan.as_dict())
        restored = json.loads(document_text)
        self.assertEqual([t["name"] for t in restored["tiers"]], ["fast", "slow"])
        self.assertTrue(restored["plannable"])
        self.assertEqual(restored["budget"]["window_label"], "28d")

    def test_a_rate_below_a_tier_threshold_serialises_as_null_not_as_zero(self) -> None:
        # A 30x tier on a 0.5% budget fires at 15%, so the sampled 10% error
        # rate never reaches it. "Does not fire" must not arrive at a consumer
        # as "fires instantly".
        plan = burn_rate.plan_objective(with_tiers(tier("blunt", 30, "1h", "5m")))
        self.assertAlmostEqual(plan.tiers[0].threshold, 0.15)
        detection = plan.as_dict()["tiers"][0]["detection_seconds"]
        self.assertIsNotNone(detection["1"])
        self.assertIsNotNone(detection["0.5"])
        self.assertIsNone(detection["0.1"])


class FormattingTests(unittest.TestCase):
    def test_rates_are_rendered_at_a_precision_they_actually_have(self) -> None:
        self.assertEqual(burn_rate.format_rate(0.072), "7.20%")
        self.assertEqual(burn_rate.format_rate(0.005), "0.5000%")
        self.assertTrue(burn_rate.format_rate(0.0000025).endswith("e-06"))


if __name__ == "__main__":  # pragma: no cover - thin wrapper
    unittest.main()
