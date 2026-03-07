import json
import boto3

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table('Maily-Emails')

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