variable "region" {
  description = "AWS region for the runner, the S3 bucket, and the SSM key parameter. Keep in sync with REGION in the Makefile."
  type        = string
  default     = "us-east-1"
}

variable "instance_type" {
  description = "Runner instance type. The client is I/O-bound (streaming API calls); t3.micro is plenty."
  type        = string
  default     = "t3.micro"
}

variable "bucket_name" {
  description = "S3 bucket for code + results. Empty string means the default: serving-econ-bench-<account-id>."
  type        = string
  default     = ""
}
