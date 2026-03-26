# zip the Lambda function code, this will create a zip file that can be uploaded to AWS Lambda, we use the archive_file data source to do this, it takes the source Python file and creates a zip archive that can be deployed as a Lambda function
data "archive_file" "google_auth_zip" {
  type        = "zip"
  source_file = "google_auth.py"
  output_path = "google_auth.zip"
}

# create the Lambda function for Google authentication, this function will handle the OAuth flow with Google, it will receive the auth code from the frontend, exchange it for tokens with Google's OAuth servers, and return the tokens to the frontend, we also set environment variables for the Google client ID and secret that the Lambda function will use to authenticate with Google's API
resource "aws_lambda_function" "google_auth_lambda" {
  filename         = "google_auth.zip"
  function_name    = "Maily-Google-Auth"
  
  # make sure to update the role ARN to the correct IAM role that has permissions for this Lambda function, this role should have at least basic Lambda execution permissions and any additional permissions needed for the function's logic (like logging to CloudWatch)
  role             = aws_iam_role.maily_backend_lambda_role.arn
  
  handler          = "google_auth.lambda_handler"
  runtime          = "python3.10"
  source_code_hash = data.archive_file.google_auth_zip.output_base64sha256

  environment {
    variables = {
      GOOGLE_CLIENT_ID     = var.google_client_id
      GOOGLE_CLIENT_SECRET = var.google_client_secret
    }
  }
}

# connect the new Lambda function to the API Gateway, we need to create a new integration for this Lambda function and then define a new route that will trigger this Lambda when a request is made to the /auth/google endpoint, we also need to set the same JWT authorization for this route to ensure it's protected by Cognito like our other routes
resource "aws_apigatewayv2_integration" "google_auth_integration" {
  api_id             = aws_apigatewayv2_api.maily_http_api.id
  integration_type   = "AWS_PROXY"
  integration_method = "POST"
  integration_uri    = aws_lambda_function.google_auth_lambda.invoke_arn
}

# create a new route for the Google authentication Lambda function, this route will be triggered when a POST request is made to the /auth/google endpoint, it will invoke the google_auth_lambda and use JWT authorization with Cognito to secure it
resource "aws_apigatewayv2_route" "google_auth_route" {
  api_id    = aws_apigatewayv2_api.maily_http_api.id
  route_key = "POST /auth/google"
  target    = "integrations/${aws_apigatewayv2_integration.google_auth_integration.id}"

  # use a JWT authorizer to secure this route with Cognito, this ensures that only authenticated users can access the Google authentication endpoint, we use the same authorizer we defined for the other routes
  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito_auth.id
}

# define a permission for the Lambda function to allow API Gateway to invoke it, this is necessary for the integration to work, the source_arn specifies which API Gateway routes are allowed to invoke the Lambda function, in this case we allow all routes of our API to invoke this Lambda, but you could restrict it further if needed
resource "aws_lambda_permission" "api_gw_google_auth_permission" {
  statement_id  = "AllowExecutionFromAPIGatewayGoogleAuth"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.google_auth_lambda.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.maily_http_api.execution_arn}/*/*"
}