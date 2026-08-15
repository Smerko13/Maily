data "archive_file" "google_auth_zip" {
  type        = "zip"
  source_file = "google_auth.py"
  output_path = "google_auth.zip"
}

resource "aws_lambda_function" "google_auth_lambda" {
  filename      = "google_auth.zip"
  function_name = "Maily-Google-Auth"

  role = local.lab_role_arn

  handler          = "google_auth.lambda_handler"
  runtime          = "python3.12"
  timeout          = 30
  source_code_hash = data.archive_file.google_auth_zip.output_base64sha256

  environment {
    variables = {
      SECRET_NAME = aws_secretsmanager_secret.maily_secrets.name
    }
  }
}



