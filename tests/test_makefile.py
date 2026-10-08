"""The Makefile against the pipeline it claims to reproduce.

`make check` exists so that a change can be checked before it is pushed. That
is only true while its commands are the pipeline's commands, and nothing about
either file makes a divergence visible: a target that has drifted still runs,
still reports success, and reports it about something else. So the two are
compared here command by command, in both directions -- a gate the pipeline
runs and the Makefile does not is just as much a gap as the reverse.

The comparison is made on the EXPANDED recipe, with Make's variable references
resolved, because that is the string a shell would receive. Comparing the
written lines would pass for a target whose variables point somewhere else.
"""

from __future__ import annotations

import re
import unittest

import yaml

from _support import ROOT

MAKEFILE = (ROOT / "Makefile").read_text(encoding="utf-8")
WORKFLOW = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))

#: Which target is meant to reproduce which pipeline step. This mapping is the
#: only thing stated twice, and it is a mapping rather than a figure: the
#: commands themselves are read from the two files.
EQUIVALENTS = {
    "Validate every specification": "validate",
    "Run the suite": "test",
    "Render the budget report": "report",
    "Render the alerting rules": "rules",
    "Audit the policy against the objectives it governs": "audit",
    "pyflakes": "lint",
    "flake8": "lint",
    "yamllint": "lint",
    "terraform fmt": "fmt-check",
    "terraform init": "tf-init",
    "terraform validate": "tf-validate",
    "tflint": "tf-lint",
}

#: Pipeline steps with no target, each for a reason: the two JSON-parseability
#: checks and the rule-file structural check are inline scripts belonging to the
#: gate rather than to the deployment path, the dependency install is `deps`
#: under a different invocation, and the gate summary exists only in the
#: pipeline.
UNMAPPED_STEPS = {
    "Install check dependencies",
    "Confirm the JSON report is parseable on its own",
    "Confirm the plan document is parseable on its own",
    "Confirm every rule file parses as YAML",
    "Validate the shipped example on its own",
    "Report the state of every gate",
}


def _assignments(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for line in text.split("\n"):
        if line.startswith("\t") or line.lstrip().startswith("#"):
            continue
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*(\?=|:=|=)\s*(.*)$", line)
        if match:
            found[match.group(1)] = match.group(3).split("#")[0].strip()
    return found


VARIABLES = _assignments(MAKEFILE)


def expand(text: str) -> str:
    """Resolve `$(VAR)` the way Make would, then unescape `$$`."""
    for _ in range(8):
        replaced = re.sub(
            r"\$\(([A-Za-z_][A-Za-z0-9_]*)\)",
            lambda m: VARIABLES.get(m.group(1), m.group(0)),
            text,
        )
        if replaced == text:
            break
        text = replaced
    return text.replace("$$", "$")


def _rules(text: str) -> dict[str, list[str]]:
    """Target name to its recipe lines, continuations joined."""
    rules: dict[str, list[str]] = {}
    current: str | None = None
    pending = ""
    for line in text.split("\n"):
        if line.startswith("\t"):
            if current is None:
                continue
            body = line[1:]
            if body.rstrip().endswith("\\"):
                pending += body.rstrip()[:-1]
                continue
            rules[current].append((pending + body).strip())
            pending = ""
            continue
        pending = ""
        match = re.match(r"^([^\s:#=][^:=]*):(?!=)(.*)$", line)
        if match and not line.startswith("."):
            current = match.group(1).strip()
            rules.setdefault(current, [])
        elif line.strip() and not line.startswith(("\t", "#")):
            current = None
    return rules


RULES = _rules(MAKEFILE)


def _prerequisites(target: str) -> list[str]:
    match = re.search(rf"^{re.escape(target)}:([^=\n]*?)(?:\s*##.*)?$", MAKEFILE, re.MULTILINE)
    return match.group(1).split() if match else []


def _steps() -> dict[str, dict]:
    steps: dict[str, dict] = {}
    for job in WORKFLOW["jobs"].values():
        for step in job.get("steps", []):
            if "name" in step and "run" in step:
                steps[step["name"]] = step
    return steps


STEPS = _steps()


def normalise(command: str) -> list[str]:
    """One command per element, with the interpreter name and spacing settled."""
    out: list[str] = []
    for line in command.strip().split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = re.sub(r"^@|^-(?=\S)", "", line)
        line = re.sub(r"\bpython3(?:\.\d+)?\b", "python", line)
        line = re.sub(r"\s+", " ", line)
        out.append(line)
    return out


def strip_environment(commands: list[str]) -> tuple[list[str], dict[str, str]]:
    """Separate leading `VAR=value` assignments from the command they prefix."""
    stripped: list[str] = []
    environment: dict[str, str] = {}
    for command in commands:
        while True:
            match = re.match(r"^([A-Z][A-Z0-9_]*)=(\S*)\s+(.*)$", command)
            if not match:
                break
            environment[match.group(1)] = match.group(2).strip('"')
            command = match.group(3)
        stripped.append(command)
    return stripped, environment


class EquivalenceTests(unittest.TestCase):
    """Each mapped target runs the pipeline's command, character for character."""

    def test_every_mapped_step_exists_in_the_workflow(self) -> None:
        self.assertEqual(sorted(set(EQUIVALENTS) - set(STEPS)), [])

    def test_every_mapped_target_exists_in_the_makefile(self) -> None:
        self.assertEqual(sorted(set(EQUIVALENTS.values()) - set(RULES)), [])

    def test_no_pipeline_step_is_silently_unaccounted_for(self) -> None:
        accounted = set(EQUIVALENTS) | UNMAPPED_STEPS
        self.assertEqual(
            sorted(set(STEPS) - accounted),
            [],
            "a gate was added to the pipeline with no target and no stated reason",
        )

    def test_unmapped_steps_are_still_real_steps(self) -> None:
        self.assertEqual(sorted(UNMAPPED_STEPS - set(STEPS)), [])

    def test_each_target_runs_the_commands_its_step_runs(self) -> None:
        for target in sorted(set(EQUIVALENTS.values())):
            with self.subTest(target=target):
                expected: list[str] = []
                for step_name, mapped in EQUIVALENTS.items():
                    if mapped == target:
                        expected += normalise(STEPS[step_name]["run"])
                recipe = normalise(expand("\n".join(RULES[target])))
                recipe, _ = strip_environment(recipe)
                for command in expected:
                    self.assertIn(
                        command,
                        recipe,
                        f"the pipeline runs {command!r}; `make {target}` does not",
                    )

    def test_the_suite_target_sets_the_environment_the_gate_sets(self) -> None:
        recipe = normalise(expand("\n".join(RULES["test"])))
        _, environment = strip_environment(recipe)
        for name, value in STEPS["Run the suite"]["env"].items():
            self.assertEqual(
                str(environment.get(name)),
                str(value),
                f"the gate sets {name}={value} for the suite and the target does not",
            )


class CheckTargetTests(unittest.TestCase):
    """`make check` has to be the whole gate, not most of it."""

    def test_check_covers_every_mapped_target(self) -> None:
        reached: set[str] = set()
        frontier = _prerequisites("check")
        while frontier:
            target = frontier.pop()
            if target in reached:
                continue
            reached.add(target)
            frontier += _prerequisites(target)
        missing = set(EQUIVALENTS.values()) - reached
        self.assertEqual(sorted(missing), [], "`make check` does not reach every gated target")

    def test_check_does_not_reach_the_deployment_path(self) -> None:
        reached: set[str] = set()
        frontier = _prerequisites("check")
        while frontier:
            target = frontier.pop()
            if target in reached:
                continue
            reached.add(target)
            frontier += _prerequisites(target)
        for forbidden in ("plan", "apply", "fmt", "deps"):
            self.assertNotIn(
                forbidden,
                reached,
                f"`make check` reaches `{forbidden}`, which either rewrites the tree or needs an account",
            )


class SafetyTests(unittest.TestCase):
    """The properties that make a target trustworthy as a gate."""

    def test_the_recipe_shell_fails_on_the_first_error_and_through_a_pipe(self) -> None:
        # Each recipe line is its own shell, so without these a failing command
        # upstream of a pipe reports success and a compound line carries on past
        # its first failure.
        match = re.search(r"^\.SHELLFLAGS\s*:=\s*(.*)$", MAKEFILE, re.MULTILINE)
        self.assertIsNotNone(match, ".SHELLFLAGS is not set, so recipes run with sh defaults")
        flags = match.group(1)
        for flag in ("-e", "pipefail", "-c"):
            self.assertIn(flag, flags, f".SHELLFLAGS does not carry {flag}: {flags!r}")
        self.assertIsNotNone(
            re.search(r"^SHELL\s*:=\s*\S*bash", MAKEFILE, re.MULTILINE),
            "pipefail is a bash option and SHELL is not set to bash",
        )

    def test_no_recipe_ignores_a_failure(self) -> None:
        for target, recipe in RULES.items():
            for line in recipe:
                self.assertFalse(
                    line.startswith("-"),
                    f"`{target}` ignores a failure with a `-` prefix, so it cannot fail",
                )
                self.assertNotIn(
                    "|| true",
                    line,
                    f"`{target}` swallows a failure with `|| true`, so it cannot fail",
                )

    def test_apply_requires_a_saved_plan_and_a_confirmation(self) -> None:
        recipe = " ".join(RULES["apply"])
        self.assertIn(expand("$(PLAN)"), expand(recipe))
        self.assertIn("CONFIRM", recipe)
        self.assertNotIn("-auto-approve", recipe)

    def test_formatting_is_checked_by_a_target_that_does_not_repair_it(self) -> None:
        self.assertNotIn("-check", " ".join(RULES["fmt"]))
        self.assertIn("-check", " ".join(RULES["fmt-check"]))

    def test_every_phony_target_is_documented_and_every_rule_is_phony(self) -> None:
        declared = set(re.search(r"^\.PHONY:((?:.*\\\n)*.*)$", MAKEFILE, re.MULTILINE)
                       .group(1).replace("\\\n", " ").split())
        documented = set(re.findall(r"^([a-z][a-z-]*):.*?##", MAKEFILE, re.MULTILINE))
        self.assertEqual(sorted(declared - documented), [], "phony target with no help line")
        self.assertEqual(sorted(documented - declared), [], "documented target not declared phony")
        # The plan file is the one real file target, so it is the one exception.
        concrete = {t for t in RULES if not t.startswith("$")} - declared
        self.assertEqual(sorted(concrete), [], "a rule that is neither phony nor the plan file")


if __name__ == "__main__":  # pragma: no cover - thin wrapper
    unittest.main()
