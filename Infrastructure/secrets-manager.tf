# Store all sensitive credentials in Secrets Manager instead of plain Lambda environment variables.
# Both Lambda functions retrieve these at runtime using the SDK, avoiding plaintext secrets
# sitting in the Lambda console or Terraform state in cleartext.

resource "aws_secretsmanager_secret" "maily_secrets" {
  name        = "maily/app-secrets"
  description = "Google OAuth credentials and OpenAI API key for Maily"

  tags = {
    Project     = "Maily"
    Environment = "Development"
  }
}

resource "aws_secretsmanager_secret_version" "maily_secrets_version" {
  secret_id = aws_secretsmanager_secret.maily_secrets.id

  secret_string = jsonencode({
    GOOGLE_CLIENT_ID     = var.google_client_id
    GOOGLE_CLIENT_SECRET = var.google_client_secret
    OPENAI_API_KEY       = var.openai_api_key
  })
}
