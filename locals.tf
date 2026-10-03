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

  # 64 characters is the narrowest AWS name limit this configuration reaches
  # (an IAM role name). The budget is spent in three places, so it is written
  # out rather than asserted: the prefix pair, an objective's own name, and
  # the suffix identifying which burn-rate tier an alert belongs to.
  derived_name_budget    = 64
  objective_name_reserve = 32
  alert_suffix_reserve   = 10
  derived_name_length    = length(local.resource_name_prefix) + 1 + local.objective_name_reserve + local.alert_suffix_reserve

  # `fileset` raises when its directory does not exist, and its message names
  # the function and a filesystem path rather than the input an operator set.
  # The `try` exists only to convert that into the guard in main.tf; null is
  # "the directory is not there", and an empty set is "the directory is there
  # and holds no specifications", which are different states.
  spec_dir_resolved = try(fileset(var.spec_dir, var.spec_file_pattern), null)
  spec_files        = sort(tolist(coalesce(local.spec_dir_resolved, toset([]))))
}
