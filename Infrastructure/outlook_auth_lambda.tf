data "archive_file" "outlook_auth_zip" {
  type        = "zip"
  source_file = "outlook_auth.py"
  output_path = "outlook_auth.zip"
}

resource "aws_lambda_function" "outlook_auth_lambda" {
  filename      = "outlook_auth.zip"
  function_name = "Maily-Outlook-Auth"

  role = local.lab_role_arn

  handler          = "outlook_auth.lambda_handler"
  runtime          = "python3.12"
  timeout          = 30
  source_code_hash = data.archive_file.outlook_auth_zip.output_base64sha256

  environment {
    variables = {
      SECRET_NAME = aws_secretsmanager_secret.maily_secrets.name
    }
  }
}
