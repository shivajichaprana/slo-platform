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
| `specs/` | Objective specifications. Read from inside the repository on purpose. |
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
