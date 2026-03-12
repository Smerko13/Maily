# --- Amazon Cognito ---

# Create a Cognito User Pool
resource "aws_cognito_user_pool" "user_pool" {
  name = "maily-user-pool"

  # Configure password policy
  password_policy {
    minimum_length    = 8
    require_uppercase = true
    require_lowercase = true
    require_numbers   = true
    require_symbols   = false
  }

  # Enable email as a sign-in option
  auto_verified_attributes = ["email"]
  username_attributes = ["email"]

  # We want to turn off MFA (as of now)
  mfa_configuration = "OFF"

    tags = {
        Project     = "Maily"
        Environment = "Development"
    }
}

# Create a Cognito User Pool Client
resource "aws_cognito_user_pool_client" "maily_client" {
  name         = "maily-react-client"
  user_pool_id = aws_cognito_user_pool.user_pool.id
  generate_secret = false

  explicit_auth_flows = [
    "ALLOW_USER_PASSWORD_AUTH",
    "ALLOW_REFRESH_TOKEN_AUTH",
    "ALLOW_USER_SRP_AUTH"
    ]
}

# --- Output Cognito User Pool details ---
output "cognito_user_pool_id" {
  description = "The ID of the Cognito User Pool"
  value       = aws_cognito_user_pool.user_pool.id
}

output "cognito_client_id" {
  description = "The Client ID of the Cognito User Pool Client"
  value       = aws_cognito_user_pool_client.maily_client.id
}