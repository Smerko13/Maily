resource "aws_s3_bucket" "maily_exports" {
  bucket = "maily-exports-${data.aws_caller_identity.current.account_id}"

  tags = {
    Project     = "Maily"
    Environment = "Development"
  }
}

resource "aws_s3_bucket_public_access_block" "maily_exports_block" {
  bucket = aws_s3_bucket.maily_exports.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

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

data "aws_caller_identity" "current" {}

output "exports_bucket_name" {
  description = "S3 bucket used for email summary exports"
  value       = aws_s3_bucket.maily_exports.bucket
}
