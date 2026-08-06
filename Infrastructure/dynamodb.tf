resource "aws_dynamodb_table" "maily_emails" {
    name = "Maily-Emails"
    billing_mode = "PAY_PER_REQUEST"
    hash_key = "userId" 
    range_key = "emailId"
    deletion_protection_enabled = true  
  
    attribute {
        name = "userId"
        type = "S"
    }

    attribute {
        name = "emailId"
        type = "S"
    }

    attribute {
        name = "threadId"
        type = "S"
    }

    global_secondary_index {
        name            = "threadId-index"
        hash_key        = "userId"
        range_key       = "threadId"
        projection_type = "ALL"
    }

    point_in_time_recovery {
      enabled = true
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
    deletion_protection_enabled = true
  
    attribute {
        name = "userId"
        type = "S"
    }

    point_in_time_recovery {
      enabled = true
    }

    tags = {
        Project = "Maily"
        Environment = "Development"
    }
}