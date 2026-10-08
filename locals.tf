# Two locals blocks, split deliberately.
#
# Everything the provider configuration consumes must be derivable from
# variables alone (see providers.tf). Keeping those values in their own block
# makes the rule visible at the point where it would otherwise be broken by a
# one-line addition.
locals {
  default_tags = merge(
    {
      Environment = var.environment
      ManagedBy   = "terraform"
      Component   = "service-level-objectives"
    },
    var.tags,
  )
}

locals {
  resource_name_prefix = "${var.name_prefix}-${var.environment}"

  # 64 characters is a ceiling this configuration imposes on ITSELF, and
  # saying so is the correction: the previous justification called it the
  # narrowest AWS name limit reached here, an IAM role name, and no role is
  # created anywhere in this repository. The widest names actually deployed
  # are an IAM policy (`<prefix>-gate-writer`) and a Parameter Store path
  # (`/<prefix>/gate/<unit>`), each bounded well above 64. The ceiling is kept
  # for two reasons that do not depend on a service limit: every deployed name
  # stays legible in a console listing that truncates, and 64 is the limit a
  # per-objective IAM role would introduce, so a prefix accepted now is still
  # accepted if one is ever added.
  derived_name_budget = 64

  # An objective's own name — and the same 32 characters as a gate's unit name
  # in `policy/budget.py` and the unit grammar in `policy/rules.yaml`. The
  # repository's checks assert the three are one number rather than three that
  # happen to agree today.
  objective_name_reserve = 32

  # The two shapes a deployed name takes, written as the templates themselves
  # so the budget is spent against them rather than against a count of parts.
  #
  # A third reserve stood here until now: ten characters for the suffix
  # identifying which burn-rate tier an alert belongs to. Nothing this
  # configuration deploys carries such a suffix — a rendered alarm name is
  # bounded by CloudWatch's own 255 and a Prometheus alert name is a label
  # value — so the reserve protected nothing, while the longest suffix that
  # does exist, twelve characters, was not protected at all.
  gate_parameter_prefix = "/${local.resource_name_prefix}/gate/"
  gate_policy_suffixes  = ["-gate-reader", "-gate-writer"]

  gate_policy_suffix_reserve = max([for suffix in local.gate_policy_suffixes : length(suffix)]...)

  # The worst case of the two, not their sum: no single name carries both an
  # objective name and a policy suffix, which the previous formula assumed.
  #
  # The suffix shape cannot be the binding one at any legal input — the widest
  # prefix pair the validations permit is 29 characters, so it reaches 41 of
  # the 64 — and it is kept anyway, stated as unreachable here the way the
  # unreachable guards in the generator are: it is the other shape that
  # exists, it costs one comparison, and it starts binding on its own if a
  # suffix is ever added that is longer than 35 characters.
  derived_name_length = max(
    length(local.gate_parameter_prefix) + local.objective_name_reserve,
    length(local.resource_name_prefix) + local.gate_policy_suffix_reserve,
  )

  # `fileset` raises when its directory does not exist, and its message names
  # the function and a filesystem path rather than the input an operator set.
  # The `try` exists only to convert that into the guard in main.tf; null is
  # "the directory is not there", and an empty set is "the directory is there
  # and holds no specifications", which are different states.
  spec_dir_resolved = try(fileset(var.spec_dir, var.spec_file_pattern), null)
  spec_files        = sort(tolist(coalesce(local.spec_dir_resolved, toset([]))))
}
