import json
import boto3
import uuid

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table('Maily-Emails')

def lambda_handler(event, context):
    try:
        email_id = str(uuid.uuid4())
        item = {
            'userId': 'daniel_test_user_1',
            'emailId': email_id,
            'subject': 'First Test Email',
            'content': 'This email was written to the database directly from our API Gateway and Lambda!',
            'status': 'processed'
        }
        table.put_item(Item=item)
        return {
            'statusCode': 200,
            "body": json.dumps(f"Success! Email record {email_id} was saved to DynamoDB.")
        }
    except Exception as e:
        print(f"Error saving to DynamoDB: {str(e)}")
        return {
            "statusCode": 500,
            "body": json.dumps("Failed to save data. Check Lambda logs.")
        }