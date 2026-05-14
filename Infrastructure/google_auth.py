import json
import urllib.request
import urllib.parse
import os
import boto3
import time

dynamodb = boto3.resource('dynamodb')
users_table = dynamodb.Table('Maily-Users')

_secrets_cache = None

def get_secrets():
    global _secrets_cache
    if _secrets_cache is None:
        client = boto3.client('secretsmanager')
        secret_name = os.environ['SECRET_NAME']
        response = client.get_secret_value(SecretId=secret_name)
        _secrets_cache = json.loads(response['SecretString'])
    return _secrets_cache

def lambda_handler(event, context):
    try:
        authorizer = event.get('requestContext', {}).get('authorizer', {})
        
        if 'jwt' in authorizer and 'claims' in authorizer['jwt']:
            user_id = authorizer['jwt']['claims'].get('sub')
        elif 'claims' in authorizer:
            user_id = authorizer['claims'].get('sub')
        else:
            user_id = None
        if not user_id:
            return {
                "statusCode": 401,
                "body": json.dumps({"error": "Unauthorized. Could not identify user."})
            }

        # get the auth code from the request body
        body = json.loads(event.get('body', '{}'))
        auth_code = body.get('code')

        if not auth_code:
            return {
                "statusCode": 400,
                "body": json.dumps({"error": "No auth code provided in the request"})
            }

        # get client credentials from Secrets Manager
        secrets = get_secrets()
        client_id = secrets['GOOGLE_CLIENT_ID']
        client_secret = secrets['GOOGLE_CLIENT_SECRET']
        redirect_uri = 'postmessage'

        # prepare the data for the token exchange request
        url = 'https://oauth2.googleapis.com/token'
        data = urllib.parse.urlencode({
            'code': auth_code,
            'client_id': client_id,
            'client_secret': client_secret,
            'redirect_uri': redirect_uri,
            'grant_type': 'authorization_code'
        }).encode('utf-8')

        # Explicitly tell Google we are sending form data
        req = urllib.request.Request(url, data=data, method='POST')
        req.add_header('Content-Type', 'application/x-www-form-urlencoded')
        
        # Invoke the request to Google's token endpoint
        with urllib.request.urlopen(req) as response:
            google_data = json.loads(response.read().decode('utf-8'))
        
        print("Google Tokens received successfully!")

        # Save the tokens to DynamoDB, linked to the user's Cognito ID
        users_table.put_item(
            Item={
                'userId': user_id,
                'google_access_token': google_data.get('access_token'),
                'google_refresh_token': google_data.get('refresh_token'),
                'token_expires_at': int(time.time()) + google_data.get('expires_in', 3600)
            }
        )

        return {
            "statusCode": 200,
            "headers": {
                "Content-Type": "application/json",
                "Access-Control-Allow-Origin": "*"
            },
            "body": json.dumps({
                "message": "Successfully authenticated with Google and saved tokens!",
                "google_status": "connected"
            })
        }

    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8')
        print(f"Google API Error: {error_body}")
        return {
            "statusCode": e.code,
            "body": json.dumps({
                "error": "Google authentication failed",
                "details": error_body
            })
        }
    except Exception as e:
        print(f"General Error: {str(e)}")
        return {
            "statusCode": 500,
            "body": json.dumps({"error": "Internal server error", "details": str(e)})
        }