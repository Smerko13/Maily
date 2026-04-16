import json
import boto3
import urllib.request
import urllib.error

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table('Maily-Emails')

def lambda_handler(event, context):
    print("Received event:", json.dumps(event))

    http_method = event.get('requestContext', {}).get('http', {}).get('method', '')
    path = event.get('rawPath', '')

    if http_method == 'GET' and path == '/hello':
        return handle_get_emails(event)
    elif http_method == 'POST' and path == '/sync':
        return handle_sync_emails(event)
    else:
        return {
            "statusCode": 404,
            "body": json.dumps({"message": f"Route not found! Method: {http_method}, Path: {path}"})
        }

def handle_get_emails(event):
    try:
        # Extract the user ID from the Cognito JWT claims
        authorizer = event.get('requestContext', {}).get('authorizer', {})
        user_id = None
        if 'jwt' in authorizer and 'claims' in authorizer['jwt']:
            user_id = authorizer['jwt']['claims'].get('sub')
        elif 'claims' in authorizer:
            user_id = authorizer['claims'].get('sub')

        if not user_id:
            return {"statusCode": 400, "body": json.dumps({"message": "Could not find user ID"})}

        # Query only this user's emails
        response = table.query(
            KeyConditionExpression=boto3.dynamodb.conditions.Key('userId').eq(user_id)
        )
        items = response.get('Items', [])
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"message": "Data fetched successfully!", "emails": items})
        }
    except Exception as e:
        print(f"Error reading from DynamoDB: {str(e)}")
        return {
            "statusCode": 500,
            "body": json.dumps({"message": "Failed to fetch data.", "error": str(e)})
        }

def handle_sync_emails(event):
    try:
        # Extract the user ID from the Cognito JWT claims
        request_context = event.get('requestContext', {})
        authorizer = request_context.get('authorizer', {})
        
        user_id = None
        if 'jwt' in authorizer and 'claims' in authorizer['jwt']:
            user_id = authorizer['jwt']['claims'].get('sub')
        elif 'claims' in authorizer:
            user_id = authorizer['claims'].get('sub')
            
        if not user_id:
            return {"statusCode": 400, "body": json.dumps({"message": "Could not find user ID"})}
        
        # Look up the user's stored Google tokens from Maily-Users
        users_table = dynamodb.Table('Maily-Users')
        result = users_table.get_item(Key={'userId': user_id})
        user_record = result.get('Item')
        
        if not user_record or not user_record.get('google_access_token'):
            return {
                "statusCode": 400,
                "body": json.dumps({"message": "Google account not connected. Please connect your Google account in Settings."})
            }
        
        access_token = user_record['google_access_token']

        # Fetch the list of recent email IDs from Gmail
        list_req = urllib.request.Request(
            'https://gmail.googleapis.com/gmail/v1/users/me/messages?maxResults=10'
        )
        list_req.add_header('Authorization', f'Bearer {access_token}')

        with urllib.request.urlopen(list_req) as resp:
            messages = json.loads(resp.read().decode('utf-8')).get('messages', [])

        if not messages:
            return {
                "statusCode": 200,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"message": "No emails found in your Gmail inbox."})
            }

        # Fetch metadata for each email and save to Maily-Emails
        saved_count = 0
        saved_emails = []
        for msg in messages:
            email_id = msg['id']

            detail_req = urllib.request.Request(
                f'https://gmail.googleapis.com/gmail/v1/users/me/messages/{email_id}'
                f'?format=metadata&metadataHeaders=Subject&metadataHeaders=From'
            )
            detail_req.add_header('Authorization', f'Bearer {access_token}')

            with urllib.request.urlopen(detail_req) as resp:
                email_data = json.loads(resp.read().decode('utf-8'))

            headers = email_data.get('payload', {}).get('headers', [])
            subject = next((h['value'] for h in headers if h['name'] == 'Subject'), '(No Subject)')
            sender  = next((h['value'] for h in headers if h['name'] == 'From'), '(Unknown Sender)')
            snippet = email_data.get('snippet', '')
            is_unread = 'UNREAD' in email_data.get('labelIds', [])

            table.put_item(Item={
                'userId':  user_id,
                'emailId': email_id,
                'subject': subject,
                'from':    sender,
                'content': snippet,
                'status':  'unread' if is_unread else 'read'
            })
            saved_emails.append({
                'emailId': email_id,
                'subject': subject,
                'from':    sender,
                'content': snippet,
                'status':  'unread' if is_unread else 'read'
            })
            saved_count += 1

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"message": f"Successfully synced {saved_count} emails!", "emails": saved_emails})
        }

    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8')
        print(f"Gmail API Error: {error_body}")
        return {
            "statusCode": e.code,
            "body": json.dumps({"message": "Failed to fetch emails from Gmail", "error": error_body})
        }
    except Exception as e:
        print(f"Error in sync process: {str(e)}")
        return {
            "statusCode": 500,
            "body": json.dumps({"message": "Internal server error during sync", "error": str(e)})
        }