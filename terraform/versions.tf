# serving-economics-bench EC2 runner rig.
# Deliberately boring: one root module, local state (see .gitignore),
# no remote backend - this is a weekend rig, not a platform.

terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }
}
