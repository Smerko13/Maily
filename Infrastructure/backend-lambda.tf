# --- IAM Role for Backend Lambda ---

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

# --- IAM Policy for Backend Lambda ---

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

# --- Attach Policy to Role ---

resource "aws_iam_role_policy_attachment" "lambda_dynamo_db_attach" {
    role = aws_iam_role.maily_backend_lambda_role.name
    policy_arn = aws_iam_policy.dynamo_db_access_policy.arn
}

# --- Allow Lambda to write logs to CloudWatch ---
resource "aws_iam_role_policy_attachment" "lambda_logs" {
    role = aws_iam_role.maily_backend_lambda_role.name
    policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# --- Backend Lambda Function ---

data "archive_file" "backend_lambda_zip" {
    type = "zip"
    source_file = "${path.module}/backend_lambda.py"
    output_path = "${path.module}/backend_lambda.zip"
}

resource "aws_lambda_function" "maily_backend_lambda" {
    filename = "backend_lambda.zip"
    function_name = "Maily_Backend_Logic"

    role = aws_iam_role.maily_backend_lambda_role.arn

    handler = "backend_lambda.lambda_handler"
    runtime = "python3.9"

    source_code_hash = data.archive_file.backend_lambda_zip.output_base64sha256
}