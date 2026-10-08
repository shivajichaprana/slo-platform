# Every check this repository makes of itself, and the deployment path, as one
# entry point.
#
# The targets are not a convenience wrapper around the pipeline: they are the
# SAME command strings the pipeline runs, and `tests/test_makefile.py` asserts
# that, command by command, against `.github/workflows/ci.yml`. A Makefile that
# has drifted from the gate is worse than none, because it reports a clean tree
# that the gate will reject -- and the drift is silent in both directions.
#
# Nothing here reaches an account. Every generator and adapter compiles a
# payload rather than sending one, and `terraform init -backend=false` needs the
# providers but not credentials, so `make check` is the whole gate offline. The
# two targets that do reach an account are `plan` and `apply`, and they are the
# only ones that require anything of the caller.

# Overridable, so a checkout with a virtualenv or a pinned binary needs no edit
# here. `?=` throughout: `make PYTHON=python3.13 test` is a supported way to run
# the suite on the other end of the supported range.
PYTHON    ?= python3
TERRAFORM ?= terraform
TFLINT    ?= tflint

SPEC_DIR ?= specs
RULES    ?= policy/rules.yaml
OUT      ?= generated
PLAN     ?= slo.tfplan

# Each recipe line is its own shell, so a pipeline's exit status is the LAST
# command's unless pipefail is set -- which is how a `| python -c` check comes
# to report success for a generator that failed. `-u` as well, because an
# unset override would otherwise expand to an empty argument and change what is
# scanned rather than failing.
SHELL       := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c

# No target names a file it creates, apart from the plan, so every one is
# phony: without this a directory called `test` would silently make the suite
# up to date.
.PHONY: help deps validate test report rules alarms generate audit lint fmt \
        fmt-check tf-init tf-validate tf-lint check plan apply clean

.DEFAULT_GOAL := help

help:  ## Show this list
	@grep -hE '^[a-z][a-z-]*:.*?##' $(MAKEFILE_LIST) \
		| sort \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[1m%-12s\033[0m %s\n", $$1, $$2}'

deps:  ## Install what the checks need
	$(PYTHON) -m pip install --disable-pip-version-check -r requirements-dev.txt

# ---------------------------------------------------------------------------
# The specifications and the policy
# ---------------------------------------------------------------------------

validate:  ## Validate every specification, warnings fatal
	$(PYTHON) tools/validate-specs.py $(SPEC_DIR) --strict

audit:  ## Audit the error-budget policy against the objectives it governs
	$(PYTHON) policy/budget.py $(SPEC_DIR) --rules $(RULES) --strict

# ---------------------------------------------------------------------------
# Generation. Artifacts go to $(OUT), which is gitignored: anything rendered
# from a specification is rebuilt rather than reviewed as a diff, and an edited
# threshold is a threshold that no longer follows from its objective.
# ---------------------------------------------------------------------------

report:  ## Render the budget report
	$(PYTHON) generator/render.py $(SPEC_DIR) --render report --out $(OUT)/report

rules:  ## Render Prometheus alerting rules
	$(PYTHON) generator/render.py $(SPEC_DIR) --render prometheus --out $(OUT)/rules

# Fails on the shipped specifications, and that is the adapter working: their
# indicators are written in PromQL, which CloudWatch does not evaluate, so S100
# refuses them rather than passing the strings through to an API that accepts
# them as opaque and answers with an absence of data an alarm reads as health.
# The target exists for a repository whose indicators are CloudWatch metrics.
alarms:  ## Render CloudWatch alarms (needs CloudWatch-dialect indicators)
	$(PYTHON) generator/render.py $(SPEC_DIR) --render cloudwatch --out $(OUT)/alarms

# The two targets every specification in this repository can satisfy, which is
# why `alarms` is not among them. Deliberately not --strict either: the
# generator's warnings describe what a burn-rate policy cannot see, which is a
# property of the objective its author chose rather than a fault in the tree,
# and a correct single-tier policy would make the gate unpassable.
generate: report rules  ## Render every artifact the shipped specifications support

# ---------------------------------------------------------------------------
# The suite and the lint gates
# ---------------------------------------------------------------------------

# PYTHONPATH because the modules are imported from a checkout rather than an
# installed package; PYTHONDONTWRITEBYTECODE because a cached .pyc from an
# identical mtime is how a changed module comes to be tested in its previous
# form.
test:  ## Run the suite
	PYTHONPATH=tests PYTHONDONTWRITEBYTECODE=1 \
		$(PYTHON) -m unittest discover -s tests -t tests --verbose

# One target, three tools, because a long line and an unused import are the
# same repair. Both configured files are committed, so this and the gate cannot
# disagree about the limits.
lint:  ## pyflakes, flake8 and yamllint over every tracked file
	$(PYTHON) -m pyflakes $$(git ls-files '*.py')
	$(PYTHON) -m flake8 $$(git ls-files '*.py')
	$(PYTHON) -m yamllint --strict $$(git ls-files '*.yaml' '*.yml')

# ---------------------------------------------------------------------------
# Terraform. `fmt` rewrites, `fmt-check` reports -- separate targets, because a
# gate that repairs the thing it is checking always passes.
# ---------------------------------------------------------------------------

fmt:  ## Rewrite Terraform files into canonical form
	$(TERRAFORM) fmt -recursive

fmt-check:  ## Report Terraform files that are not in canonical form
	$(TERRAFORM) fmt -check -recursive -diff

tf-init:  ## Initialise providers without a backend or credentials
	$(TERRAFORM) init -backend=false -input=false

tf-validate: tf-init  ## Validate the configuration
	$(TERRAFORM) validate -no-color

tf-lint:  ## Lint the configuration
	$(TFLINT) --init
	$(TFLINT) --format compact

# ---------------------------------------------------------------------------
# Everything the pipeline runs, in the order a failure is most useful in: the
# documents first, then the arithmetic, then the shape of the tree.
# ---------------------------------------------------------------------------

check: validate test generate audit lint fmt-check tf-validate tf-lint  ## Run every gate

# ---------------------------------------------------------------------------
# The deployment path. The only targets that need an account, and the only ones
# that ask anything of the caller.
# ---------------------------------------------------------------------------

# A real file target, not phony: the plan is the artifact `apply` consumes, and
# it is gitignored because a plan file carries the values it was produced from.
$(PLAN): $(wildcard *.tf) $(wildcard $(SPEC_DIR)/*.yaml) $(RULES)
	$(TERRAFORM) plan -input=false -out=$(PLAN)

plan: validate audit $(PLAN)  ## Produce a reviewable plan, documents checked first

# `apply` takes the SAVED plan and nothing else. `terraform apply` with no plan
# file re-plans and applies in one step, so what is applied is never the thing
# anybody read -- and a Makefile is exactly where that shortcut gets taken. The
# confirmation is a second, separate requirement: a saved plan can still be
# stale, and `make apply` is two characters from `make audit`.
apply:  ## Apply a plan produced by `make plan` (needs CONFIRM=yes)
	@test -f $(PLAN) || { \
		echo "no $(PLAN): run 'make plan', read it, then apply it"; exit 1; }
	@test "$${CONFIRM-}" = "yes" || { \
		echo "refusing to apply $(PLAN) without CONFIRM=yes"; exit 1; }
	$(TERRAFORM) apply -input=false $(PLAN)

clean:  ## Remove rendered artifacts and the saved plan
	rm -rf $(OUT) $(PLAN)
