resource "aws_dynamodb_table" "maily_emails" {
    name = "Maily-Emails"
    billing_mode = "PAY_PER_REQUEST"
    hash_key = "userId"
    range_key = "emailId"
  
    attribute {
        name = "userId"
        type = "S"
    }

    attribute {
        name = "emailId"
        type = "S"
    }

    tags = {
        Project = "Maily"
        Environment = "Development"
    }
}