
// Define an IAM role for the backend Lambda function, this role will allow the Lambda function to assume the necessary permissions to access other AWS services (like DynamoDB) and to write logs to CloudWatch, the assume_role_policy specifies that this role can be assumed by the Lambda service
resource "aws_iam_role" "maily_backend_lambda_role" {
    name = "maily_backend_lambda_role"
    assume_role_policy = jsonencode({
        Version = "2012-10-17"
        Statement = [
            {
                Action = "sts:AssumeRole"
                Effect = "Allow"
                Principal = {
                    Service = "lambda.amazonaws.com"
                }
            }
        ]
    })
}

// Define an IAM policy that allows the backend Lambda function to access the DynamoDB table, this policy will be attached to the IAM role of the Lambda function, the policy allows various DynamoDB actions (like PutItem, GetItem, etc.) on the specific DynamoDB table used by Maily
resource "aws_iam_policy" "dynamo_db_access_policy" {
    name = "maily_dynamodb_access_policy"
    description = "Policy to allow Lambda function to access Maily DynamoDB table"
    policy = jsonencode({
        Version = "2012-10-17"
        Statement = [
            {
                Action = [
                    "dynamodb:PutItem",
                    "dynamodb:GetItem",
                    "dynamodb:UpdateItem",
                    "dynamodb:DeleteItem",
                    "dynamodb:Scan",
                    "dynamodb:Query"
                ]
                Effect = "Allow"
                Resource = aws_dynamodb_table.maily_emails.arn
            }
        ]
    })
}

// Attach the DynamoDB access policy to the Lambda's IAM role, this allows the Lambda function to perform the specified actions on the DynamoDB table
resource "aws_iam_role_policy_attachment" "lambda_dynamo_db_attach" {
    role = aws_iam_role.maily_backend_lambda_role.name
    policy_arn = aws_iam_policy.dynamo_db_access_policy.arn
}

// Attach the AWSLambdaBasicExecutionRole policy to the Lambda's IAM role, this is a managed policy provided by AWS that allows the Lambda function to write logs to CloudWatch, this is important for monitoring and debugging the Lambda function
resource "aws_iam_role_policy_attachment" "lambda_logs" {
    role = aws_iam_role.maily_backend_lambda_role.name
    policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

// Create a ZIP archive of the backend Lambda function code, this is necessary because AWS Lambda requires the code to be uploaded as a ZIP file, the source_file points to the Python file containing the Lambda function code, and the output_path specifies where the ZIP file will be created
data "archive_file" "backend_lambda_zip" {
    type = "zip"
    source_file = "${path.module}/backend_lambda.py"
    output_path = "${path.module}/backend_lambda.zip"
}

// Define the backend Lambda function, this resource creates a Lambda function in AWS with the specified configuration, the filename points to the ZIP file created from the previous step, the function_name is the name of the Lambda function, the role specifies which IAM role the Lambda function will assume when it executes, the handler specifies the entry point for the Lambda function (the Python file and function name), and the runtime specifies which version of Python to use for the Lambda function
resource "aws_lambda_function" "maily_backend_lambda" {
    filename = "backend_lambda.zip"
    function_name = "Maily_Backend_Logic"

    role = aws_iam_role.maily_backend_lambda_role.arn

    handler = "backend_lambda.lambda_handler"
    runtime = "python3.9"

    source_code_hash = data.archive_file.backend_lambda_zip.output_base64sha256
}