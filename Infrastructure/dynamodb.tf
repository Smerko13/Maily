resource "aws_dynamodb_table" "maily_emails" {
    name = "Maily-Emails"
    billing_mode = "PAY_PER_REQUEST"
    hash_key = "userId" // Defines the partition key of the table as userId. This means DynamoDB first groups data by userId. So all records belonging to the same user are stored under the same partition key value.
    range_key = "emailId"  //Defines the sort key of the table as emailId. This means that inside each userId, items are sorted and uniquely identified by emailId
  
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