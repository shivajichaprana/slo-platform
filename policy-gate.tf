# The enforcement point for the error-budget policy.
#
# The decision itself is computed by `policy/budget.py` from the same document
# this file reads. What is deployed here is the place the decision is PUBLISHED
# and the two permissions that make publishing it meaningful. Three properties
# shape the whole file.
#
# **Terraform does not own the decision's value.** The state changes many times
# a day, from a job rather than from a plan, so a configuration that managed the
# value would revert it: an unrelated `terraform apply` would silently thaw a
# frozen gate, and nothing about that apply would hint at it. The parameter's
# existence, its name, its tier and its permissions are managed here; its value
# is adopted once and then ignored.
#
# **The thing being gated must not be able to open the gate.** Read and write
# are separate policies for that reason and not for tidiness. A deployment role
# holding the write permission turns the whole mechanism into documentation,
# because the step that is blocked can unblock itself and would, under the
# pressure that makes a freeze matter.
#
# **The published decision is also an INPUT.** Hysteresis needs to know which
# state the gate is in, and a stateless evaluator cannot invent it: the
# evaluating job reads this parameter, applies the exit thresholds against it,
# and writes the result back. That read is why the parameter is a plain String
# -- see the type argument below -- and the reason it is version-bearing rather
# than a file somewhere.
locals {
  policy_rules_path    = "${path.module}/${var.policy_rules_file}"
  policy_rules_present = fileexists(local.policy_rules_path)

  # Same shape as the spec-directory guard in locals.tf, and for the same
  # reason: `yamldecode` raises with a message about YAML rather than about the
  # input an operator set, so the `try` exists only to convert the failure into
  # a precondition that names `policy_rules_file`. null means "unparseable",
  # which is a different state from "absent" and gets a different message.
  policy_rules = local.policy_rules_present ? try(yamldecode(file(local.policy_rules_path)), null) : null

  policy_gates_declared = try(local.policy_rules.gates, [])
  policy_gate_units     = [for gate in local.policy_gates_declared : try(gate.unit, "")]

  # Keyed by unit, which is also the parameter's last path segment and the
  # resource key. A duplicate unit would collapse two gates into one entry
  # silently, so the count is compared against the list below rather than left
  # to `for_each` to resolve by overwriting.
  policy_gates = {
    for gate in local.policy_gates_declared : gate.unit => gate
    if try(gate.unit, "") != ""
  }

  # Objective identities the specifications actually define. Read here so that a
  # gate governing an objective that does not exist is refused at plan time: at
  # runtime that gate has no condition to evaluate, so its failure direction
  # decides every deployment for as long as the reference stays wrong, with the
  # policy document still reading as configured.
  spec_documents = {
    for spec in local.spec_files : spec => try(yamldecode(file("${var.spec_dir}/${spec}")), null)
  }
  spec_documents_unparseable = [for spec, doc in local.spec_documents : spec if doc == null]
  spec_objective_keys = flatten([
    for spec, doc in local.spec_documents : [
      for objective in try(doc.objectives, []) :
      "${try(doc.metadata.service, "")}/${try(objective.name, "")}"
    ] if doc != null
  ])
  policy_objective_refs = distinct(flatten([
    for unit, gate in local.policy_gates : try(gate.objectives, [])
  ]))
  policy_objectives_unresolved = [
    for ref in local.policy_objective_refs : ref if !contains(local.spec_objective_keys, ref)
  ]

  # Parameter Store's value ceiling by tier. Intelligent-Tiering begins as a
  # standard parameter and is promoted when a value or a feature requires the
  # advanced tier, so the ceiling it is bounded by is the advanced one -- at the
  # advanced tier's price, which is the part worth knowing before choosing it.
  gate_value_ceiling = {
    "Standard"            = 4096
    "Advanced"            = 8192
    "Intelligent-Tiering" = 8192
  }

  # The document the parameter is CREATED with, and never updated with again.
  # Its state is the gate's own failure direction applied to the only honest
  # reading of a parameter nothing has evaluated yet: the budget has not been
  # read. A gate that fails closed therefore starts closed, which means a fresh
  # deployment blocks until the first evaluation runs rather than permitting
  # everything until then.
  policy_gate_initial_state = {
    for unit, gate in local.policy_gates : unit => (
      try(gate.on_unreadable_budget, "closed") == "closed" ? local.policy_gate_strongest[unit] : "allow"
    )
  }

  policy_gate_initial = {
    for unit, gate in local.policy_gates : unit => jsonencode({
      unit               = unit
      state              = local.policy_gate_initial_state[unit]
      reason             = "no budget has been evaluated since this gate was created"
      sequence           = 0
      fail_direction     = try(gate.on_unreadable_budget, "")
      hysteresis_applied = false
      permits_deployment = !contains(["review", "freeze"], local.policy_gate_initial_state[unit])
      objectives         = [for ref in try(gate.objectives, []) : { objective = ref, readable = false }]
    })
  }

  # The strongest action a gate's own rules declare. An unreadable budget is not
  # promoted to a freeze on a gate whose rules only ever notify, because that
  # would make the failure path stricter than anything the policy says. The
  # evaluator computes the same value from the same document; the repository's
  # checks assert the two agree.
  policy_gate_strongest = {
    for unit, gate in local.policy_gates : unit => try([
      for action in ["freeze", "review", "notify"] : action
      if contains([for rule in try(gate.rules, []) : try(rule.action, "")], action)
    ][0], "allow")
  }

  policy_gate_parameter_names = {
    for unit, gate in local.policy_gates : unit => "${local.gate_parameter_prefix}${unit}"
  }
}

# Plan-time refusals for the policy document.
#
# `input` carries the values being guarded for the reason given in main.tf: a
# precondition is only evaluated when Terraform plans an action for its
# resource, so a guard fed a constant stops checking after the first apply.
#
# What is deliberately NOT checked here: the policy's arithmetic. Hysteresis
# bands, projection horizons, exemption expiry and the ladder of actions are
# checked by `policy/budget.py`, which can say why a band is too narrow in terms
# of the budget's event count. A precondition reporting that in HCL would be a
# second implementation of the same arithmetic, and the two would disagree.
resource "terraform_data" "policy_gate_guards" {
  input = {
    rules_file  = var.policy_rules_file
    present     = local.policy_rules_present
    parsed      = local.policy_rules != null
    units       = local.policy_gate_units
    unresolved  = local.policy_objectives_unresolved
    unparseable = local.spec_documents_unparseable
    value_sizes = { for unit, doc in local.policy_gate_initial : unit => length(doc) }
  }

  lifecycle {
    # Absent and unparseable are separated because the fixes are different: one
    # is a path, the other is a syntax error inside a file that is already there.
    precondition {
      condition     = local.policy_rules_present
      error_message = "policy_rules_file (\"${var.policy_rules_file}\") does not exist. It is resolved relative to this module, so set it to a path inside the repository such as \"policy/rules.yaml\"."
    }

    precondition {
      condition     = !local.policy_rules_present || local.policy_rules != null
      error_message = "policy_rules_file (\"${var.policy_rules_file}\") exists and is not parseable as YAML. Run `python3 -m policy.budget specs --rules ${var.policy_rules_file}`, which reports the fault with its position."
    }

    precondition {
      condition     = try(local.policy_rules.kind, "") == "ErrorBudgetPolicy"
      error_message = "policy_rules_file (\"${var.policy_rules_file}\") declares kind \"${try(local.policy_rules.kind, "")}\", not \"ErrorBudgetPolicy\". An objective specification and a budget policy are different documents: pointing this input at a specification would create gates that govern nothing."
    }

    # A unit becomes a path segment in the parameter name and a resource key in
    # this configuration, and the grammar below is the intersection of what both
    # accept. The same expression is in `policy/budget.py`; the repository's
    # checks assert they match, because a name this configuration accepts and the
    # evaluator rejects is a gate that is deployed and never written to.
    precondition {
      condition = alltrue([
        for unit in local.policy_gate_units : can(regex("^[a-z][a-z0-9-]{1,31}$", unit))
      ])
      error_message = "Every gate's unit must be 2-32 characters of lowercase letters, digits and hyphens, starting with a letter. Offending: ${join(", ", [for unit in local.policy_gate_units : format("%q", unit) if !can(regex("^[a-z][a-z0-9-]{1,31}$", unit))])}."
    }

    # `for_each` would resolve a repeated key by keeping one gate and dropping
    # the other, with no error and no indication which survived.
    precondition {
      condition     = length(local.policy_gate_units) == length(distinct(local.policy_gate_units))
      error_message = "Two or more gates declare the same unit, so they would publish to one parameter and one resource key: the objectives and exemptions of all but one are silently not in force. Units declared: ${join(", ", local.policy_gate_units)}."
    }

    # Checked before the reference check below, because an unparseable
    # specification removes its objectives from the resolvable set and would
    # otherwise be reported as a batch of wrong references.
    precondition {
      condition     = length(local.spec_documents_unparseable) == 0
      error_message = "These objective specifications are not parseable as YAML, so the objectives they define cannot be resolved: ${join(", ", local.spec_documents_unparseable)}. Fix them before the gate references are checked, or every reference into them is reported as unresolved."
    }

    precondition {
      condition     = length(local.policy_objectives_unresolved) == 0
      error_message = "These gate objectives are not defined by any specification under spec_dir (\"${var.spec_dir}\"): ${join(", ", local.policy_objectives_unresolved)}. A gate governing an objective that does not exist has no condition to evaluate, so its failure direction decides every deployment it covers for as long as the reference is wrong — while the policy document still reads as configured."
    }

    # The decision document grows with the number of governed objectives and
    # with the length of the reason text the evaluator writes. Checked against
    # the tier's ceiling here because a value too large is rejected by the API at
    # write time, which is the moment a freeze would have been published.
    precondition {
      condition = alltrue([
        for unit, doc in local.policy_gate_initial :
        length(doc) <= local.gate_value_ceiling[var.gate_parameter_tier]
      ])
      error_message = "A gate's initial decision document exceeds the ${local.gate_value_ceiling[var.gate_parameter_tier]}-character value limit of the ${var.gate_parameter_tier} parameter tier. The published decisions grow with the reason text the evaluator writes, so a document already near the limit will be rejected at the moment a freeze is published."
    }
  }
}

# One parameter per gate: the decision a deployment pipeline reads, and the
# previous state the next evaluation applies its exit thresholds against.
resource "aws_ssm_parameter" "gate" {
  for_each = local.policy_gates

  name = local.policy_gate_parameter_names[each.key]

  # String, not SecureString, and the choice is the opposite of the cautious
  # one. A budget decision is not a secret — it is a fact about a service's
  # reliability that every deployment role has to read — and encrypting it adds
  # a KMS key policy to the gate's read path. That is a new way for the gate to
  # become unreadable, and unreadable is the case the failure direction exists
  # to cover: a key policy mistake would read, downstream, as a budget nobody
  # could measure.
  type        = "String"
  data_type   = "text"
  tier        = var.gate_parameter_tier
  description = "Error-budget policy decision for the ${each.key} deployable unit. Written by the budget evaluator, read by deployment pipelines; the value is not managed by Terraform."

  value = local.policy_gate_initial[each.key]

  tags = {
    Name          = "${local.resource_name_prefix}-gate-${each.key}"
    PolicyGate    = each.key
    FailDirection = try(each.value.on_unreadable_budget, "unset")
  }

  lifecycle {
    # The whole point of the resource. Without this, any apply reverts the
    # current decision to the initial one — thawing a frozen gate as a
    # side-effect of an unrelated change, with nothing in the plan output that
    # looks like a reliability decision.
    #
    # The consequence to accept: Terraform no longer reports drift on the
    # value, so a gate that stopped being written to looks healthy here. That is
    # detected on the read side instead, by the age of the figure in the
    # document, which is what `max_budget_age` in the policy is for.
    ignore_changes = [value]

    # A destroyed parameter is not a neutral state: the reader gets
    # ParameterNotFound, which has to be treated as an unreadable budget and
    # resolved through the failure direction — so on a fail-open gate, deleting
    # this parameter is the same thing as switching the policy off.
    #
    # The cost is named rather than discovered: removing a gate from the policy
    # document will FAIL the apply until the gate is removed from state
    # deliberately, and `terraform destroy` will not run while any gate exists.
    # That friction is the point. Retiring a budget gate is a decision about
    # what the organisation enforces, not a line deleted from a YAML file on the
    # way to something else.
    prevent_destroy = true
  }
}

# Read-only access, for the deployment pipelines the gate applies to.
data "aws_iam_policy_document" "gate_reader" {
  statement {
    sid    = "ReadBudgetGateDecisions"
    effect = "Allow"
    actions = [
      "ssm:GetParameter",
      "ssm:GetParameters",
      "ssm:GetParametersByPath",
    ]
    resources = concat(
      [for unit, name in local.policy_gate_parameter_names :
        "arn:${data.aws_partition.current.partition}:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${name}"
      ],
      # GetParametersByPath is authorised against the PATH rather than against
      # the leaves below it, so the prefix is listed explicitly. Without it the
      # call fails with an access error that names none of the parameters above
      # and reads like a missing gate.
      length(local.policy_gates) > 0 ? [
        "arn:${data.aws_partition.current.partition}:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${trimsuffix(local.gate_parameter_prefix, "/")}"
      ] : [],
    )
  }
}

# Write access, for the evaluator and for nothing else.
#
# Separate from the reader for the one reason that matters: a principal holding
# both is a principal that can decide it is not frozen. The permission is
# scoped to these parameters by ARN rather than to a path wildcard, so adding a
# gate is a reviewed change to this configuration instead of an existing grant
# quietly widening.
data "aws_iam_policy_document" "gate_writer" {
  statement {
    sid    = "PublishBudgetGateDecisions"
    effect = "Allow"
    actions = [
      "ssm:PutParameter",
      "ssm:GetParameter",
      "ssm:GetParameterHistory",
    ]
    resources = [
      for unit, name in local.policy_gate_parameter_names :
      "arn:${data.aws_partition.current.partition}:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${name}"
    ]
  }

  # A deleted gate is not a neutral state: the reader gets ParameterNotFound,
  # which resolves through the failure direction, so on a fail-open gate
  # deleting the parameter is indistinguishable from permitting everything. A
  # label is worse, because a reader that follows one is reading a version
  # somebody chose rather than the current decision. Both are denied.
  #
  # The deny is scoped to the gate path rather than to `*`: this policy may be
  # attached to a role that has unrelated Parameter Store work, and a global
  # deny on deletion would silently break it, which is the kind of breakage that
  # gets the whole policy detached.
  #
  # Residual, stated because it is not covered: the tier a parameter is written
  # at is an argument of PutParameter and has no IAM action or condition key of
  # its own, so a writer can promote a gate to a tier with a different value
  # limit. Terraform reconciles the tier on the next apply, which makes that
  # detection rather than prevention.
  statement {
    sid     = "DenyGateDeletionAndLabelling"
    effect  = "Deny"
    actions = ["ssm:DeleteParameter", "ssm:DeleteParameters", "ssm:LabelParameterVersion"]
    resources = [
      "arn:${data.aws_partition.current.partition}:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/${local.resource_name_prefix}/gate/*"
    ]
  }
}

resource "aws_iam_policy" "gate_reader" {
  count = length(local.policy_gates) > 0 ? 1 : 0

  name        = "${local.resource_name_prefix}-gate-reader"
  description = "Read error-budget policy decisions. Attach to every deployment role the policy applies to."
  policy      = data.aws_iam_policy_document.gate_reader.json
}

resource "aws_iam_policy" "gate_writer" {
  count = length(local.policy_gates) > 0 ? 1 : 0

  name        = "${local.resource_name_prefix}-gate-writer"
  description = "Publish error-budget policy decisions. Attach to the budget evaluator only — never to a role that deploys."
  policy      = data.aws_iam_policy_document.gate_writer.json
}
