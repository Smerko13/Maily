import json
import urllib.request
import urllib.parse
import os

def lambda_handler(event, context):
    try:
        # get the auth code from the request body, this code is sent by the frontend after a successful Google login, we will use this code to exchange for access tokens from Google's OAuth servers
        body = json.loads(event.get('body', '{}'))
        auth_code = body.get('code')

        if not auth_code:
            return {
                "statusCode": 400,
                "body": json.dumps({"error": "No auth code provided in the request"})
            }

        # get client credentials from environment variables, these should be set in the Lambda function's configuration, they are necessary to authenticate our request to Google's OAuth servers when we exchange the auth code for tokens
        client_id = os.environ.get('GOOGLE_CLIENT_ID')
        client_secret = os.environ.get('GOOGLE_CLIENT_SECRET')
        redirect_uri = 'http://localhost:5173'

        # prepare the data for the token exchange request, we need to send a POST request to Google's token endpoint with the auth code and our client credentials, we encode the data as application/x-www-form-urlencoded as required by Google's API
        url = 'https://oauth2.googleapis.com/token'
        data = urllib.parse.urlencode({
            'code': auth_code,
            'client_id': client_id,
            'client_secret': client_secret,
            'redirect_uri': redirect_uri,
            'grant_type': 'authorization_code'
        }).encode('utf-8')

        req = urllib.request.Request(url, data=data, method='POST')
        
        # 4. Invoke the request to Google's token endpoint, this will exchange the auth code for access and refresh tokens, we read the response and parse it as JSON to get the token data
        with urllib.request.urlopen(req) as response:
            google_data = json.loads(response.read().decode('utf-8'))
        
        print("Google Tokens received successfully!")

        return {
            "statusCode": 200,
            "headers": {
                "Content-Type": "application/json",
                "Access-Control-Allow-Origin": "*"
            },
            "body": json.dumps({
                "message": "Successfully authenticated with Google!",
                "google_status": "connected"
            })
        }

    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8')
        print(f"Google API Error: {error_body}")
        return {
            "statusCode": e.code,
            "body": json.dumps({"error": "Google authentication failed", "details": error_body})
        }
    except Exception as e:
        print(f"General Error: {str(e)}")
        return {
            "statusCode": 500,
            "body": json.dumps({"error": "Internal server error", "details": str(e)})
        }