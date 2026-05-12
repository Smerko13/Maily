# S3 bucket for storing per-user email summary exports.
# Users trigger an export from the Statistics tab; the Lambda writes a JSON file
# here and returns a pre-signed URL so the user can download it directly.

resource "aws_s3_bucket" "maily_exports" {
  # Bucket names must be globally unique; we append the AWS account ID to ensure that.
  bucket = "maily-exports-${data.aws_caller_identity.current.account_id}"

  tags = {
    Project     = "Maily"
    Environment = "Development"
  }
}

# Block all public access — files are only reachable via pre-signed URLs issued by Lambda.
resource "aws_s3_bucket_public_access_block" "maily_exports_block" {
  bucket = aws_s3_bucket.maily_exports.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Automatically delete export files after 1 day to keep the bucket clean and costs low.
resource "aws_s3_bucket_lifecycle_configuration" "maily_exports_lifecycle" {
  bucket = aws_s3_bucket.maily_exports.id

  rule {
    id     = "expire-exports"
    status = "Enabled"

    expiration {
      days = 1
    }
  }
}

# Look up the current AWS account ID (used in the bucket name above).
data "aws_caller_identity" "current" {}

# Pass the bucket name into the backend Lambda as an environment variable.
# The Lambda uses it when writing export files and generating pre-signed URLs.
output "exports_bucket_name" {
  description = "S3 bucket used for email summary exports"
  value       = aws_s3_bucket.maily_exports.bucket
}
