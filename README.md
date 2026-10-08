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
| Indicator queries | Indicator definition and a window | A query written once and reused at every window answers the same thing every time, which quietly turns a multi-window alert policy into a single alert repeated. |
| Policy decisions | Budget consumption | "Stop shipping when the budget is gone" is only enforceable if the budget is a number something can read. |

## Documentation

| Page | What it answers |
|---|---|
| [`docs/slo-model.md`](docs/slo-model.md) | Why an objective is represented this way, and what the derived arithmetic actually claims — the budget, the burn-rate identities, what a tier cannot see, and where the budget policy's usual advice is wrong. |
| [`docs/spec-reference.md`](docs/spec-reference.md) | Every field of both documents: type, bounds, the reason each bound exists, and the checks that bind it. |
| This file | What the repository produces, how to run it, and the full finding-code tables. |

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
| `sources/base.py` | The model an indicator source works from, and the vocabulary it reports in. |
| `sources/cloudwatch.py` | CloudWatch metric math and Metrics Insights, with the limits of each. |
| `sources/prometheus.py` | PromQL against the Prometheus query API, including histogram-derived indicators. |
| `generator/burn_rate.py` | What a burn-rate tier buys: detection, cost, and what it cannot see. |
| `generator/render.py` | Rendering a policy into alerting rules, alarms and a budget report. |
| `templates/` | Layout and prose for the targets with no serialiser; values are escaped, never trusted. |
| `policy/budget.py` | The error-budget policy: what a budget state permits, and what the control gets wrong. |
| `policy/rules.yaml` | A worked policy. Read by the evaluator and by the Terraform gate, so they cannot disagree. |
| `policy-gate.tf` | Where a decision is published, and the two permissions that make publishing it mean something. |
| `terraform.tfvars.example` | Placeholder values; copy to `terraform.tfvars`, which is ignored. |
| `.tflint.hcl` | Lint configuration, including the conventions this repository enforces on itself. |
| `tests/` | The suite, including the checks the repository makes about its own consistency. |
| `requirements-dev.txt` | What the checks need, as floors with closed upper bounds. |
| `.github/workflows/ci.yml` | The gates, split by what a failure would tell you. |
| `.flake8`, `.yamllint.yaml` | Lint limits, committed so a local run and the gate agree. |
| `docs/slo-model.md` | The model and its arithmetic, with the results the specification format obscures. |
| `docs/spec-reference.md` | The field-by-field contract of both documents. |

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
| `policy_rules_file` | `policy/rules.yaml` | Must be inside the repository. Read by Terraform and by the evaluator. |
| `gate_parameter_tier` | `Standard` | Caps a published decision at 4 KB. The advanced tier raises it, and costs. |

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

## Adopting this for a service

The order matters more than any individual step. Each stage produces something checkable
before the next one can take a decision away from anybody, which is the only reason a
control like this survives contact with a team.

**1. Write one objective, not five.** Copy `specs/example.yaml`, keep a single objective,
and point it at a signal that already exists. The first objective's job is to be argued
with; a file of five is a file nobody reviews. `tier` and `blind_spots` are the two fields
worth the most discussion, and both are required for that reason.

**2. Make it validate.** `python3 tools/validate-specs.py specs --strict` reads the schema
first and the arithmetic second.
Warnings are fatal here on purpose — every one of them describes an alert policy that
deploys and reports the wrong thing. Expect `W403` until the `sampling` block is filled in;
that block is what lets the validator work out whether the objective is finer than its own
signal.

**3. Read what the alerts would promise before deploying any.**
`python3 generator/render.py specs --render report` prints, per tier, when it fires at several error rates, how much of the budget is gone by then, the
shortest incident it cannot see at all, and the band of slow burns the policy misses
entirely. If those figures are not acceptable, the tier list is wrong — not the threshold,
which is derived. `docs/slo-model.md` explains each column.

**4. Deploy the alerts in the quietest form the target allows.**
`python3 generator/render.py specs --render prometheus --out generated/rules` renders them —
`--render cloudwatch` for the other target. Route them to a ticket queue first regardless of what `notify`
says, and leave them there for at least one full objective window. A burn-rate policy's
first month is data about the indicator, not about the service: a denominator measured in
the wrong place shows up as an alert that fires during a deploy, or one that stays silent
through an incident everybody saw.

**5. Reconcile the alerts against what actually happened.** For every incident in that
period, ask which tier fired and when. The two answers worth acting on are a tier that
fired after the incident was already being handled — its long window is too long for the
promise it is making — and an incident nothing fired for, which is either shorter than the
detection floor or inside the slow-burn band. Both are reported by the generator before the
fact; the point of this step is to confirm the report described reality.

**6. Only then write a policy, and start it at `notify`.** Copy `policy/rules.yaml`, keep
the gate's rules but set every `action` to `notify`, and run
`python3 policy/budget.py specs --rules policy/rules.yaml --strict`. A policy in this
shape blocks nothing and still publishes a decision, so the figures it would have acted on
can be read for a period before they stop a release. `on_unreadable_budget` has to be
answered even here, because it is what the gate does when the budget cannot be measured at
all.

**7. Attach the gate, writer first.** Apply the configuration, attach the writer policy to
the budget evaluator, and let it publish for a while with nothing reading the parameter.
Then attach the reader policy to the deployment roles and have the pipeline call
`budget.py --enforce <unit>`. Until the reader policy is attached every gate is unreadable,
so each pipeline resolves through its own failure direction — which is the behaviour to
confirm deliberately rather than discover.

**8. Promote `review` and `freeze` last, and say so out loud.** A freeze is not ended by
shipping a fix; it ends when the window advances, when an enumerated exemption is claimed,
or when the objective changes. Make sure the team that owns the service knows that before
the first freeze rather than during it, and make sure a `reliability-fix` exemption exists —
without one, a freeze blocks its own remedy.

What to avoid, in each case because it produces something that looks right and is not:

- **Do not hand-edit anything under `generated/`.** It is rebuilt from the specification and
  is gitignored. An edited threshold is a threshold that no longer follows from the
  objective, which is the single failure this repository exists to prevent.
- **Do not raise an objective to silence an alert.** A weaker objective is a larger budget
  and a higher threshold; the alert goes quiet because the commitment was reduced, and
  nothing records that this is what happened. Change the tier, or change the service.
- **Do not add a tier at the same burn rate as an existing one** to get a second
  notification. It differs only in window, so the shorter always fires first (`W406`).
- **Do not set every rule on a level.** Remaining budget is a level and exhaustion is a
  rate; a level-only policy both freezes services that were never going to exhaust their
  budget and misses the ones that are about to (`P301`).

## What this repository checks about itself

Three input mistakes are refused before anything is created, and one correct-but-empty
state is reported rather than refused:

- **A spec directory that does not exist** is a misconfiguration and is refused, with a
  message naming the input rather than the filesystem call that failed.
- **A spec directory outside the repository** is refused. Objectives read from elsewhere
  are not reviewed in the same change as the alerts generated from them, which is the one
  property this repository exists to provide.
- **A name prefix that leaves too little room** is refused. Every deployed name is derived
  from the prefix, the environment and either an objective's own name or a fixed policy
  suffix, against a 64-character ceiling this configuration imposes on itself and justifies
  in `locals.tf`. An overflow is invisible to whoever set the first two and would appear
  only when a resource is created.
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

## Reading an indicator

A specification says what proportion of events must be good. It does not say how
to obtain that proportion, because the answer is a different sentence in every
metric system. A source adapter is that translation, and there are two:
`cloudwatch` and `prometheus`.

**An adapter compiles; it never queries.** Nothing under `sources/` opens a
socket, signs a request or reads a credential. Each adapter turns one objective
into the request payloads a caller may send -- a whole-window figure for a
report, and the two spans of each burn-rate tier -- so the arithmetic worth
reviewing sits in a file rather than in a log, and can be checked without an
account.

**The window is supplied per evaluation, never written into the query.** An
indicator query marks its range as `$window`:

```yaml
good_query: sum(rate(http_requests_total{service="checkout-api",code!~"5.."}[$window]))
```

The same query is evaluated once for the objective window, once for each tier's
long window and once for each tier's short window. A query that pins its own
range returns the same figure for all of them, so every threshold above the
fastest tier fires on one condition and the multi-window policy is decoration --
which is the thing this repository exists to prevent. A pinned range is
therefore refused rather than rewritten: choosing which of several literal
ranges to replace would be a guess.

**The two sources are not interchangeable**, and a document is written for one of
them whether or not it says so. An adapter handed a foreign dialect refuses,
because the queries are opaque strings to both APIs: the alternative is a
configuration that applies cleanly, creates every alert, and measures nothing.

### CloudWatch

Two dialects are accepted, and they are not variants of one another.

*Metric math over metric statistics* reads stored metrics at a period, so it
reaches the full retention of the data and an alarm may evaluate as much as
seven days of it. Indicators are written as compact metric references:

```yaml
good_query: AWS/ApplicationELB/HTTPCode_Target_2XX_Count:Sum[LoadBalancer=app/checkout/50dc6c495c0c9188]
valid_query: AWS/ApplicationELB/RequestCount:Sum[LoadBalancer=app/checkout/50dc6c495c0c9188]
```

*Metrics Insights* reads a `SELECT` statement, which can select across metrics
without naming each one. It reaches two weeks of history for a chart and **the
most recent three hours for an alarm condition**, so any tier slower than three
hours cannot be an alarm on a Metrics Insights query at all.

Four properties of CloudWatch shape what is generated, and each of them is a
configuration the service accepts and reports on:

- **The alarm cannot be given the aggregate the report quotes.** Summing good
  events over the window, summing valid events and dividing is the natural way
  to write the figure, and metric math will do it -- a sum over one time series
  returns a scalar. The published guidance is not to put a scalar-returning
  function in an alarm, because an evaluating alarm retrieves more data points
  than its evaluation periods ask for and such a function does not answer the
  same way when it is given them. So the window aggregate is compiled for the
  report, the alarm is compiled as per-period arithmetic, and a scalar aggregate
  reaching an alarm expression is refused.
- **A total outage looks like missing data.** A period in which every request
  failed leaves the success metric with no data point, so the ratio is absent
  rather than zero -- and an alarm holds its previous state on missing data
  unless told otherwise. Every alarm expression therefore fills its numerator
  with zero, and because a fill cannot invent a series that was never reported
  at all, every alarm also treats missing data as breaching.
- **A long window is a tumbling period, not a sliding one.** An alarm compares
  period-aligned data points, so a one-hour window is each clock hour and not
  the trailing hour. A burn starting mid-period is split across two periods and
  can breach neither.
- **A period has to suit the window's age.** One-minute data is kept for 15
  days, five-minute for 63, one-hour for 455. A four-week budget asked for at
  one-minute resolution is answered for the recent fortnight and silently
  unanswered before it.

A threshold indicator is **refused** for CloudWatch rather than approximated.
Metric math compares period aggregates, not events, so a comparison against the
threshold counts the periods whose aggregate was good instead of the events that
were good -- a different quantity that tracks the right one closely enough never
to be questioned. A percentile statistic is not an answer either, for the reason
the specification has no percentile kind.

### Managed Prometheus

An objective compiles to query parameters for the Prometheus HTTP API: an
instant query for the window figure, and one per side of each tier.

- **An absent series is not a zero.** A selector matching nothing returns no
  sample, so a renamed label empties the whole expression and an alerting rule
  over it fires on nothing. Division makes it worse: zero over zero is NaN,
  every comparison against NaN is false, and no traffic becomes
  indistinguishable from healthy traffic. Numerators are therefore defaulted to
  zero and denominators deliberately are not, because an absent denominator
  means nothing was *measured* rather than nothing *failed*.
- **Each objective also gets a staleness expression.** No burn-rate tier can
  report a vanished denominator -- every one of them evaluates to nothing and
  nothing does not fire -- so one expression per objective asks the question
  separately, over the shortest window any tier uses.
- **A rate needs at least two samples in its range.** A window shorter than
  twice the sample interval yields nothing rather than zero, so a tier can be
  unable to fire for a reason unrelated to the service. The interval is an input
  to the adapter because it is a property of the deployment, not of the
  objective.
- **A threshold indicator's numerator is derived from its denominator** by
  swapping the histogram's observation count for one of its cumulative buckets.
  That is the only construction that guarantees both halves count the same
  population and use the same range-vector function: a numerator written
  independently inherits none of the denominator's label selection, and a rate
  divided by an increase is out by the length of the window, which makes a tier
  fire permanently.
- **A bucket boundary is a label value, not a number.** `le="0.30"` does not
  match a bucket published as `0.3`, and a boundary that was never configured
  matches nothing -- numerator empty, ratio absent, objective reported as met. A
  cumulative bucket also cannot express a strict comparison, so `less_than` is
  compiled as `less_than_or_equal` and said to be.

### Exercising a source

```bash
python3 -m sources.prometheus specs            # report
python3 -m sources.prometheus specs --json     # payloads and findings
python3 -m sources.cloudwatch specs            # refuses this example, by design
```

The shipped example is written for Prometheus, so the CloudWatch adapter refuses
it. That is the dialect guard working, not a failure.

| Code | What it means |
|---|---|
| `S100` | The indicator is not written for this source, so nothing was compiled. |
| `S101` | A metric reference, or a metric name, could not be read. |
| `S102` | The query identifies more than one series where one is required. |
| `S200` | The period or resolution does not reach as far back as the window asks. |
| `S201` | The window exceeds the points one range query may return. |
| `S202` | The window is beyond the reach of this query engine entirely. |
| `S203` | The window holds too few samples for a rate to be computed. |
| `S204` | A calendar window's length is nominal, so every figure from it is too. |
| `S205` | A threshold was converted into the unit the metric is published in. |
| `S206` | The window figure is correct and expensive; a recorded ratio is cheaper. |
| `S207` | Counts over a range are extrapolated, so a budget in events is an estimate. |
| `S300` | A query pins its own range, so every window would read the same figure. |
| `S301` | A construct belongs in the report rather than in an alarm. |
| `S302` | Missing data would read as success, and how the expression handles it. |
| `S303` | The statistic does not count events, so the ratio is not a proportion. |
| `S304` | A comparison the backend can only approximate, and by how much. |
| `S305` | The indicator kind cannot be expressed on this source at all. |
| `S306` | The numerator depends on a bucket boundary that may not exist. |
| `S307` | Good means above the boundary, which a cumulative bucket reaches by subtraction. |
| `S308` | Only the staleness expression distinguishes a measured success from no measurement. |
| `S309` | Numerator and denominator would count different populations. |
| `S400` | An alarm's evaluation span is past what the service permits. |
| `S401` | An alarm on this dialect cannot see the span its threshold was computed for. |
| `S402` | A service cap the query can exceed while still succeeding. |

Findings are errors, warnings or notes. An error means nothing was compiled for
that objective: there is no partial output, because a half-translated indicator
deploys as readily as a whole one.

## Generating alerts

```bash
python3 -m generator.render specs --render report                     # the arithmetic, as prose
python3 -m generator.render specs --render prometheus --out generated # alerting rules
python3 -m generator.render specs --render cloudwatch --out generated # alarms, as Terraform
python3 -m generator.render specs --render report --json              # the computed plan
```

An objective says what proportion of events must be good and at what multiples of the
budget's spend rate somebody should be told. It does not say what those choices buy, and
that is what is computed here.

### What a tier actually promises

A tier fires when the error rate averaged over its long window reaches
`burn_rate x (1 - objective)`. An incident at a constant error rate `r` pushes that
trailing average up in proportion, so the condition is met after:

```
detection = threshold x long_window / r
```

There is therefore no single detection time for a tier — it is a curve, fast for a total
outage and asymptotically the whole long window for an incident sitting on the threshold.
Substituting that back into the budget consumed gives the figure that does **not** depend
on the incident:

```
budget spent at detection = burn_rate x long_window / objective_window
```

`r` cancels. The share of the budget already gone when a tier fires is the same whether the
service is wholly down or barely degraded, and that figure — not the burn rate — is what
choosing a tier costs. It is the one worth arguing about, and the two window fields in the
specification obscure it completely.

Two consequences follow from the same arithmetic and are reported per tier:

- **Every tier is blind to short incidents.** The long average cannot reach the threshold
  before `threshold x long_window` has elapsed, even at a 100% error rate, so an outage
  that ends sooner is invisible to that tier however complete it was. The smallest such
  figure across the tiers is the fastest the whole policy can ever be.
- **Every policy is blind to a band of slow burns.** The lowest threshold in the policy is
  the lowest sustained error rate anything will report. A rate just under it spends the
  budget in `objective_window / lowest_burn_rate` with nothing firing at any point — unless
  the slowest burn rate is 1, which closes the band at the cost of a tier that fires while
  the service is meeting its objective.

The short window's contribution is also arithmetic rather than intuition. For a constant-rate
incident it is already above the threshold by the time the long window crosses it, so it does
not affect firing; what it removes is the latch, cutting the time an alert stays up after the
burn stops from most of the long window to most of the short one. What it costs is reported
as `G310`: budget spent in bursts shorter than the detection floor can satisfy the long
condition between them at a moment when the short window is quiet, and the tier stays silent.

### What is rendered

| Target | Output | Shape |
|---|---|---|
| `prometheus` | `<service>-<objective>.rules.yaml` | One rule per tier, two conditions joined by `and`, plus a staleness rule. |
| `cloudwatch` | `<service>-<objective>.tf` | Two alarms and one composite per tier; a Terraform variable for the topics. |
| `report` | `<service>-<objective>.md` | The arithmetic above, per tier, with every figure derived. |

A tier is one Prometheus rule and three CloudWatch resources, and that is not a stylistic
difference. A rule's expression can join two windows; an alarm evaluates a single period, so
the long and the short window have to be two alarms with a composite above them requiring
both. The children's actions are disabled so one tier sends one notification, and the
composite's rule interpolates the children's names from the resources — written as literal
strings, Terraform could not see the dependency and would be free to create the composite
first.

Rule files are **serialised**, not templated: their payload is PromQL, full of braces, quoted
label values and comparison operators, and interpolating one into a text template is how a
rule file becomes invalid or — worse — valid and different. The templates in `templates/`
carry only what a serialiser cannot emit. Terraform has no serialiser here, so the escaping
is explicit instead: every interpolated value passes through one function that escapes the
quote, the backslash and the `${` and `%{` that open a Terraform interpolation. Templates use
`@{...}` placeholders for the same reason — `$` belongs to HCL in those files.

One caveat is carried into the generated artifacts rather than left here. The arithmetic above
describes a query engine evaluating a trailing range. A CloudWatch alarm compares
period-aligned data points, so it cannot fire before the end of the period the breach fell in:
against that target every detection figure is an optimistic bound and the budget-at-detection
figure is a lower bound. `G411` says so in the alarm file and in the report.

| Code | Severity | What it means |
|---|---|---|
| `G100` | error | The objective declares no tier, so there is nothing to generate. |
| `G200` | error | Two tiers share a name, so they render to one alert and one is lost. |
| `G300` | note | The detection curve, and the budget figure that does not depend on it. |
| `G301` | error | The tier fires above a 100% error rate, so it is deployable and permanently silent. |
| `G302` | note | The shortest total outage the tier can see at all. |
| `G303` | note | What the short window changes: the clearing time, not the firing time. |
| `G304` | note | A burn rate at or below 1 fires while the objective is being met. |
| `G305` | warning | The short window holds too few events for its threshold to mean anything. |
| `G306` | warning | A tier that can never be the first notification about anything. |
| `G307` | note/warning | The band of sustained error rates no tier reports, and what it costs. |
| `G308` | warning | The fastest paging tier cannot see an outage short enough to matter to a human. |
| `G309` | note | The budget in each unit it gets quoted in, and which of them is a translation. |
| `G310` | note | Budget spent in brief bursts satisfies the long window and not the short one. |
| `G400` | error | A rendered identity exceeds the target's name ceiling. |
| `G401` | error | A rendered identity is produced twice, so one alert silently replaces another. |
| `G402` | note | The rule's `for` duration is one evaluation interval, and why it is not the short window. |
| `G403` | note | The two sides of the `and` must agree on labels or the rule never fires. |
| `G404` | error | The source compiled only one of the two windows, so there is no multi-window condition. |
| `G405` | warning | No staleness expression, so an unmeasured objective reads as a met one. |
| `G406` | note | Why a tier is three CloudWatch resources, and why the children's actions are off. |
| `G407` | note | How values are escaped for HCL, and which interpolations are deliberate. |
| `G408` | error | The alarm description exceeds what the API accepts. |
| `G409` | error | The composite alarm's rule exceeds what the API accepts. |
| `G410` | warning | Nothing here can confirm the notification topics reach anybody. |
| `G411` | warning | The target evaluates tumbling periods, so the computed detection figures are bounds. |

Artifacts are written under `generated/`, which is ignored: the specification is the reviewed
document and anything rendered from it is rebuilt rather than read as a diff.

## Enforcing an error budget

```bash
python3 -m policy.budget specs --rules policy/rules.yaml --strict        # audit the policy
python3 -m policy.budget specs --observations budget.yaml                # decide
python3 -m policy.budget specs --observations budget.yaml --json         # the decision documents
python3 -m policy.budget specs --observations budget.yaml \
        --previous current.json --gate checkout-api                      # with exit thresholds
python3 -m policy.budget specs --observations budget.yaml \
        --enforce checkout-api                                           # exit 3 if blocked
```

Exit status is `0` clean, `1` findings, `2` input that could not be read, and `3` the gate named
by `--enforce` blocks this deployment. `3` is distinct on purpose: a gate returning the same
status when it blocks and when it breaks is a gate that fails open the first time somebody
appends `|| true`.

Consumption is an **input**. Nothing here queries a metric backend, for the same reason the
source adapters do not: it keeps the arithmetic reviewable without an account, and it keeps a
credential out of the one component that is allowed to stop a release.

A gate names the objectives it governs, how they `combine`, what an unreadable budget means
(`on_unreadable_budget`), how old a figure may be (`max_budget_age`), and a list of rules. A rule
triggers on `remaining_below`, on `exhaustion_within`, or on either, takes one of three `action`s
— `notify`, `review`, `freeze` — and names the level it releases at in `clear_above`. Exemptions
are classes of change a freeze does not block, each with an `approver` and a `max_duration`.
[`policy/rules.yaml`](policy/rules.yaml) is a worked one.

### What the control actually does

**A threshold on the remaining budget is a lagging control.** Remaining budget is a level; what
decides whether stopping deployments helps is the rate. A service at 30% remaining with no burn
will never exhaust its budget and freezing it achieves nothing; a service at 60% burning at 10x
exhausts in days and is not caught by a rule about 30%. So the projection is the primary
condition and the level is a floor beneath it:

```
time to exhaustion = remaining_fraction x objective_window / burn_rate
```

**The projection's error has a stated direction.** On a rolling window, spend ages out as the
window advances and the expression does not subtract it, so the figure under-estimates the time
available and the control triggers earlier than strictly necessary. On a calendar window nothing
ages out, so the consumption is exact — but the window *ends*, and the projection does not know
that, which is why a horizon reaching past the reset is reported (`P306`).

**A freeze is never ended by a fix.** On a rolling window the spend that caused it leaves the
window `window_length` after it happened, so the exit date is set by *when* the budget was spent
and nothing done afterwards moves it. On a calendar window nothing returns until the period
boundary. Either way "we have shipped the fix, please unblock us" is not an exit condition, and
the exits that do exist are the window advancing, an enumerated exemption, or changing the
objective.

**A gate with one threshold flaps, and every flap is a pipeline state change.** Each rule
therefore carries `clear_above` as well as its trigger, and the band between them is checked
against the budget's quantum — one event moves the remaining fraction by `1 / budget_events`, and
a band narrower than a handful of those is crossed by ordinary variation rather than by the
service. Release is a staircase rather than a switch: as the remaining fraction rises it passes
each blocking rule's exit in turn, so a frozen gate de-escalates to `review` before it clears.

**A stale figure widens both thresholds.** A decision made from a figure computed `age` ago is a
decision about `age` ago; at burn rate `B` the remaining fraction moves `B x age / window` in
that time. When that approaches the hysteresis band the two thresholds are indistinguishable in
practice, however carefully they were chosen (`P303`).

**The direction a gate fails in has no safe default.** Closed stops every deployment including
the fix for whatever made the budget unreadable; open disables the policy during exactly the
outage it exists for. `on_unreadable_budget` is required per gate, like the window kind in a
specification, and the published decision records the readability of every objective rather than
only the state — so nothing downstream has to infer which of the two cases it is looking at.

### The gate

A decision is published to one Parameter Store parameter per deployable unit, and that parameter
is read as well as written: hysteresis needs the state the gate is in, and a stateless evaluator
cannot invent it. Three consequences are visible in `policy-gate.tf`.

- **Terraform does not own the value.** The state changes many times a day, from a job rather
  than from a plan, so `ignore_changes` covers it. Without that, an unrelated `terraform apply`
  would silently thaw a frozen gate, and nothing in the plan output would look like a
  reliability decision. The cost is accepted: drift on the value is no longer reported here, and
  a gate that stopped being written to is detected on the read side instead, by the age of the
  figure in the document.
- **Read and write are separate policies.** A deployment role holding the write permission can
  decide it is not frozen, and would, under the pressure that makes a freeze matter. The
  policies are created and attached to nothing, so until an operator attaches them the gate is
  unreadable and every pipeline resolves through its failure direction.
- **The decision is a plain `String`.** Not the cautious choice, and deliberately so: a budget
  decision is not a secret, and encrypting it puts a key policy on the gate's read path — a new
  way for the gate to become unreadable, which is the one case the failure direction exists to
  cover.

Four things are refused at plan time, and they are the ones Terraform depends on rather than the
policy's arithmetic: a missing or unparseable policy document, a document that is actually an
objective specification, two gates sharing a unit, and a gate governing an objective no
specification defines. The last is the sharpest — at runtime such a gate has no condition to
evaluate, so its failure direction decides every deployment it covers for as long as the
reference is wrong, with the document still reading as configured. The arithmetic is left to
`policy/budget.py`, because a second implementation of it in HCL is two implementations that
will disagree.

One caveat about `--strict` in a pipeline: `P307` is a statement about where *now* falls in a
calendar period, so a policy governing a calendar-window objective can change its warning count
with the date. Pass `--now` when the result has to be reproducible.

| Code | Severity | What it means |
|---|---|---|
| `P100` | error | The policy declares no gate, so it enforces nothing. |
| `P101` | error | A gate declares no rule, so no budget state changes it. |
| `P102` | error | A gate governs no objective, so it permits every deployment. |
| `P200` | error | Two gates share a unit, so one silently replaces the other. |
| `P201` | error | A gate governs an objective nothing defines, so its failure direction decides permanently. |
| `P202` | warning | An objective no gate acts on: a measurement rather than a commitment. |
| `P203` | error | A unit name exceeds the configuration's name budget. |
| `P204` | note/warning | How several objectives on one gate combine, and what that hides. |
| `P300` | note | The exhaustion projection, and the direction of its error. |
| `P301` | warning | Every rule reads the level alone, which is a lagging control. |
| `P302` | error/warning | No exit, an exit below the entry, or a band narrower than the measurement's quantum. |
| `P303` | note/warning | What the staleness allowance does to the hysteresis band. |
| `P304` | note | What ends a freeze, which is the window and not a fix. |
| `P305` | warning | A gentler rule that can never be the gate's state. |
| `P306` | warning | A projection horizon reaching past the window it is computed from. |
| `P307` | note/warning | How much budget a calendar window can still lose before it resets. |
| `P308` | note | The smallest change in remaining budget one event can make. |
| `P309` | note | Why a projection trigger has a level-shaped exit. |
| `P400` | note | The gate's failure direction, and what it costs in this gate's terms. |
| `P401` | warning | The gate's strongest action blocks nothing, so its failure direction is inert. |
| `P402` | error | An exemption with no expiry: the usual way a gate ends up permanently off. |
| `P403` | warning | An exemption that outlives any freeze it is granted against. |
| `P404` | note | The approver is the team the gate stops, so the exemption records a decision rather than checking one. |
| `P405` | error | A duplicated exemption class, where document order decides the approver. |
| `P406` | warning | A gate that can freeze and exempts nothing, so a freeze blocks its own remedy. |

## Testing

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m unittest discover -s tests -t tests
```

The suite is stdlib `unittest` and imports the modules the way the tools themselves do,
from a checkout rather than from an installed package. It needs no running backend and
no credentials: every adapter here compiles a payload and never sends one, so the
arithmetic is checkable offline, which is the property the whole repository is arranged
around.

| Module | What it holds the code to |
|---|---|
| `tests/test_spec_model.py` | Parsing a specification into the model: durations, both window kinds, identity, and the difference between an absent sampling rate and a zero one. |
| `tests/test_schema.py` | The schema's deliberate refusals — no percentile indicator, no default window kind, no objective of 1 — and the bounds it shares with the model. |
| `tests/test_validate_specs.py` | Every finding the validator can emit, fired by a document that earns it, plus the exit ladder. |
| `tests/test_burn_rate.py` | The arithmetic, as closed-form identities rather than recorded outputs. |
| `tests/test_render.py` | Escaping, identity transliteration, and the structure of each rendered artifact. |
| `tests/test_policy.py` | The projection, the calendar boundaries, the action ladder, and what each `combine` mode means. |

Three of those are assertions about the repository rather than about a function, and they
are the ones most likely to catch a future edit:

- **Finding codes are checked in both directions.** Every code the validator can emit is
  exercised by a test, every code a test asserts on is one the validator can emit, and
  every emitted code appears in this README. The set of exercised codes is read from the
  test module's own source rather than accumulated as the tests run — `unittest` orders
  classes alphabetically, so a set filled in at run time is a claim about test ordering.
- **Figures that exist twice are asserted to be one figure.** The identity budget is
  checked across the schema pattern, the model and the validator; the duration parsers in
  the model and the validator are compared across a range of inputs; the reachability
  boundary the generator re-derives is checked against the one the validator reports; and
  every `notify` value the schema permits is checked against the urgency table the
  shadowing comparison indexes with.
- **The arithmetic is tested as identities.** That the share of budget spent at detection
  does not depend on the error rate is asserted by computing it at four rates, not by
  pinning the number the code happens to produce — so an edit that changes what the
  figure means fails even when the shape of the output is unchanged.

### Continuous integration

`.github/workflows/ci.yml` runs six gates on every push and pull request, split by what a
failure would tell you rather than by which tool produces it — a specification that no
longer validates, arithmetic that changed, a policy that refers to objectives nothing
defines, and Terraform that no longer parses are four different repairs, and one job
running all four reports only the first.

| Gate | What it fails on |
|---|---|
| `validate specifications` | Any finding from `tools/validate-specs.py` under `--strict`, and a `--json` report that is not parseable on its own. |
| `unit tests` | The suite, on the oldest and the newest Python the modules claim to support. |
| `render artifacts` | A plan that produces no artifact, a rule file that does not parse, or a rendered expression still carrying a placeholder. |
| `audit error-budget policy` | Any error or warning from the policy audit against the specifications in the same checkout. |
| `lint` | `pyflakes`, `flake8` and `yamllint`, each reading a committed configuration so a local run and the gate cannot disagree. |
| `terraform` | `fmt -check`, `validate` against a backend-less init, and `tflint`. |

Three deliberate choices in that file:

- **Actions are pinned to a commit, never to a tag.** A tag is a reference the publisher
  can repoint, so a pinned tag is a dependency that changes without a commit here. The
  readable version is in the comment beside each SHA.
- **The render gate does not run under `--strict`.** The generator's warnings describe
  what a burn-rate policy cannot see — the band of slow burns below its lowest threshold,
  chiefly — which is a property of the objective its author chose, not a fault introduced
  by the change under test.
- **`ci complete` is the single required check, and it runs under `if: always()`.** A
  branch protection rule naming every job leaves the next job added here ungated until
  somebody remembers to add it there as well; and without `always()` a cancelled or
  skipped dependency leaves the gate job *skipped*, which several interfaces present as a
  pass.

Nothing in the pipeline needs credentials: every adapter compiles payloads rather than
sending them, and `terraform init -backend=false` needs the providers but not an account.

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
- A control states the direction of its own error. A projection whose bias is unknown cannot be
  the reason a release is stopped.
- A translation that cannot preserve something says so and stops. Nothing is quietly
  substituted, approximated or partially emitted, because every one of those produces
  a configuration that deploys cleanly and reports the wrong thing.

## Versioning

Tags are the stable interface. `main` is where changes land; a release tag is what a
consumer should pin.

## License

MIT — see [LICENSE](LICENSE).
