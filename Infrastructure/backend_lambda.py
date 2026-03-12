import json
import boto3

dynamodb = boto3.resource('dynamodb')  # Create a DynamoDB resource using boto3, this allows us to interact with DynamoDB tables in our Lambda function
table = dynamodb.Table('Maily-Emails') # Reference to the DynamoDB table named 'Maily-Emails', this is where we will be reading data from in our Lambda function

# This is the main handler function for the Lambda, it will be invoked when a request is made to the API Gateway endpoint that is integrated with this Lambda function
def lambda_handler(event, context):
    try:
        # Read all items from the DynamoDB table
        response = table.scan()
        items = response.get('Items', [])
        
        # We are returning an object that contains both a message and the list of emails
        return {
            "statusCode": 200,
            "headers": {
                "Content-Type": "application/json"
            },
            "body": json.dumps({
                "message": "Data fetched successfully!",
                "emails": items
            })
        }
        
    except Exception as e:
        print(f"Error reading from DynamoDB: {str(e)}")
        return {
            "statusCode": 500,
            "body": json.dumps({
                "message": "Failed to fetch data.",
                "error": str(e)
            })
        }