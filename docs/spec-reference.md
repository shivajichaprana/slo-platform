# Specification reference

Every field of the two documents this repository reads: an objective specification
(`specs/*.yaml`) and an error-budget policy (`policy/rules.yaml`). The schema in
`schema/slo.schema.json` is authoritative for shape, vocabulary and per-field bounds; this
page is that schema in prose, with the reason each bound exists and the checks that bind
each field.

Reasoning about the model — why an indicator is a proportion, why there is no percentile
kind, what a burn-rate tier promises — is in [slo-model.md](slo-model.md). The finding
codes named here are documented in full in the tables in the [README](../README.md).

Validate any document before deploying anything derived from it:

```bash
python3 tools/validate-specs.py specs --strict             # every specification
python3 policy/budget.py specs --rules policy/rules.yaml --strict   # the policy
```

---

## Objective specification

### Document

| Field | Required | Type | Notes |
|---|---|---|---|
| `apiVersion` | yes | `slo.platform/v1` | Bumped only for a change that would alter the meaning of an existing document, so a pinned consumer never reinterprets one. |
| `kind` | yes | `ServiceLevelObjectives` | |
| `metadata` | yes | object | See below. |
| `objectives` | yes | array, at least 1 | A document with no objectives generates nothing and reports success. |

Unknown keys are refused everywhere in the document (`additionalProperties: false`
throughout, including under every `$ref`). A misspelled field is a field that silently does
nothing, which is the failure mode this setting exists to remove.

### `metadata`

| Field | Required | Type | Bounds | Notes |
|---|---|---|---|---|
| `service` | yes | string | `^[a-z][a-z0-9-]{1,47}$`, so 2–48 characters | Identity, not prose. Shares a 32-character budget with `name` — see the note below. |
| `owner` | yes | string | same grammar | A team, not a person. A rota outlives an individual. |
| `tier` | yes | `critical` \| `important` \| `best-effort` | | How much the service may cost in human attention. The validator uses it to judge whether paging on a given tier is proportionate (`W405`). |
| `description` | no | string | ≤ 500 characters | |
| `runbook` | no | string | | Where the responder goes when an alert generated from this document fires. |

**The identity budget.** `service` and `name` are bounded individually by the schema, but
only their sum matters: `service/name` becomes a resource name derived from the deployment
prefix, and the pair is capped at **32 characters** (`E202`) out of the 64-character
ceiling `locals.tf` imposes on a derived name. A 48-character `service` is
therefore legal on its own and never legal in practice. The same 32 is the gate unit budget
in `policy/budget.py` and the reserve in `locals.tf`; the suite asserts the three are one
number rather than three that happen to agree.

### `objectives[]`

| Field | Required | Type | Bounds | Notes |
|---|---|---|---|---|
| `name` | yes | string | `^[a-z][a-z0-9-]{1,31}$`, so 2–32 characters | Identity. Unique within the document (`E200`) and across documents (`E201`). |
| `title` | yes | string | 8–120 characters | The sentence the objective is. Carried onto alerts and into the report. |
| `description` | no | string | ≤ 1000 characters | |
| `sli` | yes | object | one of two kinds | See below. |
| `objective` | yes | number | ≥ 0.5, strictly < 1 | A fraction, not a percentage: `0.995`, never `99.5`. |
| `window` | yes | object | one of two kinds | See below. |
| `alerting` | yes | object | | See below. |

**Why `objective` is strictly below 1.** An objective of 1 is the claim that no budget
exists. It sets every burn-rate threshold to zero and every projection to infinity, so
every alert fires for ever and the policy can never clear. The 0.5 floor is not policy — it
is a misplaced-decimal guard, catching `0.95` written as `0.095`.

### `sli` — ratio kind

```yaml
sli:
  kind: ratio
  good_query: 'sum(rate(http_requests_total{service="checkout-api",code!~"5.."}[$window]))'
  valid_query: 'sum(rate(http_requests_total{service="checkout-api"}[$window]))'
  blind_spots:
    - "requests rejected by the load balancer never reach this counter"
  sampling:
    expected_events_per_hour: 600000
```

| Field | Required | Type | Notes |
|---|---|---|---|
| `kind` | yes | `ratio` | |
| `good_query` | yes | non-empty string | Events served acceptably. |
| `valid_query` | yes | non-empty string | Events that should have been. |
| `blind_spots` | yes | array of strings, ≥ 1 entry, each ≥ 3 characters | Required; see below. |
| `sampling` | no | object | `expected_events_per_hour` > 0, plus an optional `note` ≤ 300 characters. |

**`$window` is a placeholder and must stay one.** The range in a query is substituted per
tier. A query that pins its own range evaluates one window length at the budget window and
at both sides of every tier, which collapses a whole burn-rate ladder into one alert
repeated at three thresholds. A pinned range is refused rather than rewritten (`S300`),
because deciding which of several literal ranges is the indicator's own is a guess, and a
wrong guess deploys cleanly.

**Why `blind_spots` is required.** The usual way an availability indicator lies is a
denominator measured behind the thing that broke: a failure in front of the service removes
traffic rather than failing it, so the indicator reports health during a total outage. The
schema can force the question to be answered; it cannot check the answer. A short entry
asserting "none" is reported (`W407`) rather than accepted — the field exists so the claim
is visible in review, not so it can be dismissed.

**Why `sampling` matters.** It is how the validator can tell that an objective is finer
than its own signal. A threshold below `1 / events in the short window` is tripped by a
single failed event, which makes every tier above it decoration (`W402`); a budget smaller
than one event is reported outright (`W409`).

### `sli` — threshold kind

```yaml
sli:
  kind: threshold
  valid_query: 'sum(rate(refund_settlement_seconds_count{service="checkout-api"}[$window]))'
  metric: refund_settlement_seconds
  threshold: 0.3
  unit: seconds
  comparison: less_than
  blind_spots:
    - "a refund never enqueued is never observed here"
```

| Field | Required | Type | Notes |
|---|---|---|---|
| `kind` | yes | `threshold` | |
| `valid_query` | yes | non-empty string | Must read `<metric>_count` (`S309`) — see below. |
| `metric` | yes | non-empty string | The histogram the per-event comparison is made against. |
| `threshold` | yes | number > 0 | |
| `unit` | yes | `milliseconds` \| `seconds` \| `bytes` \| `count` | Stated so the figure is not read in the wrong one. |
| `comparison` | yes | `less_than` \| `less_than_or_equal` \| `greater_than` \| `greater_than_or_equal` | |
| `blind_spots`, `sampling` | as above | | |

**The numerator is produced by rewriting the denominator**, swapping the histogram's
observation count for a cumulative bucket, so population, label selection and
range-vector function are inherited by construction rather than rebuilt. That is why
`valid_query` must read `<metric>_count`: without it there is nothing to rewrite. A
numerator composed independently counts a different population from its denominator, and a
proportion that can exceed 1 does not look implausible enough to be noticed.

**A cumulative bucket cannot express a strict comparison**, so `less_than` compiles as
`less_than_or_equal` and says so. A bucket boundary is also a *label value*: `le="0.30"`
does not match a bucket published as `0.3`.

**CloudWatch refuses this kind outright** (`S100`). Metric math compares period aggregates
and not events, so the obvious translation counts the periods whose average was fast
instead of the requests that were fast — a different quantity that tracks the right one
closely enough never to be questioned.

### `window`

Rolling:

```yaml
window:
  kind: rolling
  duration: 28d
```

Calendar:

```yaml
window:
  kind: calendar
  period: month
  timezone: Europe/Amsterdam
```

| Field | Required | Type | Notes |
|---|---|---|---|
| `kind` | yes | `rolling` \| `calendar` | **No default.** Neither is safe; see [slo-model.md](slo-model.md). |
| `duration` | rolling only | `^[1-9][0-9]*(m\|h\|d)$` | Minutes, hours or days. Weeks and months are not expressible: a month is 28, 29, 30 or 31 days, so an objective measured per month is a calendar window. |
| `period` | calendar only | `week` \| `month` \| `quarter` | |
| `timezone` | calendar only | IANA zone, default `UTC` | A calendar budget resets at a wall-clock instant, so the zone is part of the objective. |

A calendar window's length is *nominal* for arithmetic that needs a figure before the
period is known — 30 days for a month, 91 for a quarter — and every derived number computed
from it is labelled nominal rather than reported as fact.

### `alerting`

```yaml
alerting:
  policy: multiwindow-burn-rate
  tiers:
    - name: fast
      burn_rate: 14.4
      long_window: 1h
      short_window: 5m
      notify: page
```

| Field | Required | Type | Notes |
|---|---|---|---|
| `policy` | yes | `multiwindow-burn-rate` | Alerts are generated from the *rate* the budget is being spent at, never from the indicator's current value. An indicator below its objective says the budget has already gone. |
| `tiers` | yes | array, 1–6 entries | At least one, because a policy with no tier reports nothing. |

### `alerting.tiers[]`

| Field | Required | Type | Bounds | Notes |
|---|---|---|---|---|
| `name` | yes | string | `^[a-z][a-z0-9-]{1,15}$`, so 2–16 characters | Transliterated for targets whose identifiers exclude the hyphen. |
| `burn_rate` | yes | number > 0 | | Multiple of the rate the objective permits. |
| `long_window` | yes | `^[1-9][0-9]*(m\|h\|d)$` | | The condition that fires the tier. |
| `short_window` | yes | same grammar | | The condition that clears it. |
| `notify` | yes | `page` \| `ticket` | | `page` wakes a human and `ticket` does not. Also the urgency the shadowing check orders tiers by. |

Checks that bind a tier, each refusing something every system downstream would accept:

| Code | Refuses |
|---|---|
| `E300` | A tier that cannot fire: `burn_rate x (1 - objective)` exceeds 1, so firing needs an error rate above 100%. The message names the highest reachable burn rate. |
| `E301` | A long window not shorter than the objective window — an alert that *is* the objective, reported once it has already been missed. |
| `E302` | A short window that cannot release the alert. |
| `W400` | A threshold above 50%: a near-total-outage detector rather than a fast burn. |
| `W402` | A threshold below the sampling floor, which a single failed event trips. |
| `W406` | Two tiers at the same burn rate. They differ only in window, so the shorter always fires first and the longer adds a second notification and nothing else. |
| `G306` | A tier that can never be the first notification *and* is no more urgent than the tier shadowing it. An escalation — a page behind a ticket — is the one case a shadowed tier is legitimate, so urgency is part of the test. |
| `W401` | A tier that fires once more than half the budget is spent: it reports the budget's end rather than its burn. |
| `W403` | No `sampling` block, so the detection floor cannot be computed and `W402` is not checked for any tier. |
| `W404`, `W410` | The slowest tier paging, and any tier paging on a calendar-window objective — a page can arrive for a budget about to be forgiven at the period boundary, or be suppressed by the reset while the service is still failing. |
| `W408` | A long-to-short window ratio that leaves the pair behaving as a single window. |

---

## Error-budget policy

One document, read by both `policy/budget.py` and the Terraform gate, so the evaluator and
the deployed resources cannot disagree about what the policy says.

### Document

| Field | Required | Notes |
|---|---|---|
| `apiVersion` | yes | `slo.platform/v1`. |
| `kind` | yes | `ErrorBudgetPolicy`. |
| `metadata` | yes | `name`, `owner`, optional `description`. The owner is who the policy belongs to, which is not necessarily who approves an exemption from it. |
| `defaults` | no | `max_budget_age`, `exemption_max_duration`. |
| `gates` | yes | One per deployable unit. |

`max_budget_age` is the staleness allowance: a decision made from a figure computed longer
ago than this is a decision about then, so beyond it the figure is treated as *unreadable*
and the outcome is handed to the gate's failure direction rather than to the last cached
value. `exemption_max_duration` applies to any exemption that does not set its own, so
adding one cannot accidentally create an undated exemption.

### `gates[]`

| Field | Required | Notes |
|---|---|---|
| `unit` | yes | `^[a-z][a-z0-9-]{1,31}$` — a path segment in the published parameter and a Terraform resource key, so the grammar is the intersection of both. Bounded by the same 32 characters as an objective identity. |
| `description` | no | |
| `objectives` | yes | References as `service/name`. A gate naming an objective no document defines is refused at plan time as well as by the evaluator (`P201`): at run time such a gate has no condition to evaluate, so its failure direction silently decides every deployment it covers. |
| `combine` | yes | `any` (the most restrictive action any objective reaches) or `all`. |
| `on_unreadable_budget` | yes | `closed` \| `open`. **No default, and refused at parse rather than warned about** — without it there is no defined behaviour to evaluate. |
| `rules` | yes | Evaluated in order of severity, not of appearance. |
| `exemptions` | no | Named classes that may ship during a freeze. |

### `gates[].rules[]`

| Field | Required | Notes |
|---|---|---|
| `name` | yes | Appears in the published decision, so it is the reason a pipeline gives for stopping. |
| `action` | yes | `notify` \| `review` \| `freeze`, in increasing strictness. |
| `when` | yes | At least one of `remaining_below` (a level) or `exhaustion_within` (a projection); either one holding triggers the rule. A rule with neither is refused at parse — it either never applies or always does, and the document does not say which. |
| `clear_above` | optional at parse | The exit, and always a level even when the entry is a projection (`P309`). A rule that blocks deployments and declares none is reported, as is one at or below its trigger, or one whose band is narrower than five of the budget's own quanta (`P302`). |
| `reason` | optional | Carried into the published decision, so a rule without one blocks without saying why. |

A level-only policy is reported (`P301`) with both counterexamples computed from its own
figures: a service at 30% remaining and no burn that freezing does not help, and one at 60%
burning fast that the rule never catches.

### `gates[].exemptions[]`

| Field | Required | Notes |
|---|---|---|
| `class` | yes | Claimed by `--exemption` at enforcement time. |
| `description` | no | |
| `approver` | yes | Recorded, not authenticated. The record is the point, and an approver equal to the policy's own owner is reported. |
| `max_duration` | no | Falls back to `defaults.exemption_max_duration`. Every exemption carries an expiry, because an exemption is granted during an incident and revoked by somebody remembering to revoke it. |

A `reliability-fix` class is not optional in practice: without it, a freeze blocks its own
remedy.

### Enforcing a gate

```bash
python3 policy/budget.py specs \
  --rules policy/rules.yaml \
  --observations observations.yaml \
  --previous current-decision.json \
  --enforce checkout-api
```

Exit `0` the gate permits the deployment, `1` the policy or the objectives are faulty,
`2` usage or input failure, `3` the gate refuses the deployment. `--previous` is what makes
the exit thresholds apply: hysteresis needs the state the gate is already in, and a
stateless evaluator cannot invent it — which is why the published decision is the next
evaluation's input.
