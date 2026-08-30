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

resource "aws_dynamodb_table" "maily_category_types" {
    name = "Maily-CategoryTypes"
    billing_mode = "PAY_PER_REQUEST"
    hash_key = "userId"
    range_key = "categoryTypeId"
    deletion_protection_enabled = true

    attribute {
        name = "userId"
        type = "S"
    }

    attribute {
        name = "categoryTypeId"
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

# User-created "trip" wrappers for the built-in Travel category — a trip is just a name + date range;
# which Maily-CategoryItems rows belong to it is computed (a travel item's startDate falling inside the
# trip's range), never stored here or on the item, so editing a trip's dates naturally re-groups items
# without any migration.
resource "aws_dynamodb_table" "maily_travel_trips" {
  name                        = "Maily-TravelTrips"
  billing_mode                = "PAY_PER_REQUEST"
  hash_key                    = "userId"
  range_key                   = "tripId"
  deletion_protection_enabled = true

  attribute {
    name = "userId"
    type = "S"
  }

  attribute {
    name = "tripId"
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