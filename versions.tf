terraform {
  # 1.6.0 floor, chosen rather than inherited: `terraform_data` carries the
  # plan-time guards in this configuration and arrived in 1.4, and the native
  # test framework the validation pipeline runs arrived in 1.6.
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source = "hashicorp/aws"
      # A floor, not a pin: 5.60.0 is the oldest release this configuration is
      # written against. The upper bound is closed on purpose, because a major
      # provider release is permitted to remove arguments set here.
      version = ">= 5.60.0, < 7.0.0"
    }
  }
}
