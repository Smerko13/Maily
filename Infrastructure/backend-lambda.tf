data "aws_iam_role" "lab_role" {
  name = "LabRole"
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

    role = data.aws_iam_role.lab_role.arn

    handler = "backend_lambda.lambda_handler"
    runtime = "python3.12"
    timeout = 120

    source_code_hash = data.archive_file.backend_lambda_zip.output_base64sha256

    environment {
      variables = {
        # The Lambdas only receive the secret name; actual credentials are
        # fetched at runtime from Secrets Manager via the SDK.
        SECRET_NAME          = aws_secretsmanager_secret.maily_secrets.name
        EXPORTS_BUCKET_NAME  = aws_s3_bucket.maily_exports.bucket
      }
    }
}