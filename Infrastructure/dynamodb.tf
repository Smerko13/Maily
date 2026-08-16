resource "aws_dynamodb_table" "maily_emails" {
  name                        = "Maily-Emails"
  billing_mode                = "PAY_PER_REQUEST"
  hash_key                    = "userId"
  range_key                   = "emailId"
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
    projection_type = "ALL"

    key_schema {
      attribute_name = "userId"
      key_type       = "HASH"
    }

    key_schema {
      attribute_name = "threadId"
      key_type       = "RANGE"
    }
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = {
    Project     = "Maily"
    Environment = "Development"
  }
}

resource "aws_dynamodb_table" "maily_users" {
  name                        = "Maily-Users"
  billing_mode                = "PAY_PER_REQUEST"
  hash_key                    = "userId"
  deletion_protection_enabled = true

  attribute {
    name = "userId"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = {
    Project     = "Maily"
    Environment = "Development"
  }
}

resource "aws_dynamodb_table" "maily_labels" {
  name                        = "Maily-Labels"
  billing_mode                = "PAY_PER_REQUEST"
  hash_key                    = "userId"
  range_key                   = "labelId"
  deletion_protection_enabled = true

  attribute {
    name = "userId"
    type = "S"
  }

  attribute {
    name = "labelId"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = {
    Project     = "Maily"
    Environment = "Development"
  }
}

resource "aws_dynamodb_table" "maily_category_items" {
  name                        = "Maily-CategoryItems"
  billing_mode                = "PAY_PER_REQUEST"
  hash_key                    = "userId"
  range_key                   = "itemId"
  deletion_protection_enabled = true

  attribute {
    name = "userId"
    type = "S"
  }

  attribute {
    name = "itemId"
    type = "S"
  }

  attribute {
    name = "categoryType"
    type = "S"
  }

  global_secondary_index {
    name            = "categoryType-index"
    projection_type = "ALL"

    key_schema {
      attribute_name = "userId"
      key_type       = "HASH"
    }

    key_schema {
      attribute_name = "categoryType"
      key_type       = "RANGE"
    }
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = {
    Project     = "Maily"
    Environment = "Development"
  }
}