output "instance_id" {
  description = "Runner instance ID (SSM send-command / start-session target)."
  value       = aws_instance.runner.id
}

output "bucket_name" {
  description = "S3 bucket holding the code zip and results."
  value       = aws_s3_bucket.results.bucket
}

output "region" {
  description = "Region everything lives in."
  value       = var.region
}

output "session_command" {
  description = "Interactive shell on the runner, if you ever need one (no SSH exists)."
  value       = "aws ssm start-session --target ${aws_instance.runner.id} --region ${var.region}"
}
