data "aws_iam_role" "lab_role" {
  name = "LabRole"
}

data "archive_file" "backend_lambda_zip" {
    type = "zip"
    source_file = "${path.module}/backend_lambda.py"
    output_path = "${path.module}/backend_lambda.zip"
}

resource "aws_lambda_function" "maily_backend_lambda" {
    filename = "backend_lambda.zip"
    function_name = "Maily_Backend_Logic"

    role = data.aws_iam_role.lab_role.arn

    handler = "backend_lambda.lambda_handler"
    runtime = "python3.12"
    timeout = 120

    source_code_hash = data.archive_file.backend_lambda_zip.output_base64sha256

    environment {
      variables = {
        SECRET_NAME          = aws_secretsmanager_secret.maily_secrets.name
        EXPORTS_BUCKET_NAME  = aws_s3_bucket.maily_exports.bucket
      }
    }
}