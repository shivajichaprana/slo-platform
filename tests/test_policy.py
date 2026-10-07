"""What an error-budget policy permits, and the parts of it that are not a level.

The arithmetic here is small; the failure modes are not. A gate that reads the
level alone is a lagging control, a gate with no exit threshold is a flapping
one, and a gate whose failure direction is implicit decides every deployment
during the one outage it exists for.
"""

from __future__ import annotations

import copy
import unittest
from datetime import datetime, timedelta, timezone

import yaml

from _support import ROOT, document, ratio_objective

import base
import budget as budget_module
import burn_rate

NOW = datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc)


def policy_document(**gate_overrides) -> dict:
    gate = {
        "unit": "checkout",
        "objectives": ["checkout/availability"],
        "combine": "any",
        "on_unreadable_budget": "closed",
        "rules": [
            {"name": "freeze", "action": "freeze",
             "when": {"remaining_below": 0.10}, "clear_above": 0.30},
            {"name": "review", "action": "review",
             "when": {"exhaustion_within": "48h"}, "clear_above": 0.60},
        ],
    }
    gate.update(copy.deepcopy(gate_overrides))
    return {
        "apiVersion": "slo.platform/v1",
        "kind": "ErrorBudgetPolicy",
        "metadata": {"name": "test-policy", "owner": "payments-platform"},
        "defaults": {"max_budget_age": "15m", "exemption_max_duration": "24h"},
        "gates": [gate],
    }


def objectives() -> dict[str, base.Objective]:
    doc = document(ratio_objective())
    objective = base.Objective.from_spec(doc, doc["objectives"][0], document="spec.yaml")
    return {"checkout/availability": objective}


def observation(consumed: float, burn: float | None = None,
                age_seconds: int = 0) -> dict[str, budget_module.Observation]:
    return {
        "checkout/availability": budget_module.Observation(
            service="checkout", name="availability", consumed_fraction=consumed,
            computed_at=NOW - timedelta(seconds=age_seconds), burn_rate=burn,
        ),
    }


class ExhaustionArithmeticTests(unittest.TestCase):
    def test_the_projection_is_the_consumption_identity_read_for_time(self) -> None:
        window = 28 * 86400
        for remaining, burn in ((1.0, 1.0), (0.4, 10.0), (0.05, 14.4)):
            with self.subTest(remaining=remaining, burn=burn):
                self.assertAlmostEqual(
                    budget_module.time_to_exhaustion_seconds(remaining, burn, window),
                    remaining * window / burn,
                )

    def test_a_budget_already_spent_is_exhausted_now_not_never(self) -> None:
        self.assertEqual(budget_module.time_to_exhaustion_seconds(0.0, 2.0, 100), 0.0)
        self.assertEqual(budget_module.time_to_exhaustion_seconds(-0.2, 2.0, 100), 0.0)

    def test_a_burn_rate_of_zero_never_exhausts_the_budget(self) -> None:
        self.assertIsNone(budget_module.time_to_exhaustion_seconds(0.5, 0.0, 100))

    def test_the_quantum_is_one_event_expressed_as_a_fraction_of_the_budget(self) -> None:
        # The same quantity the validator calls a sampling floor, used here for
        # a different purpose: it is the smallest step the remaining fraction
        # can take, and a hysteresis band narrower than a few of them is a flap
        # dressed up as a band.
        objective = objectives()["checkout/availability"]
        computed = burn_rate.Budget.of(objective)
        self.assertAlmostEqual(budget_module.budget_quantum(computed), 1 / computed.events)

    def test_an_unsampled_budget_has_no_quantum(self) -> None:
        spec = ratio_objective()
        del spec["sli"]["sampling"]
        doc = document(spec)
        objective = base.Objective.from_spec(doc, doc["objectives"][0])
        self.assertIsNone(budget_module.budget_quantum(burn_rate.Budget.of(objective)))


class ActionLadderTests(unittest.TestCase):
    """The order of the four actions is the only thing that ranks them."""

    def test_the_ladder_runs_from_permissive_to_restrictive(self) -> None:
        self.assertEqual(
            budget_module.ACTION_ORDER, ("allow", "notify", "review", "freeze"))

    def test_review_outranks_notify_and_freeze_outranks_both(self) -> None:
        # The pair a permuted ladder gets wrong: a gate that should hold a
        # deployment for review instead merely notifies, and nothing in the
        # decision document looks unusual.
        self.assertEqual(budget_module._worst(["notify", "review"]), "review")
        self.assertEqual(budget_module._worst(["review", "freeze"]), "freeze")
        self.assertEqual(budget_module._worst(["allow", "notify"]), "notify")

    def test_only_the_top_two_actions_block_a_deployment(self) -> None:
        self.assertEqual(budget_module.BLOCKING_ACTIONS, ("review", "freeze"))
        self.assertEqual(
            list(budget_module.BLOCKING_ACTIONS), list(budget_module.ACTION_ORDER[-2:]))


class CalendarBoundaryTests(unittest.TestCase):
    def test_a_month_ends_at_the_first_instant_of_the_next_one(self) -> None:
        end, problem = budget_module.period_end(NOW, "month", "UTC")
        self.assertIsNone(problem)
        self.assertEqual(end, datetime(2026, 4, 1, tzinfo=timezone.utc))

    def test_a_quarter_rolls_over_at_the_year_boundary(self) -> None:
        december = datetime(2026, 12, 20, tzinfo=timezone.utc)
        end, _ = budget_module.period_end(december, "quarter", "UTC")
        self.assertEqual(end, datetime(2027, 1, 1, tzinfo=timezone.utc))

    def test_an_unknown_zone_is_reported_rather_than_silently_becoming_utc(self) -> None:
        _, problem = budget_module.period_end(NOW, "month", "Mars/Olympus")
        self.assertIsNotNone(problem)


class DocumentRefusalTests(unittest.TestCase):
    def parse(self, doc: dict) -> budget_module.Policy:
        return budget_module.parse_policy(doc, "rules.yaml")

    def test_the_worked_policy_parses(self) -> None:
        self.assertTrue(self.parse(policy_document()).gates)

    def test_a_gate_without_a_failure_direction_is_refused_at_parse(self) -> None:
        # Not warned about: without it there is no defined behaviour to
        # evaluate when a budget cannot be read.
        doc = policy_document()
        del doc["gates"][0]["on_unreadable_budget"]
        with self.assertRaises(budget_module.PolicyDocumentError):
            self.parse(doc)

    def test_an_unknown_failure_direction_is_refused(self) -> None:
        with self.assertRaises(budget_module.PolicyDocumentError):
            self.parse(policy_document(on_unreadable_budget="maybe"))

    def test_an_unknown_action_is_refused(self) -> None:
        doc = policy_document()
        doc["gates"][0]["rules"][0]["action"] = "shout"
        with self.assertRaises(budget_module.PolicyDocumentError):
            self.parse(doc)

    def test_an_objective_reference_without_a_service_is_refused(self) -> None:
        with self.assertRaises(budget_module.PolicyDocumentError):
            self.parse(policy_document(objectives=["availability"]))


class AuditTests(unittest.TestCase):
    def audit(self, doc: dict) -> set[str]:
        policy = budget_module.parse_policy(doc, "rules.yaml")
        return {f.code for f in budget_module.audit_policy(policy, objectives(), NOW)}

    def test_a_gate_reading_only_the_level_is_a_lagging_control(self) -> None:
        # A service at 30% remaining with no burn never exhausts its budget
        # and freezing it achieves nothing; one at 60% burning at 10x is not
        # caught by a rule about 30%.
        doc = policy_document(rules=[
            {"name": "freeze", "action": "freeze",
             "when": {"remaining_below": 0.10}, "clear_above": 0.30},
        ])
        self.assertIn("P301", self.audit(doc))

    def test_a_gate_with_a_projection_rule_is_not_reported_as_lagging(self) -> None:
        self.assertNotIn("P301", self.audit(policy_document()))

    def test_a_rule_with_no_exit_threshold_is_reported(self) -> None:
        doc = policy_document()
        del doc["gates"][0]["rules"][0]["clear_above"]
        self.assertIn("P302", self.audit(doc))

    def test_an_exit_at_the_entry_threshold_is_the_likeliest_typo_and_a_pure_flap(self) -> None:
        doc = policy_document()
        doc["gates"][0]["rules"][0]["clear_above"] = 0.10
        self.assertIn("P302", self.audit(doc))

    def test_a_gate_governing_an_objective_nothing_defines_is_reported(self) -> None:
        self.assertIn("P201", self.audit(policy_document(objectives=["checkout/nonexistent"])))

    def test_two_gates_over_one_unit_are_reported(self) -> None:
        doc = policy_document()
        doc["gates"].append(copy.deepcopy(doc["gates"][0]))
        self.assertIn("P200", self.audit(doc))

    def test_the_shipped_policy_audits_clean_at_error_and_warning(self) -> None:
        policy = budget_module.read_policy(ROOT / "policy" / "rules.yaml")
        collected = budget_module.collect_objectives(ROOT / "specs", "*.yaml")
        findings = budget_module.audit_policy(policy, collected, NOW)
        self.assertEqual([f.render() for f in findings if f.severity != "note"], [])


class DecisionTests(unittest.TestCase):
    def decide(self, consumed: float, burn: float | None = None, *,
               previous: dict | None = None, age_seconds: int = 0,
               **gate_overrides) -> budget_module.Decision:
        policy = budget_module.parse_policy(policy_document(**gate_overrides), "rules.yaml")
        return budget_module.evaluate_gate(
            policy.gates[0], objectives(),
            observation(consumed, burn, age_seconds), NOW, previous)

    def test_a_healthy_budget_permits_deployment(self) -> None:
        decision = self.decide(0.10, 0.5)
        self.assertEqual(decision.state, "allow")
        self.assertTrue(decision.permits_deployment)

    def test_a_spent_budget_freezes_the_gate(self) -> None:
        decision = self.decide(0.95, 0.5)
        self.assertEqual(decision.state, "freeze")
        self.assertFalse(decision.permits_deployment)

    def test_a_fast_burn_blocks_before_the_level_rule_would(self) -> None:
        # 50% remaining is nowhere near the freeze threshold, but at 14.4x it
        # is gone in under two days -- which is the whole argument for the
        # projection being the primary condition.
        decision = self.decide(0.50, 14.4)
        self.assertEqual(decision.state, "review")

    def test_release_passes_through_each_rules_exit_in_turn(self) -> None:
        # The staircase: one exit for the whole gate would jump from freeze to
        # allow on a single crossing, unblocking a pipeline at the moment the
        # budget is least able to absorb a bad release.
        frozen = {"state": "freeze", "sequence": 3}
        self.assertEqual(self.decide(0.95, 0.1, previous=frozen).state, "freeze")
        self.assertEqual(self.decide(0.80, 0.1, previous=frozen).state, "freeze")
        recovering = self.decide(0.50, 0.1, previous=frozen)
        self.assertIn(recovering.state, ("review", "notify"))
        self.assertEqual(self.decide(0.05, 0.1, previous=frozen).state, "allow")

    def test_without_a_previous_decision_only_entry_thresholds_are_applied(self) -> None:
        decision = self.decide(0.50, 0.1)
        self.assertFalse(decision.hysteresis_applied)
        self.assertTrue(self.decide(0.50, 0.1, previous={"state": "allow"}).hysteresis_applied)

    def test_a_stale_figure_hands_the_decision_to_the_failure_direction(self) -> None:
        closed = self.decide(0.10, 0.1, age_seconds=3600)
        self.assertFalse(closed.permits_deployment)
        self.assertTrue(all(not v.readable for v in closed.verdicts))

        opened = self.decide(0.10, 0.1, age_seconds=3600, on_unreadable_budget="open")
        self.assertTrue(opened.permits_deployment)

    def test_a_figure_dated_in_the_future_is_unreadable_too(self) -> None:
        decision = self.decide(0.10, 0.1, age_seconds=-3600)
        self.assertTrue(all(not v.readable for v in decision.verdicts))

    def test_the_sequence_number_advances_from_the_published_decision(self) -> None:
        # The published decision is the next evaluation's input, because
        # hysteresis needs the state the gate is in and a stateless evaluator
        # cannot invent it.
        self.assertEqual(self.decide(0.1, 0.1, previous={"state": "allow", "sequence": 7})
                         .sequence, 8)

    def test_the_decision_document_round_trips_as_json(self) -> None:
        import json

        decision = self.decide(0.95, 0.5)
        restored = json.loads(json.dumps(decision.as_dict()))
        self.assertEqual(restored["state"], "freeze")
        self.assertEqual(restored["unit"], "checkout")


class CombineModeTests(unittest.TestCase):
    """`any` and `all` differ only when the governed budgets disagree."""

    def setUp(self) -> None:
        doc = document(ratio_objective(), ratio_objective(name="latency", objective=0.99))
        self.objectives = {
            f"checkout/{spec['name']}": base.Objective.from_spec(doc, spec, document="spec.yaml")
            for spec in doc["objectives"]
        }

    def observations(self) -> dict[str, budget_module.Observation]:
        # One budget all but gone, one barely touched.
        return {
            "checkout/availability": budget_module.Observation(
                service="checkout", name="availability", consumed_fraction=0.97,
                computed_at=NOW, burn_rate=0.1),
            "checkout/latency": budget_module.Observation(
                service="checkout", name="latency", consumed_fraction=0.01,
                computed_at=NOW, burn_rate=0.1),
        }

    def decide(self, combine: str) -> budget_module.Decision:
        doc = policy_document(
            objectives=["checkout/availability", "checkout/latency"], combine=combine)
        policy = budget_module.parse_policy(doc, "rules.yaml")
        return budget_module.evaluate_gate(
            policy.gates[0], self.objectives, self.observations(), NOW)

    def test_any_takes_the_most_restrictive_action_reached(self) -> None:
        self.assertEqual(self.decide("any").state, "freeze")

    def test_all_requires_every_governed_budget_to_agree(self) -> None:
        # Deliberately lenient: one exhausted budget among several blocks
        # nothing, which is why adding an objective to an `all` gate can only
        # ever loosen it. Collapsing this into `any` would make the two modes
        # indistinguishable and the choice meaningless.
        self.assertEqual(self.decide("all").state, "allow")

    def test_the_two_modes_agree_when_the_budgets_do(self) -> None:
        for combine in budget_module.COMBINE_MODES:
            with self.subTest(combine=combine):
                doc = policy_document(
                    objectives=["checkout/availability", "checkout/latency"], combine=combine)
                policy = budget_module.parse_policy(doc, "rules.yaml")
                spent = {
                    ref: budget_module.Observation(
                        service="checkout", name=ref.split("/")[1], consumed_fraction=0.97,
                        computed_at=NOW, burn_rate=0.1)
                    for ref in self.objectives
                }
                decision = budget_module.evaluate_gate(
                    policy.gates[0], self.objectives, spent, NOW)
                self.assertEqual(decision.state, "freeze")

    def test_an_unreadable_budget_is_a_floor_over_the_combination(self) -> None:
        # Readability and `combine` are independent axes. Folding the first
        # into the second makes a deliberately lenient gate strict for a
        # reason that has nothing to do with its objectives.
        doc = policy_document(
            objectives=["checkout/availability", "checkout/latency"], combine="all",
            on_unreadable_budget="closed")
        policy = budget_module.parse_policy(doc, "rules.yaml")
        partial = {"checkout/availability": self.observations()["checkout/availability"]}
        decision = budget_module.evaluate_gate(
            policy.gates[0], self.objectives, partial, NOW)
        self.assertFalse(decision.permits_deployment)


class ShippedPolicyTests(unittest.TestCase):
    def test_the_worked_policy_is_valid_yaml_and_names_only_known_actions(self) -> None:
        doc = yaml.safe_load((ROOT / "policy" / "rules.yaml").read_text(encoding="utf-8"))
        policy = budget_module.parse_policy(doc, "rules.yaml")
        for gate in policy.gates:
            with self.subTest(unit=gate.unit):
                self.assertIn(gate.on_unreadable_budget, budget_module.FAIL_DIRECTIONS)
                self.assertIn(gate.combine, budget_module.COMBINE_MODES)
                for rule in gate.rules:
                    self.assertIn(rule.action, budget_module.ACTION_ORDER)

    def test_every_objective_the_policy_governs_is_defined_by_a_specification(self) -> None:
        # The sharpest plan-time refusal, asserted here as a property of the
        # two shipped documents rather than of the Terraform that enforces it.
        policy = budget_module.read_policy(ROOT / "policy" / "rules.yaml")
        collected = budget_module.collect_objectives(ROOT / "specs", "*.yaml")
        for gate in policy.gates:
            for ref in gate.objectives:
                with self.subTest(gate=gate.unit, objective=ref):
                    self.assertIn(ref, collected)


if __name__ == "__main__":  # pragma: no cover - thin wrapper
    unittest.main()
