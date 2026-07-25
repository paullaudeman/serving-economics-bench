# In-region EC2 runner for serving-economics-bench.
#
# Access model: zero SSH, zero key pairs, zero ingress. The security group
# has NO ingress rules; all access is SSM Session Manager / send-command over
# the instance's outbound 443.
#
# Secret model (CRITICAL): the Anthropic API key NEVER appears in Terraform
# variables, state, or user_data. It is written to an SSM SecureString
# parameter out-of-band (make key) and read into the runner's process
# environment at run time (run_remote.sh). This module only grants the READ
# permission on that parameter path.

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project = "serving-economics-bench"
    }
  }
}

data "aws_caller_identity" "current" {}

locals {
  bucket_name = var.bucket_name != "" ? var.bucket_name : "serving-econ-bench-${data.aws_caller_identity.current.account_id}"
}

# ---------------------------------------------------------------------------
# Network: default VPC, egress-only security group
# ---------------------------------------------------------------------------

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

resource "aws_security_group" "runner" {
  name_prefix = "serving-econ-bench-"
  description = "Egress-only runner SG - no ingress, access via SSM Session Manager"
  vpc_id      = data.aws_vpc.default.id

  # No ingress blocks on purpose: nothing dials in, ever.
  egress {
    description = "All outbound (Bedrock, Anthropic API, SSM, S3)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# ---------------------------------------------------------------------------
# S3: one bucket for code zip + results
# ---------------------------------------------------------------------------

resource "aws_s3_bucket" "results" {
  bucket        = local.bucket_name
  force_destroy = true # weekend rig: destroy must not strand on leftover objects
}

resource "aws_s3_bucket_public_access_block" "results" {
  bucket = aws_s3_bucket.results.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ---------------------------------------------------------------------------
# IAM: least-privilege instance role
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "runner" {
  name_prefix        = "serving-econ-bench-"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

data "aws_iam_policy_document" "runner" {
  # Bedrock invoke surface only. Region is wildcarded because the us.* geo
  # cross-region inference profiles route to every destination region in the
  # geography (see RUN.md troubleshooting) - a single-region grant breaks them.
  statement {
    sid = "BedrockInvoke"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
      "bedrock:Converse",
      "bedrock:ConverseStream",
    ]
    resources = [
      "arn:aws:bedrock:*::foundation-model/*",
      "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:inference-profile/*",
    ]
  }

  # Read the out-of-band Anthropic key parameter - nothing else in SSM.
  statement {
    sid       = "ReadBenchParameters"
    actions   = ["ssm:GetParameter"]
    resources = ["arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter/serving-econ-bench/*"]
  }

  # Results bucket only.
  statement {
    sid = "ResultsBucket"
    actions = [
      "s3:PutObject",
      "s3:GetObject",
      "s3:ListBucket",
    ]
    resources = [
      aws_s3_bucket.results.arn,
      "${aws_s3_bucket.results.arn}/*",
    ]
  }
}

resource "aws_iam_role_policy" "runner" {
  name_prefix = "serving-econ-bench-"
  role        = aws_iam_role.runner.id
  policy      = data.aws_iam_policy_document.runner.json
}

# Session Manager / send-command channel.
resource "aws_iam_role_policy_attachment" "ssm_core" {
  role       = aws_iam_role.runner.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "runner" {
  name_prefix = "serving-econ-bench-"
  role        = aws_iam_role.runner.name
}

# ---------------------------------------------------------------------------
# The runner instance
# ---------------------------------------------------------------------------

data "aws_ssm_parameter" "al2023_ami" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

resource "aws_instance" "runner" {
  ami                    = data.aws_ssm_parameter.al2023_ami.value
  instance_type          = var.instance_type
  subnet_id              = sort(data.aws_subnets.default.ids)[0]
  vpc_security_group_ids = [aws_security_group.runner.id]
  iam_instance_profile   = aws_iam_instance_profile.runner.name

  # Public IP is the egress path only (default VPC, no NAT). The SG has zero
  # ingress, so nothing can reach it.
  associate_public_ip_address = true

  metadata_options {
    http_tokens = "required" # IMDSv2 only
  }

  # Install uv system-wide; AL2023 already ships AWS CLI v2 and python3.
  # Nothing auto-runs - the instance idles until an SSM command arrives
  # (make run / make smoke). No secrets here, ever.
  user_data = <<-EOT
    #!/bin/bash
    set -euo pipefail
    curl -LsSf https://astral.sh/uv/install.sh | UV_INSTALL_DIR=/usr/local/bin sh
  EOT

  tags = {
    Name = "serving-econ-bench-runner"
  }
}
