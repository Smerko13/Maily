# --- API Gateway ---

resource "aws_apigatewayv2_api" "maily_http_api" {
  name        = "Maily-Backend-API"
  description = "API Gateway for Maily Backend"
  protocol_type = "HTTP"

  // CORS controls which frontend origins and HTTP methods are allowed to call this API from a browser
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

// Define the integration between API Gateway and the Lambda function, the API Gateway needs to know which Lambda function to invoke when a request is made to the API
resource "aws_apigatewayv2_integration" "backend_lambda_integration" {
  api_id = aws_apigatewayv2_api.maily_http_api.id
  integration_type = "AWS_PROXY"
  integration_method = "POST"
  integration_uri = aws_lambda_function.maily_backend_lambda.invoke_arn
}

// Define a route for the API Gateway, this route will be triggered when a GET request is made to the /hello endpoint, it will invoke the backend Lambda function and use JWT authorization with Cognito
resource "aws_apigatewayv2_route" "hello_route" {
    api_id = aws_apigatewayv2_api.maily_http_api.id
    route_key = "GET /hello"
    target = "integrations/${aws_apigatewayv2_integration.backend_lambda_integration.id}"

    authorization_type = "JWT"
    authorizer_id = aws_apigatewayv2_authorizer.cognito_auth.id
}

// Define a default stage for the API Gateway, this stage will be used to deploy the API and make it accessible to clients, auto_deploy is set to true so that any changes to the API will be automatically deployed to this stage
resource "aws_apigatewayv2_stage" "default_stage" {
    api_id = aws_apigatewayv2_api.maily_http_api.id
    name = "$default"
    auto_deploy = true
}
 // Define a permission for the Lambda function to allow API Gateway to invoke it, this is necessary for the integration to work, the source_arn specifies which API Gateway routes are allowed to invoke the Lambda function
resource "aws_lambda_permission" "api_gw_lambda_permission" {
    statement_id = "AllowExecutionFromAPIGateway"
    action = "lambda:InvokeFunction"
    function_name = aws_lambda_function.maily_backend_lambda.function_name
    principal = "apigateway.amazonaws.com"
    source_arn = "${aws_apigatewayv2_api.maily_http_api.execution_arn}/*/*"
}

// Define a Cognito User Pool Authorizer for the API Gateway, this authorizer will be used to secure the API endpoints with JWT tokens issued by the Cognito User Pool, the identity_sources specifies where the JWT token will be extracted from (in this case, the Authorization header of the request), and the jwt_configuration specifies the issuer and audience for the JWT tokens that will be accepted by this authorizer
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

// Output the API Gateway endpoint URL, this will be used by the frontend to make requests to the backend, the value is obtained from the api_endpoint attribute of the aws_apigatewayv2_api resource
output "api_endpoint" {
  description = "The URL to trigger the backend Lambda"
  value = aws_apigatewayv2_api.maily_http_api.api_endpoint
}