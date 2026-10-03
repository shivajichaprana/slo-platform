# slo-platform

Service-level objectives as code: specifications, generated burn-rate alerts, and an
error-budget policy that can refuse a release.

An objective that lives in a dashboard description or a wiki page is a sentence. An
objective that lives here is a file with a schema, a budget computed from it, alerts
derived from that budget, and a policy that acts on what the alerts report. The point of
keeping all four in one repository is that none of them can drift from the others
silently: the alert thresholds are not typed in beside the objective, they are produced
from it.

## What this produces

| Artifact | Derived from | Why it is generated rather than written |
|---|---|---|
| Error budget | Objective and window | The arithmetic is simple and is nearly always wrong by hand, because a budget is a duration or an event count, never a percentage. |
| Burn-rate alerts | Budget and alert policy | Multi-window alert thresholds are a function of the objective. Written by hand they are copied between services whose objectives differ. |
| Policy decisions | Budget consumption | "Stop shipping when the budget is gone" is only enforceable if the budget is a number something can read. |

## Layout

| Path | Contents |
|---|---|
| `versions.tf` | Terraform and provider version constraints, with the reason for each bound. |
| `providers.tf` | Provider configuration. Its inputs are variable-only; the reason is in the file. |
| `main.tf` | Account and partition identity, and the plan-time refusals. |
| `variables.tf` | Every input, each with a validation. |
| `locals.tf` | Derived values, split into two blocks for the reason given in the file. |
| `outputs.tf` | Including the outputs that report what is **not** the case. |
| `schema/slo.schema.json` | The specification's shape, vocabulary and per-field bounds. |
| `specs/` | Objective specifications. Read from inside the repository on purpose. |
| `specs/example.yaml` | A worked specification; every field is one a check depends on. |
| `tools/validate-specs.py` | Structural validation plus the arithmetic a schema cannot express. |
| `terraform.tfvars.example` | Placeholder values; copy to `terraform.tfvars`, which is ignored. |
| `.tflint.hcl` | Lint configuration, including the conventions this repository enforces on itself. |

## Configuration

| Input | Default | Notes |
|---|---|---|
| `aws_region` | `us-east-1` | Region the generated alerts and the policy gate are deployed into. |
| `environment` | `dev` | Part of every derived name, so it is length-bounded. |
| `name_prefix` | `slo` | Also part of every derived name. Short on purpose. |
| `allowed_account_ids` | `[]` | Empty is unpinned. An explicit list is the cheapest wrong-account guard. |
| `spec_dir` | `specs` | Must be inside the repository; an absolute or `..` path is refused. |
| `spec_file_pattern` | `*.yaml` | Single-segment glob, so it cannot reach outside `spec_dir`. |
| `tags` | `{}` | Merged into the provider's default tags. Keys may not start with `aws:`. |

## Getting started

```bash
cp terraform.tfvars.example terraform.tfvars   # then edit it
terraform init
terraform validate
terraform plan
```

A fresh checkout plans cleanly and creates nothing, because `specs/` holds no objectives
yet. That state is reported by the `slo_specs_absent` output rather than left to be
inferred from an empty plan — see below.

## What this repository checks about itself

Three input mistakes are refused before anything is created, and one correct-but-empty
state is reported rather than refused:

- **A spec directory that does not exist** is a misconfiguration and is refused, with a
  message naming the input rather than the filesystem call that failed.
- **A spec directory outside the repository** is refused. Objectives read from elsewhere
  are not reviewed in the same change as the alerts generated from them, which is the one
  property this repository exists to provide.
- **A name prefix that leaves too little room** is refused. Every deployed name is derived
  from the prefix, the environment and an objective's own name against a 64-character
  ceiling, so an overflow is invisible to whoever set those three and would appear only
  when a resource is created.
- **A spec directory that exists and is empty** is valid: it is what a fresh checkout
  looks like. It is reported through `slo_specs_absent`, because a configuration that
  deploys no alerts at all otherwise looks exactly like one whose objectives are all met.

The input validations cannot replace the name-budget guard: their widest legal values
exceed the ceiling together, which is precisely the case a per-input check cannot see.

## The specification model

An objective is a file. [`specs/example.yaml`](specs/example.yaml) is a worked one and
[`schema/slo.schema.json`](schema/slo.schema.json) is what it has to satisfy.

Six decisions shape it, and each of them exists because the alternative produces a
specification that looks fine and cannot be used:

**An indicator is always a proportion** — good events over valid events. Two kinds are
accepted and they differ only in how `good` is obtained: `ratio` takes both counts
directly, `threshold` derives `good` per event from a measured value and a bound. That
keeps the budget arithmetic identical for every objective in the repository.

**There is no percentile kind.** A latency objective is written as "99% of requests
complete within 300 ms", never as "p95 under 300 ms". A percentile is not a proportion,
so no error budget can be computed from one — and percentiles do not compose, so the
average of a window's p95 values is not that window's p95 and a percentile objective
cannot be evaluated over any window except the one it was measured in.

**The window kind is required and has no default.** A rolling window never resets, so an
incident keeps consuming budget for the whole window after it ends. A calendar window
resets at its boundary, so the same incident is forgiven the next morning if it lands on
the last day of the period and poisons the whole period if it lands on the first. Neither
is the safe choice, so neither is assumed.

**An objective is strictly below 1.** An objective of 1 is not a target, it is the claim
that no error budget exists, which sets every burn-rate threshold to zero and every burn
rate to infinity. A floor of 0.5 is there as a misplaced-decimal guard, not as policy.

**`blind_spots` is required.** The usual way an availability indicator lies is a
denominator measured behind the thing that broke: count requests arriving at the service
and a failure in front of it removes traffic instead of failing it, so the indicator
reports health — or no data — during a total outage. The schema can force the question to
be answered; it cannot check the answer, and the validator says so when the answer is
"none".

**`name` is identity, not prose.** Generated alert names and stored budget observations
are keyed on it, so renaming an objective deletes one and restarts the other. `title`
carries the readable sentence and is free to change.

## Validating a specification

```bash
python3 tools/validate-specs.py specs              # report
python3 tools/validate-specs.py specs --strict     # warnings fail too
python3 tools/validate-specs.py specs --json       # findings plus computed budgets
```

Structure is checked against the schema; everything that relates two fields to each other
is checked afterwards, because that is the part a schema cannot see. A document that fails
the schema is not put through the arithmetic, which would otherwise report faults in
fields the author never wrote.

Alongside the findings, the validator prints what it worked out: the budget as a duration
and as an event count, and per burn-rate tier the error rate it fires at, the share of the
budget already spent by then, and the finest error rate the signal can express.

| Code | Severity | What it means |
|---|---|---|
| `E100` | error | The document does not match the schema. |
| `E200` | error | Two objectives in one document share a name. |
| `E201` | error | Two documents declare the same `service/name` identity, so generated names would collide. |
| `E202` | error | `service` and `name` together exceed the 32-character identity budget; derived resource names would overflow their 64-character ceiling at creation time. |
| `E300` | error | A burn-rate tier is unreachable: firing would need an error rate above 100%. The textbook 14.4x tier is impossible for any objective below about 93%. |
| `E301` | error | An alert's long window is not shorter than the objective window, so the alert is the objective, reported once it is already missed. |
| `E302` | error | A short window is not shorter than its long window, so it cannot release the alert when the burn stops. |
| `E303` | error | Duplicate tier name within one objective. |
| `W400` | warning | A tier needs more than half of all events to fail, so it detects a near-total outage rather than a fast burn. |
| `W401` | warning | More than half the budget is spent by the time the tier fires. |
| `W402` | warning | The tier's threshold is below the finest error rate its short window can express, so a single failed event trips it and every tier above it fires on the same failure. |
| `W403` | warning | No `sampling` block, so the detection floor cannot be computed at all. |
| `W404` | warning | The slowest tier pages. |
| `W405` | warning | A `best-effort` service pages. |
| `W406` | warning | Two tiers share a burn rate, so the one with the longer window can never fire first. |
| `W407` | warning | `blind_spots` asserts there are none. |
| `W408` | warning | A short window is far from the conventional twelfth of its long window. |
| `W409` | warning | The entire budget is less than one event, so a single failure breaches the objective. |
| `W410` | warning | A calendar-window objective pages, so a page can arrive for a budget about to be forgiven. |

Budget figures for a calendar window are **nominal**: a month is 28, 29, 30 or 31 days and
the arithmetic needs one number, so 30 is used and every figure derived from it is labelled
nominal rather than presented as fact.

## Conventions

- Inputs are validated where they are declared; cross-field rules that a single input
  cannot express are plan-time preconditions, not documentation.
- Preconditions are given a changing `input` rather than a static one, because a
  precondition is only evaluated when Terraform plans an action for its resource — a
  static guard quietly stops checking after the first apply.
- Outputs report absences as well as values. An empty result is a conclusion, not a
  silence.
- Version constraints are floors with closed upper bounds, and each bound carries the
  reason it exists.

## Versioning

Tags are the stable interface. `main` is where changes land; a release tag is what a
consumer should pin.

## License

MIT — see [LICENSE](LICENSE).
