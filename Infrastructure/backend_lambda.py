import json

def lambda_handler(event, context):
    print("Received event: ", event)
    return {
        'statusCode': 200,
        'body': json.dumps("Hello from Maily Backend! The lambda is working.")
    }