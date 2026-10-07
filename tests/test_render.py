"""Rendering a plan into something a backend will accept.

The renderers are where a correct plan becomes a wrong file: an unescaped
quote, a name a backend rejects, or a threshold written in exponent form all
produce output that is either refused on sight or -- worse -- accepted and
different from what the specification said.
"""

from __future__ import annotations

import copy
import unittest

import yaml

from _support import ROOT, document, ratio_objective, threshold_objective

import base
import burn_rate
import render


def objective_from(spec: dict) -> base.Objective:
    doc = document(spec)
    return base.Objective.from_spec(doc, doc["objectives"][0], document="spec.yaml")


class EscapingTests(unittest.TestCase):
    def test_quotes_and_backslashes_are_escaped_for_hcl(self) -> None:
        self.assertEqual(render.hcl_string('a "b"'), 'a \\"b\\"')
        self.assertEqual(render.hcl_string("a\\b"), "a\\\\b")

    def test_terraform_interpolation_openers_are_neutralised(self) -> None:
        # `${` and `%{` in a rendered PromQL fragment would otherwise be read
        # by Terraform as an expression against variables that do not exist --
        # or, if one happens to exist, as a value nobody wrote.
        self.assertEqual(render.hcl_string("rate(x[${window}])"), "rate(x[$${window}])")
        self.assertEqual(render.hcl_string("%{if true}"), "%%{if true}")

    def test_the_backslash_is_escaped_before_the_quote(self) -> None:
        # Order matters: escaping the quote first and the backslash second
        # would double the backslash the first step just added, and close the
        # string one character early.
        self.assertEqual(render.hcl_string('\\"'), '\\\\\\"')

    def test_a_newline_in_author_prose_cannot_end_a_comment(self) -> None:
        # The fault this exists for: a title with a newline would put the rest
        # of the line into a generated file as code, in a language the
        # surrounding lines are not.
        flattened = render.comment_text("first line\nresource \"aws_iam_policy\" \"x\" {}")
        self.assertNotIn("\n", flattened)
        self.assertTrue(flattened.startswith("first line resource"))

    def test_comment_text_collapses_all_whitespace_and_bounds_length(self) -> None:
        self.assertEqual(render.comment_text("a   b\t\tc"), "a b c")
        self.assertEqual(len(render.comment_text("x" * 500, limit=40)), 40)
        self.assertTrue(render.comment_text("x" * 500, limit=40).endswith("…"))


class NamingTests(unittest.TestCase):
    def test_a_prometheus_alert_name_excludes_the_hyphen(self) -> None:
        # An alert name has to be a legal metric name, and every identifier in
        # the schema is built from a character class that includes the hyphen.
        name = render.prometheus_alert_name("checkout-api", "refund-settlement", "fast")
        self.assertNotIn("-", name)
        self.assertTrue(name.replace("_", "").replace(":", "").isalnum())

    def test_the_transliteration_is_reversible_in_the_only_way_that_matters(self) -> None:
        # The schema's patterns exclude the underscore, so no two legal
        # identifiers can collide by being mapped onto one name.
        first = render.prometheus_alert_name("a-b", "c")
        second = render.prometheus_alert_name("a", "b-c")
        self.assertEqual(first, second)  # same joined identifier, same name
        self.assertNotEqual(
            render.prometheus_alert_name("ab", "c"), render.prometheus_alert_name("a", "bc"))

    def test_a_terraform_label_is_the_same_identifier_with_underscores(self) -> None:
        self.assertEqual(render.terraform_label("checkout-api", "fast"), "checkout_api_fast")

    def test_empty_parts_are_dropped_rather_than_leaving_a_double_separator(self) -> None:
        self.assertEqual(render.prometheus_alert_name("a", "", "b"), "a_b")
        self.assertEqual(render.terraform_label("a", "", "b"), "a_b")


class ThresholdLiteralTests(unittest.TestCase):
    def test_a_small_threshold_is_never_written_in_exponent_form(self) -> None:
        # A threshold is the one figure in a generated alarm that gets checked
        # by eye, and `7.2e-05` is where a reviewer stops reading.
        literal = render._threshold_literal(0.000072)
        self.assertNotIn("e", literal)
        self.assertEqual(float(literal), 0.000072)

    def test_trailing_zeros_are_trimmed_without_losing_the_number(self) -> None:
        self.assertEqual(render._threshold_literal(0.072), "0.072")
        self.assertEqual(render._threshold_literal(1.0), "1")
        self.assertEqual(render._threshold_literal(0.0), "0")


class PrometheusArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.objective = objective_from(ratio_objective())
        self.plan, self.compiled, self.artifact = render.render_objective(
            self.objective, "prometheus", "prometheus")

    def rules(self) -> list[dict]:
        loaded = yaml.safe_load(self.artifact.content)
        self.assertIn("groups", loaded)
        return [r for group in loaded["groups"] for r in group["rules"]]

    def burn_rules(self) -> list[dict]:
        return [r for r in self.rules() if not r["expr"].startswith("absent(")]

    def test_the_rule_file_is_serialised_yaml_not_templated_text(self) -> None:
        # A rule's payload is PromQL full of braces, quoted label values and
        # comparison operators. Interpolating one into a text template is how
        # a rule file becomes invalid, or valid and different. Round-tripping
        # it through the parser is the only check that says it is neither.
        self.assertEqual(len(self.burn_rules()), len(self.plan.tiers))

    def test_one_staleness_rule_is_emitted_alongside_the_tiers(self) -> None:
        # An absent series is not a zero, and no burn-rate tier can report its
        # own missing denominator -- so the objective carries one rule that
        # does, over the shortest window any tier uses.
        stale = [r for r in self.rules() if r["expr"].startswith("absent(")]
        self.assertEqual(len(stale), 1)
        self.assertIn("[5m]", stale[0]["expr"])

    def test_every_tier_rule_carries_both_window_conditions(self) -> None:
        for rule in self.burn_rules():
            with self.subTest(alert=rule["alert"]):
                self.assertIn("\nand\n", rule["expr"])
                self.assertTrue(rule["labels"])
                self.assertTrue(rule["annotations"])

    def test_the_two_conditions_stay_on_separate_lines(self) -> None:
        # The literal-block representer exists so a reader can see the long
        # and the short condition as two statements rather than as one escaped
        # line, which is how a rule file stops being reviewable.
        self.assertIn("|", self.artifact.content)
        for rule in self.burn_rules():
            with self.subTest(alert=rule["alert"]):
                self.assertEqual(len(rule["expr"].strip().split("\n")), 3)

    def test_the_rendered_window_spans_differ_between_the_two_conditions(self) -> None:
        # The fault the `$window` placeholder exists to prevent: one literal
        # range evaluated at every threshold collapses the whole ladder into
        # one alert repeated at three levels.
        first = self.burn_rules()[0]
        self.assertIn("[1h]", first["expr"])
        self.assertIn("[5m]", first["expr"])

    def test_alert_identities_within_one_artifact_are_distinct(self) -> None:
        self.assertEqual(len(set(self.artifact.identities)), len(self.artifact.identities))


class CloudWatchArtifactTests(unittest.TestCase):
    def test_a_promql_indicator_is_refused_by_the_cloudwatch_source(self) -> None:
        # Refused, not approximated: the two dialects compare different
        # quantities, and a translation that merely looks plausible deploys.
        plan, compiled, artifact = render.render_objective(
            objective_from(ratio_objective()), "cloudwatch", "cloudwatch")
        self.assertIsNone(artifact)
        self.assertTrue(compiled.errors)

    def test_a_tier_becomes_two_alarms_and_a_composite(self) -> None:
        # An alarm evaluates a single period, so the long and the short window
        # cannot be two conditions of one alarm.
        spec = ratio_objective()
        spec["sli"]["good_query"] = "AWS/ApplicationELB/RequestCount:Sum[LoadBalancer=app/x]"
        spec["sli"]["valid_query"] = "AWS/ApplicationELB/RequestCount:Sum[LoadBalancer=app/y]"
        spec["alerting"]["tiers"] = [spec["alerting"]["tiers"][0]]
        plan, compiled, artifact = render.render_objective(
            objective_from(spec), "cloudwatch", "cloudwatch")
        self.assertIsNotNone(artifact, [f.render() for f in compiled.all_findings()])
        self.assertEqual(artifact.content.count('resource "aws_cloudwatch_metric_alarm"'), 2)
        self.assertEqual(
            artifact.content.count('resource "aws_cloudwatch_composite_alarm"'), 1)

    def test_the_tumbling_window_caveat_travels_with_the_alarms(self) -> None:
        # Every detection figure assumes a sliding window and a CloudWatch
        # alarm period is tumbling, so the caveat belongs in the file as well
        # as in the report.
        finding = render.sliding_window_caveat("somewhere")
        self.assertEqual(finding.code, "G411")


class ReportArtifactTests(unittest.TestCase):
    def test_the_report_renders_without_any_source(self) -> None:
        # The report is the arithmetic, and the arithmetic does not depend on
        # which backend the indicator is written for.
        plan, compiled, artifact = render.render_objective(
            objective_from(threshold_objective()), "report", None)
        self.assertIsNone(compiled)
        self.assertIsNotNone(artifact)
        self.assertIn("checkout", artifact.content)

    def test_the_report_quotes_the_same_cost_figure_the_plan_computed(self) -> None:
        objective = objective_from(ratio_objective())
        plan, _, artifact = render.render_objective(objective, "report", None)
        for tier in plan.tiers:
            with self.subTest(tier=tier.name):
                self.assertIn(f"{tier.budget_fraction_at_detection:.2%}", artifact.content)


class RegisteredTargetTests(unittest.TestCase):
    def test_every_declared_target_has_a_renderer(self) -> None:
        self.assertEqual(set(render.TARGETS), set(render.RENDERERS))

    def test_every_target_names_a_registered_source_or_none(self) -> None:
        for target, source in render.TARGET_SOURCE.items():
            with self.subTest(target=target):
                self.assertIn(target, render.TARGETS)
                if source is not None:
                    self.assertIn(source, render.REGISTERED_SOURCES)

    def test_every_template_the_renderers_load_exists(self) -> None:
        import re

        source = (ROOT / "generator" / "render.py").read_text(encoding="utf-8")
        for name in set(re.findall(r'load_template\(\s*"([^"]+)"', source)):
            with self.subTest(template=name):
                self.assertTrue((ROOT / "templates" / name).is_file())

    def test_every_shipped_template_is_loaded_by_the_renderers(self) -> None:
        source = (ROOT / "generator" / "render.py").read_text(encoding="utf-8")
        for path in sorted((ROOT / "templates").iterdir()):
            with self.subTest(template=path.name):
                self.assertIn(path.name, source)


class ShippedSpecificationRenderTests(unittest.TestCase):
    def test_every_objective_in_the_example_renders_a_report_and_a_rule_file(self) -> None:
        doc = yaml.safe_load((ROOT / "specs" / "example.yaml").read_text(encoding="utf-8"))
        for objective in base.objectives_in(doc, "example.yaml"):
            for target, source in (("report", None), ("prometheus", "prometheus")):
                with self.subTest(objective=objective.key, target=target):
                    _, _, artifact = render.render_objective(objective, target, source)
                    self.assertIsNotNone(artifact)
                    self.assertTrue(artifact.content.strip())

    def test_no_rendered_rule_file_carries_an_unsubstituted_placeholder(self) -> None:
        doc = yaml.safe_load((ROOT / "specs" / "example.yaml").read_text(encoding="utf-8"))
        for objective in base.objectives_in(doc, "example.yaml"):
            with self.subTest(objective=objective.key):
                _, _, artifact = render.render_objective(objective, "prometheus", "prometheus")
                self.assertNotIn("$window", artifact.content)
                self.assertNotIn("@{", artifact.content)


class PlanConsistencyTests(unittest.TestCase):
    def test_rendering_does_not_change_the_plan_it_renders(self) -> None:
        objective = objective_from(ratio_objective())
        direct = burn_rate.plan_objective(objective).as_dict()
        through_renderer = render.render_objective(objective, "report", None)[0].as_dict()
        self.assertEqual(direct, through_renderer)

    def test_a_tier_the_plan_refuses_renders_nothing(self) -> None:
        spec = ratio_objective(objective=0.9)
        spec["alerting"]["tiers"] = [copy.deepcopy(spec["alerting"]["tiers"][0])]
        plan, _, artifact = render.render_objective(objective_from(spec), "report", None)
        self.assertFalse(plan.plannable)
        self.assertIsNone(artifact)


if __name__ == "__main__":  # pragma: no cover - thin wrapper
    unittest.main()
