resource "aws_amplify_app" "maily_frontend" {
  name         = "Maily"
  repository   = var.github_repository
  access_token = var.github_token

  # Build spec — tells Amplify how to build the Vite app inside the maily-web/ subdirectory
  build_spec = <<-EOT
    version: 1
    applications:
      - frontend:
          phases:
            preBuild:
              commands:
                - npm ci
            build:
              commands:
                - npm run build
          artifacts:
            baseDirectory: dist
            files:
              - '**/*'
          cache:
            paths:
              - node_modules/**/*
        appRoot: maily-web
  EOT

  # Inject all frontend environment variables at build time
  # Values are pulled directly from existing Terraform resources — no duplication
  environment_variables = {
    VITE_USER_POOL_ID        = aws_cognito_user_pool.user_pool.id
    VITE_USER_POOL_CLIENT_ID = aws_cognito_user_pool_client.maily_client.id
    VITE_GOOGLE_CLIENT_ID    = var.google_client_id
    VITE_API_BASE_URL        = "https://${aws_apigatewayv2_api.maily_http_api.id}.execute-api.us-east-1.amazonaws.com"
  }

  # Redirect all unknown paths to index.html so the React SPA handles routing
  custom_rule {
    source = "/<*>"
    status = "404-200"
    target = "/index.html"
  }

  tags = {
    Project     = "Maily"
    Environment = "Development"
  }
}

resource "aws_amplify_branch" "main" {
  app_id      = aws_amplify_app.maily_frontend.id
  branch_name = "main"

  enable_auto_build = true  # redeploy automatically on every git push to main
}

output "amplify_app_url" {
  description = "Public URL of the deployed Maily frontend"
  value       = "https://main.${aws_amplify_app.maily_frontend.default_domain}"
}
