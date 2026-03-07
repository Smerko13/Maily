# --- API Gateway ---

resource "aws_apigatewayv2_api" "maily_http_api" {
  name        = "Maily-Backend-API"
  description = "API Gateway for Maily Backend"
  protocol_type = "HTTP"

  cors_configuration {
    allow_origins = ["*"]
    allow_methods = ["GET", "POST", "PUT", "DELETE", "OPTIONS"]
    allow_headers = ["content-type", "authorization"]
  }

  tags = {
      Project     = "Maily"
      Environment = "Development"
  }
}

resource "aws_apigatewayv2_integration" "backend_lambda_integration" {
  api_id = aws_apigatewayv2_api.maily_http_api.id
  integration_type = "AWS_PROXY"
  integration_method = "POST"
  integration_uri = aws_lambda_function.maily_backend_lambda.invoke_arn
}

resource "aws_apigatewayv2_route" "hello_route" {
    api_id = aws_apigatewayv2_api.maily_http_api.id
    route_key = "GET /hello"
    target = "integrations/${aws_apigatewayv2_integration.backend_lambda_integration.id}"

    authorization_type = "JWT"
    authorizer_id = aws_apigatewayv2_authorizer.cognito_auth.id
}

resource "aws_apigatewayv2_stage" "default_stage" {
    api_id = aws_apigatewayv2_api.maily_http_api.id
    name = "$default"
    auto_deploy = true
}

resource "aws_lambda_permission" "api_gw_lambda_permission" {
    statement_id = "AllowExecutionFromAPIGateway"
    action = "lambda:InvokeFunction"
    function_name = aws_lambda_function.maily_backend_lambda.function_name
    principal = "apigateway.amazonaws.com"
    source_arn = "${aws_apigatewayv2_api.maily_http_api.execution_arn}/*/*"
}

resource "aws_apigatewayv2_authorizer" "cognito_auth" {
  api_id = aws_apigatewayv2_api.maily_http_api.id
  name = "cognito-authorizer"
  authorizer_type = "JWT"
  identity_sources = ["$request.header.Authorization"]

  jwt_configuration {
    issuer   = "https://${aws_cognito_user_pool.user_pool.endpoint}"
    audience = [aws_cognito_user_pool_client.maily_client.id]
  }
}

# --- Output API Gateway URL ---

output "api_endpoint" {
  description = "The URL to trigger the backend Lambda"
  value = aws_apigatewayv2_api.maily_http_api.api_endpoint
}