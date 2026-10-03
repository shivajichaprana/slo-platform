output "aws_account_id" {
  description = "Account this configuration is pointed at."
  value       = data.aws_caller_identity.current.account_id
}

output "aws_partition" {
  description = "Partition of the target account, used wherever an ARN is assembled."
  value       = data.aws_partition.current.partition
}

output "resource_name_prefix" {
  description = "Prefix every derived resource name starts with."
  value       = local.resource_name_prefix
}

output "objective_name_budget" {
  description = "Characters actually left for an objective's own name by this name_prefix and environment. The guard in main.tf refuses a pair that leaves fewer than the reserved 32."
  value       = local.derived_name_budget - length(local.resource_name_prefix) - 1 - local.alert_suffix_reserve
}

output "spec_dir" {
  description = "Directory searched for objective specifications."
  value       = var.spec_dir
}

output "spec_files" {
  description = "Objective specifications found, relative to spec_dir."
  value       = local.spec_files
}

# Reports a state that is valid, plannable and produces nothing. Without it, a
# configuration pointed at a directory holding no specifications applies
# cleanly, creates no alerts, and is indistinguishable from one whose
# objectives are all met.
output "slo_specs_absent" {
  description = "True when spec_dir exists but holds no file matching spec_file_pattern, so this configuration would deploy no alerts at all."
  value       = local.spec_dir_resolved != null && length(local.spec_files) == 0
}
