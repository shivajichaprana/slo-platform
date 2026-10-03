variable "aws_region" {
  description = "AWS region the generated alerts and the error-budget policy gate are deployed into."
  type        = string
  default     = "us-east-1"

  validation {
    condition     = can(regex("^[a-z]{2}(-[a-z]+)+-[0-9]$", var.aws_region))
    error_message = "aws_region must be a region code such as \"us-east-1\" or \"ap-south-1\"."
  }
}

variable "environment" {
  description = "Deployment environment. Part of every derived resource name, so it is bounded; see the name-budget guard in main.tf."
  type        = string
  default     = "dev"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,11}$", var.environment))
    error_message = "environment must be 2-12 characters of lowercase letters, digits and hyphens, starting with a letter."
  }
}

variable "name_prefix" {
  description = "Prefix for every resource this configuration creates. Bounded because derived names have a 64-character ceiling."
  type        = string
  default     = "slo"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,15}$", var.name_prefix))
    error_message = "name_prefix must be 2-16 characters of lowercase letters, digits and hyphens, starting with a letter."
  }
}

variable "allowed_account_ids" {
  description = "Accounts this configuration may be applied to. Empty means unpinned; an explicit list is the cheapest guard against applying objectives to the wrong account."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for id in var.allowed_account_ids : can(regex("^[0-9]{12}$", id))])
    error_message = "Each entry in allowed_account_ids must be a 12-digit AWS account id."
  }
}

variable "spec_dir" {
  description = "Directory holding the objective specifications, relative to the repository root. Objectives live in the same review as the alerts generated from them, so a path outside the repository is refused."
  type        = string
  default     = "specs"

  validation {
    condition     = length(trimspace(var.spec_dir)) > 0
    error_message = "spec_dir must not be empty. Use \"specs\" for the directory shipped with this repository."
  }
}

variable "spec_file_pattern" {
  description = "Glob matched inside spec_dir to find objective specifications."
  type        = string
  default     = "*.yaml"

  validation {
    condition     = can(regex("^[A-Za-z0-9_*?.\\[\\]-]+$", var.spec_file_pattern))
    error_message = "spec_file_pattern must be a single-segment glob such as \"*.yaml\"; a pattern containing a path separator would reach outside spec_dir."
  }
}

variable "tags" {
  description = "Tags applied to every resource through the provider's default tags."
  type        = map(string)
  default     = {}

  validation {
    condition     = alltrue([for k in keys(var.tags) : !startswith(lower(k), "aws:")])
    error_message = "Tag keys may not start with \"aws:\" — that prefix is reserved by AWS and the apply is rejected."
  }
}
