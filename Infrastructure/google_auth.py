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

        access_token = google_data.get('access_token')
        refresh_token = google_data.get('refresh_token')
        expires_at = int(time.time()) + google_data.get('expires_in', 3600)

        # Fetch the Gmail email address for this account
        profile_req = urllib.request.Request('https://www.googleapis.com/gmail/v1/users/me/profile')
        profile_req.add_header('Authorization', f'Bearer {access_token}')
        with urllib.request.urlopen(profile_req) as resp:
            profile = json.loads(resp.read().decode('utf-8'))
        google_email = profile.get('emailAddress')
        print(f"Connecting Google account: {google_email}")

        # Read the user's existing accounts list
        result = users_table.get_item(Key={'userId': user_id})
        existing_item = result.get('Item', {})
        accounts = existing_item.get('email_accounts', [])

        # Look up the existing entry for this account (if any) before rebuilding the list below —
        # used to preserve the refresh_token and primary status across a re-auth/reconnect.
        existing = next((a for a in accounts if a.get('email') == google_email and a.get('provider') == 'gmail'), None)

        # If Google didn't return a refresh_token (happens on re-auth), keep the existing one
        if not refresh_token and existing:
            refresh_token = existing.get('refresh_token')

        # The first account a user ever connects becomes "primary" (e.g. Compose's default sender);
        # reconnecting an already-connected account preserves whatever primary status it already had.
        is_primary = existing.get('isPrimary', False) if existing else (len(accounts) == 0)

        # Replace the account if it already exists, otherwise append
        new_account = {
            'provider': 'gmail',
            'email': google_email,
            'access_token': access_token,
            'refresh_token': refresh_token,
            'token_expires_at': expires_at,
            'isPrimary': is_primary,
            'needsReauth': False
        }
        accounts = [a for a in accounts if not (a.get('email') == google_email and a.get('provider') == 'gmail')]
        accounts.append(new_account)

        # Save the updated accounts list (preserves email_fetch_limit and other fields)
        users_table.update_item(
            Key={'userId': user_id},
            UpdateExpression='SET email_accounts = :accounts',
            ExpressionAttributeValues={':accounts': accounts}
        )

        return {
            "statusCode": 200,
            "headers": {
                "Content-Type": "application/json",
                "Access-Control-Allow-Origin": "*"
            },
            "body": json.dumps({
                "message": "Successfully authenticated with Google and saved tokens!",
                "google_status": "connected",
                "email": google_email
            })
        }

    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8')
        print(f"Google API Error: {error_body}")
        return {
            "statusCode": e.code,
            "body": json.dumps({
                "error": "Google authentication failed"
            })
        }
    except Exception as e:
        print(f"General Error: {str(e)}")
        return {
            "statusCode": 500,
            "body": json.dumps({"error": "Internal server error"})
        }