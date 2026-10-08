# The model

Why this repository represents an objective the way it does, and what the arithmetic
derived from it actually claims. Everything here is a property of the model rather than of
the implementation; the field-by-field contract is in
[spec-reference.md](spec-reference.md), and the code is where the checks live.

The figures used as examples are the ones `generator/render.py --render report` produces
from `specs/example.yaml`.
They are quoted so the claims can be checked against a real document rather than taken on
trust, and they are the generator's output, not a recollection of it.

## 1. An indicator is a proportion, always

An indicator here is `good / valid`: how many of the events that should have been served
acceptably were. The two kinds the schema permits differ only in how `good` is obtained —
given directly as a query, or derived per event by comparing a measurement against a
threshold. Nothing else about the arithmetic changes, which is the reason for the
restriction: an error budget, a burn rate and every alert threshold are the same
expressions for every objective in the repository.

The consequence worth stating is the shape of a latency objective. It is not "p95 below
300 ms". It is **"99% of requests complete within 300 ms"** — a proportion of events, with
the threshold inside the indicator and the objective outside it.

## 2. There is deliberately no percentile kind

Two reasons, and the second is the harder one.

**A percentile is not a proportion, so no budget can be computed from it.** "p95 = 420 ms"
does not say how many requests were unacceptable; it says where one point in the
distribution sits. There is no numerator to divide, so there is nothing to spend.

**Percentiles do not compose.** The average of a window's p95 values is not that window's
p95, and no weighted combination of them is either. A percentile objective can therefore
only be evaluated over exactly the window it was measured in — which makes a multi-window
burn-rate policy, whose whole mechanism is evaluating one indicator over several window
lengths at once, unimplementable rather than merely approximate. A tool that offered the
kind anyway would produce thresholds that look right and mean nothing.

If the available signal is a histogram, this is not a limitation: the threshold kind reads
a cumulative bucket, which is a count of events, and the proportion follows.

## 3. The budget is a count, and the span is a translation of it

An objective of 99.5% over 28 days on a service seeing a million events a day permits
0.5% of them to fail. That is **403,200 events**, and the repository reports it that way
first. The familiar form — "3.4 hours" — is a *restatement* of the same number: the length
of a hypothetical total failure that would consume the whole budget at the stated event
rate. It is not downtime, and it is not a duration the service is allowed to be broken
for, unless the indicator happens to count time rather than events. Both figures appear in
the generated report, with the second labelled as what it is.

## 4. The window kind is required, and has no default

A rolling window never resets. An incident keeps consuming budget for a full window length
after it ends, so the budget recovers on a schedule set by *when* the spend happened and by
nothing anybody does afterwards.

A calendar window resets at a boundary. The same incident is nearly forgiven if it happens
on the last day of the period, and poisons the entire period if it happens on the first.

Neither is safe, and the difference only shows up in the situation that matters, so the
schema refuses to choose. A specification that omits the kind is rejected rather than
defaulted.

A calendar window also needs a zone, because its boundary is a wall-clock instant rather
than an offset — so `timezone` is part of the objective and not a display setting.

## 5. A burn rate, and the one identity worth memorising

A burn rate of `B` means the budget is being spent `B` times faster than the objective
permits. The error rate that corresponds to it is

```
threshold = B x (1 - objective)
```

so a 14.4x tier on a 99.5% objective fires at a 7.2% error rate, and the same tier on a
99% objective fires at 14.4%.

### Detection is a curve, not a figure

An incident at a constant error rate `r` pulls the trailing average up in proportion, so
the long window's average reaches the threshold after

```
detection = threshold x long_window / r
```

For the example's `fast` tier on the availability objective — 14.4x, one-hour long window —
that is **4 minutes** into a total outage, **9 minutes** into a 50% one, and **the whole
hour** for an incident sitting exactly on its threshold. Quoting a single "detects in 4
minutes" is quoting the best case.

### What the tier costs does not depend on the incident

Substituting that detection time back into the budget consumed gives

```
budget spent at detection = burn_rate x long_window / objective_window
```

and `r` has cancelled. The share of the budget already gone when a tier fires is
**identical** whether the service is wholly down or barely degraded: 2.1%, 5.4% and 10.7%
for the example's three tiers. That invariant, not the burn rate, is what choosing a tier
buys — and the two window fields in the specification obscure it completely, which is why
the generator computes and prints it.

### Every tier is blind to short incidents

Even at a 100% error rate the long average cannot reach the threshold before
`threshold x long_window` has elapsed. So an outage that *ends* before then never fires the
tier, however total it was: a 4-minute complete outage is invisible to the example's whole
availability policy. This is the same quantity as the total-outage detection time, read
from the other side, and the report says so rather than presenting it as a second
independent fact.

The short window does not help here. It is a second condition, not a faster one.

### Every policy is blind to a band of slow burns

The lowest threshold in a policy is the lowest sustained error rate anything reports. A
rate just below it spends the whole budget in `objective_window / lowest_burn_rate` with
nothing ever firing — **9.3 days** for the example's latency objective, which misses a
28-day objective with no alert having fired at any point. That is the failure a burn-rate
policy is usually assumed to rule out.

A tier at a burn rate of 1 closes the band, because its threshold is exactly the rate the
objective permits. The cost is that it fires while the service is meeting its objective, so
it is a budget-tracking signal and not a prediction of exhaustion — consistent with
notifying by ticket, and not with paging. The example's availability objective takes that
trade and its latency objective does not, which is why one is reported as closed and the
other carries a warning.

### A tier can be arithmetically unable to fire

Since `threshold = B x (1 - objective)` and an error rate cannot exceed 1, a tier is
unreachable unless

```
objective >= 1 - 1 / B
```

The textbook 14.4x tier therefore needs a 144% error rate against a 99.0%-to-90% objective
and is impossible for any objective below **93.06%**. Such a tier is accepted by every
system that will take it, reports healthy for ever, and never fires. It is refused here,
with the highest reachable burn rate named in the message.

## 6. The short window governs clearing, not firing

For an incident at a constant rate the short window is already above the threshold by the
moment the long one crosses it — which is proved rather than assumed, and is why firing is
governed by the long window alone. What the short window removes is the latch: after a
total outage stops, the example's `fast` tier would stay firing for 56 minutes on the long
condition alone, and clears in 5 with the short one.

The cost is specific, and it is the policy's structural weakness: **budget spent in bursts
shorter than the detection floor is not reported.** Several brief failures inside one long
window can satisfy the long condition between them at a moment when the short window is
quiet, so the long side is true, the short side is false, and nothing fires while the
budget goes. Repeated brief failures are better reported as a count than as a burn rate.

## 7. Sliding and tumbling are not the same window

Every figure above assumes a query engine evaluating a trailing range. A CloudWatch alarm
does not: it compares period-aligned data points, so a burn starting mid-period is split
across two periods and may breach neither — which is exactly the case a multi-window policy
exists to catch. Against that target the detection figures are optimistic bounds rather
than equalities, and the budget-at-detection figure is a lower bound. The correction is not
a constant factor, because it depends on where in the period the incident started, so it is
carried as a caveat in the generated alarm file and in the report rather than folded into
the numbers.

## 8. A budget policy is about a month, not a minute

An alert answers "is something burning now". A policy answers "may this change ship". The
second is where the usual advice is wrong in ways that still deploy.

**A threshold on the remaining budget is a lagging control.** Remaining budget is a level;
what decides whether freezing helps is the rate. A service at 30% remaining and no burn
will never exhaust its budget, and freezing it achieves nothing. One at 60% burning at 10x
exhausts in days and is not caught by a rule about 30%. So the projection

```
time to exhaustion = remaining x objective_window / burn_rate
```

is the primary condition, and the level is a floor beneath it. A policy whose every rule
reads the level alone is reported, with both counterexamples computed from that policy's
own figures.

**The projection's error has a stated direction, and that is what makes it usable.** On a
rolling window, spend ages out during the projection and is not subtracted, so the figure
under-estimates the time available and the control triggers early — conservative. On a
calendar window nothing ages out, so consumption is exact, but the window *ends* and the
projection does not know it. Those are different caveats, and the one that applies is the
one reported.

**A freeze is never ended by a fix.** Repairing a service does not return spent budget. On
a rolling window the spend leaves the window one window-length after it happened, so the
exit date is set by when the budget was spent; the freeze clears on its own and "we shipped
the fix, unblock us" is not an exit condition. On a calendar window nothing returns until
the boundary, so an incident early in the period can hold the gate shut for the rest of it.
The exits that exist are the window advancing, an enumerated exemption, or changing the
objective.

**One threshold flaps, and every flap is a pipeline that stops or starts.** Each rule
carries an exit as well as a trigger, and the exit must be above the trigger by more than a
few of the budget's own quanta — one event moves the remaining fraction by
`1 / budget_events`, so a band narrower than that is noise. Release is a **staircase**: the
latch is held per rule, so a frozen gate de-escalates to review and then to notify as the
remaining fraction rises past each rule's exit. A single exit for the whole gate would jump
from freeze to allow on one crossing, unblocking a pipeline at the moment the budget is
least able to absorb a bad release.

**A stale figure widens both thresholds.** At burn rate `B` the remaining fraction moves
`B x age / objective_window` during the staleness allowance. At 15 minutes and 14.4x on a
28-day window that is 0.54% against a 15% hysteresis band — 4% of it, which is reported as
a note. Above a quarter of the band the two thresholds are not distinguishable in practice,
however carefully they were chosen.

**The failure direction has no safe default.** Closed stops every deployment, including the
one that would repair whatever made the budget unreadable. Open disables the policy during
exactly the outage it exists for. So it is required per gate, with no default, and it is
refused at parse rather than warned about — without it there is no defined behaviour to
evaluate. Readability and the combination mode are independent axes: the unreadable
verdicts are a floor applied over the combination, so a deliberately lenient gate does not
become strict for a reason unrelated to its objectives.

## 9. What this model does not tell you

- **Whether the objective is the right one.** Nothing here derives a target from user
  experience, revenue, or a contract. It checks that the target is internally coherent and
  that the alerts follow from it.
- **Whether the indicator measures what you think.** The usual way an availability
  indicator lies is a denominator measured behind the thing that broke: a failure in front
  of the service removes traffic rather than failing it, so the indicator reports health
  during a total outage. The schema can force the question to be answered — `blind_spots`
  is required — but it cannot check the answer. A declared blind spot of "none" is
  reported as implausible rather than accepted.
- **What the budget was spent on.** Consumption is an input to the policy, not a
  conclusion from it. Attribution needs the incident record, which lives elsewhere.
- **Whether a freeze was the right call.** The policy makes the decision reviewable and
  states the direction of its own error. It does not claim the decision is correct.
