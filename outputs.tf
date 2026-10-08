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
  description = "Characters left for an objective's own name, or a gate's unit name, by this name_prefix and environment. The guard in main.tf refuses a pair that leaves fewer than the reserved 32."
  value       = local.derived_name_budget - length(local.gate_parameter_prefix)
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

output "policy_gate_parameters" {
  description = "Parameter each gate publishes its decision to, keyed by deployable unit. A deployment pipeline reads its own key and nothing else."
  value       = local.policy_gate_parameter_names
}

output "policy_gate_initial_state" {
  description = "State each gate is created in, keyed by unit. It is the gate's failure direction applied to a budget nothing has evaluated yet, so a gate that fails closed blocks until the first evaluation runs rather than permitting everything until then."
  value       = local.policy_gate_initial_state
}

output "policy_gate_reader_policy_arn" {
  description = "IAM policy granting read access to the gate decisions. It is created and attached to nothing: until an operator attaches it to the deployment roles the policy covers, every gate is unreadable and those pipelines resolve through their failure direction."
  value       = try(aws_iam_policy.gate_reader[0].arn, null)
}

output "policy_gate_writer_policy_arn" {
  description = "IAM policy granting write access to the gate decisions. Attach to the budget evaluator only — a deployment role holding both policies can decide it is not frozen."
  value       = try(aws_iam_policy.gate_writer[0].arn, null)
}

# Reports a state that is valid, plannable and enforces nothing — the policy
# counterpart of `slo_specs_absent`. A repository with objectives and no gates
# computes every budget and lets every deployment through, which looks exactly
# like a repository whose budgets are all healthy.
output "policy_gates_absent" {
  description = "True when no error-budget gate is deployed, so no objective's budget can stop a deployment however far it is spent."
  value       = length(local.policy_gates) == 0
}

output "policy_objectives_uncovered" {
  description = "Objectives defined by the specifications that no gate governs. Their budgets are computed and alerted on, and exhausting them changes nothing about what ships."
  value       = sort(setsubtract(toset(local.spec_objective_keys), toset(local.policy_objective_refs)))
}
