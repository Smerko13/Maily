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

resource "aws_dynamodb_table" "maily_users" {
    name = "Maily-Users"
    billing_mode = "PAY_PER_REQUEST"
    hash_key = "userId" 
  
    attribute {
        name = "userId"
        type = "S"
    }

    tags = {
        Project = "Maily"
        Environment = "Development"
    }
}