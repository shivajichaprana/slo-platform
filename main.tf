# Identity of the account and partition this configuration is pointed at.
#
# There is deliberately no `aws_region` data source here: both its `name` and
# its `id` attributes are deprecated in provider 6.x, which is inside the
# version range above, so the region is read from `var.aws_region` directly.
data "aws_caller_identity" "current" {}

data "aws_partition" "current" {}

# Plan-time refusals.
#
# `input` is fed the values being guarded rather than left static. A
# precondition is only evaluated when Terraform plans an action for the
# resource it is attached to, so a guard whose input never changes stops being
# checked after the first apply and reports nothing for the rest of the
# deployment's life.
resource "terraform_data" "scaffold_guards" {
  input = {
    spec_dir            = var.spec_dir
    spec_dir_resolved   = local.spec_dir_resolved
    derived_name_length = local.derived_name_length
  }

  lifecycle {
    # A spec directory that does not exist is a misconfiguration; a spec
    # directory that exists and is empty is the correct state of a fresh
    # checkout. Only the first is refused, and the second is reported through
    # the `slo_specs_absent` output, because refusing it would make an empty
    # repository unplannable.
    #
    # `local.spec_dir_resolved` is produced by a `try()` around `fileset()`
    # purely to turn the function's own message — which names `fileset` and a
    # filesystem path — into one that names the input the operator sets. If a
    # future Terraform release stops swallowing that error, the plan still
    # fails, so this degrades to a worse message rather than to no check.
    precondition {
      condition     = local.spec_dir_resolved != null
      error_message = "spec_dir (\"${var.spec_dir}\") does not resolve to a directory inside this configuration. Set it to a path relative to the repository root, such as \"specs\"."
    }

    # Specs read from outside the repository are not reviewed alongside the
    # alerts they generate, which is the single property this repository
    # exists to provide. An absolute path or a `..` segment is refused for
    # that reason and not for a filesystem one.
    precondition {
      condition     = !startswith(var.spec_dir, "/") && !strcontains(var.spec_dir, "..")
      error_message = "spec_dir must be a path inside the repository: an absolute path or one containing \"..\" puts the objectives outside the review that covers the alerts generated from them."
    }

    # Every deployed name is DERIVED from the prefix, the environment and
    # either an objective's own name or a fixed policy suffix, against the
    # 64-character ceiling locals.tf imposes and justifies there. An overflow
    # is invisible to whoever set the first two and surfaces only when the
    # resource is created. The input validations below cannot guarantee the
    # budget on their own: their widest legal values exceed it together.
    precondition {
      condition     = local.derived_name_length <= local.derived_name_budget
      error_message = "name_prefix (\"${var.name_prefix}\") and environment (\"${var.environment}\") give a worst-case derived name of ${local.derived_name_length} characters against a ceiling of ${local.derived_name_budget} (${local.objective_name_reserve} reserved for an objective or gate unit name, ${local.gate_policy_suffix_reserve} for the longest policy suffix; the wider of the two shapes applies). Shorten name_prefix by at least ${local.derived_name_length - local.derived_name_budget} characters."
    }
  }
}
