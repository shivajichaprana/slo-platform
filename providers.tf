# `default_tags` is fed from a locals value assembled FROM VARIABLES ONLY.
#
# A provider configuration that reads a data source — a caller identity, a
# region lookup — makes Terraform report a dependency cycle against the
# provider itself, and that error names the provider rather than the line that
# caused it. Keeping the provider's inputs variable-only is what stops that,
# and it is why locals.tf is split into two blocks.
provider "aws" {
  region = var.aws_region

  # An empty list is not "no opinion" to the provider: it pins the apply to a
  # set of zero permitted accounts and refuses every operation. Normalise it.
  allowed_account_ids = length(var.allowed_account_ids) > 0 ? var.allowed_account_ids : null

  default_tags {
    tags = local.default_tags
  }
}
