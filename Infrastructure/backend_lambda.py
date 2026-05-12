import json
import os
import urllib.parse
import boto3
import urllib.request
import urllib.error

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table('Maily-Emails')

def refresh_google_access_token(user_id, refresh_token):
    # Exchange the refresh token for a new access token with Google
    client_id = os.environ.get('GOOGLE_CLIENT_ID', '').strip()
    client_secret = os.environ.get('GOOGLE_CLIENT_SECRET', '').strip()

    data = urllib.parse.urlencode({
        'client_id': client_id,
        'client_secret': client_secret,
        'refresh_token': refresh_token,
        'grant_type': 'refresh_token'
    }).encode('utf-8')

    req = urllib.request.Request('https://oauth2.googleapis.com/token', data=data, method='POST')
    req.add_header('Content-Type', 'application/x-www-form-urlencoded')

    with urllib.request.urlopen(req) as resp:
        token_data = json.loads(resp.read().decode('utf-8'))

    new_access_token = token_data['access_token']

    # Save the new access token back to DynamoDB
    users_table = dynamodb.Table('Maily-Users')
    users_table.update_item(
        Key={'userId': user_id},
        UpdateExpression='SET google_access_token = :t',
        ExpressionAttributeValues={':t': new_access_token}
    )

    return new_access_token

def summarize_email(subject, snippet):
    api_key = os.environ.get('OPENAI_API_KEY', '').strip()
    prompt = f"Summarize this email in 1-2 sentences.\n\nSubject: {subject}\n\nContent: {snippet}"

    body = json.dumps({
        "model": "gpt-4.1-nano",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 100
    }).encode('utf-8')

    req = urllib.request.Request(
        'https://api.openai.com/v1/chat/completions',
        data=body,
        method='POST'
    )
    req.add_header('Authorization', f'Bearer {api_key}')
    req.add_header('Content-Type', 'application/json')

    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read().decode('utf-8'))

    return result['choices'][0]['message']['content'].strip()

def lambda_handler(event, context):
    print("Received event:", json.dumps(event))

    # Handle both payload format v1.0 and v2.0
    http_method = event.get('httpMethod') or event.get('requestContext', {}).get('http', {}).get('method', '')
    path = event.get('path') or event.get('rawPath', '')

    if http_method == 'GET' and path == '/hello':
        return handle_get_emails(event)
    elif http_method == 'POST' and path == '/sync':
        return handle_sync_emails(event)
    elif http_method == 'GET' and path == '/stats':
        return handle_get_stats(event)
    elif http_method == 'POST' and path == '/draft':
        return handle_draft_email(event)
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
            return {
                "statusCode": 401,
                "body": json.dumps({"error": "Unauthorized. Could not identify user."})
            }

        # Query all of this user's emails, paginating through DynamoDB results
        # (a single query only returns up to 1MB; LastEvaluatedKey means there's more)
        items = []
        response = table.query(
            KeyConditionExpression=boto3.dynamodb.conditions.Key('userId').eq(user_id)
        )
        items.extend(response.get('Items', []))
        while 'LastEvaluatedKey' in response:
            response = table.query(
                KeyConditionExpression=boto3.dynamodb.conditions.Key('userId').eq(user_id),
                ExclusiveStartKey=response['LastEvaluatedKey']
            )
            items.extend(response.get('Items', []))
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
        refresh_token = user_record.get('google_refresh_token')

        # Fetch the list of recent email IDs from Gmail, refreshing the token once if it has expired
        def gmail_get(url, token):
            req = urllib.request.Request(url)
            req.add_header('Authorization', f'Bearer {token}')
            return req

        try:
            list_req = gmail_get(
                'https://gmail.googleapis.com/gmail/v1/users/me/messages?maxResults=10',
                access_token
            )
            with urllib.request.urlopen(list_req) as resp:
                messages = json.loads(resp.read().decode('utf-8')).get('messages', [])
        except urllib.error.HTTPError as e:
            if e.code == 401 and refresh_token:
                print("Access token expired, refreshing...")
                access_token = refresh_google_access_token(user_id, refresh_token)
                list_req = gmail_get(
                    'https://gmail.googleapis.com/gmail/v1/users/me/messages?maxResults=10',
                    access_token
                )
                with urllib.request.urlopen(list_req) as resp:
                    messages = json.loads(resp.read().decode('utf-8')).get('messages', [])
            else:
                raise

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

            detail_req = gmail_get(
                f'https://gmail.googleapis.com/gmail/v1/users/me/messages/{email_id}'
                f'?format=metadata&metadataHeaders=Subject&metadataHeaders=From',
                access_token
            )
            with urllib.request.urlopen(detail_req) as resp:
                email_data = json.loads(resp.read().decode('utf-8'))

            headers = email_data.get('payload', {}).get('headers', [])
            subject = next((h['value'] for h in headers if h['name'] == 'Subject'), '(No Subject)')
            sender  = next((h['value'] for h in headers if h['name'] == 'From'), '(Unknown Sender)')
            snippet = email_data.get('snippet', '')
            is_unread = 'UNREAD' in email_data.get('labelIds', [])

            summary = summarize_email(subject, snippet)

            table.put_item(Item={
                'userId':  user_id,
                'emailId': email_id,
                'subject': subject,
                'from':    sender,
                'content': snippet,
                'summary': summary,
                'status':  'unread' if is_unread else 'read'
            })
            saved_emails.append({
                'emailId': email_id,
                'subject': subject,
                'from':    sender,
                'content': snippet,
                'summary': summary,
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
        print(f"Google API Error: {error_body}")
        return {
            "statusCode": e.code,
            "body": json.dumps({
                "error": "Google authentication failed",
                "details": error_body
            })
        }
    except Exception as e:
        print(f"Error in sync process: {str(e)}")
        return {
            "statusCode": 500,
            "body": json.dumps({"message": "Internal server error during sync", "error": str(e)})
        }

def handle_get_stats(event):
    try:
        # Extract the user ID from the Cognito JWT claims
        authorizer = event.get('requestContext', {}).get('authorizer', {})
        user_id = None
        if 'jwt' in authorizer and 'claims' in authorizer['jwt']:
            user_id = authorizer['jwt']['claims'].get('sub')
        elif 'claims' in authorizer:
            user_id = authorizer['claims'].get('sub')

        if not user_id:
            return {
                "statusCode": 401,
                "body": json.dumps({"error": "Unauthorized. Could not identify user."})
            }

        # Fetch all of this user's emails from DynamoDB, with pagination
        emails = []
        response = table.query(
            KeyConditionExpression=boto3.dynamodb.conditions.Key('userId').eq(user_id)
        )
        emails.extend(response.get('Items', []))
        while 'LastEvaluatedKey' in response:
            response = table.query(
                KeyConditionExpression=boto3.dynamodb.conditions.Key('userId').eq(user_id),
                ExclusiveStartKey=response['LastEvaluatedKey']
            )
            emails.extend(response.get('Items', []))

        if not emails:
            return {
                "statusCode": 200,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"total": 0, "unread": 0, "read": 0, "top_senders": []})
            }

        # Count read vs unread
        total = len(emails)
        unread = sum(1 for e in emails if e.get('status') == 'unread')
        read = total - unread

        # Count emails per sender and return the top 5
        sender_counts = {}
        for e in emails:
            sender = e.get('from', 'Unknown')
            sender_counts[sender] = sender_counts.get(sender, 0) + 1

        top_senders = sorted(sender_counts.items(), key=lambda x: x[1], reverse=True)[:5]
        top_senders = [{"sender": s, "count": c} for s, c in top_senders]

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({
                "total": total,
                "unread": unread,
                "read": read,
                "top_senders": top_senders
            })
        }

    except Exception as e:
        print(f"Error in stats: {str(e)}")
        return {
            "statusCode": 500,
            "body": json.dumps({"message": "Internal server error during stats", "error": str(e)})
        }

def handle_draft_email(event):
    try:
        # Extract the user ID from the Cognito JWT claims
        authorizer = event.get('requestContext', {}).get('authorizer', {})
        user_id = None
        if 'jwt' in authorizer and 'claims' in authorizer['jwt']:
            user_id = authorizer['jwt']['claims'].get('sub')
        elif 'claims' in authorizer:
            user_id = authorizer['claims'].get('sub')

        if not user_id:
            return {
                "statusCode": 401,
                "body": json.dumps({"error": "Unauthorized. Could not identify user."})
            }

        # Get the email details sent from the frontend
        body = json.loads(event.get('body', '{}'))
        subject = body.get('subject', '(No Subject)')
        summary = body.get('summary', '')
        content = body.get('content', '')

        if not summary and not content:
            return {
                "statusCode": 400,
                "body": json.dumps({"error": "No email content provided to draft a reply for."})
            }

        # Call OpenAI to generate a reply draft
        api_key = os.environ.get('OPENAI_API_KEY', '').strip()
        prompt = (
            f"You are a helpful email assistant. Write a professional and polite reply to the following email.\n\n"
            f"Subject: {subject}\n"
            f"Summary: {summary}\n"
            f"Original snippet: {content}\n\n"
            f"Write only the body of the reply, without a subject line or greeting header."
        )

        request_body = json.dumps({
            "model": "gpt-4.1-nano",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 300
        }).encode('utf-8')

        req = urllib.request.Request(
            'https://api.openai.com/v1/chat/completions',
            data=request_body,
            method='POST'
        )
        req.add_header('Authorization', f'Bearer {api_key}')
        req.add_header('Content-Type', 'application/json')

        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read().decode('utf-8'))

        draft = result['choices'][0]['message']['content'].strip()

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"draft": draft})
        }

    except Exception as e:
        print(f"Error generating draft: {str(e)}")
        return {
            "statusCode": 500,
            "body": json.dumps({"message": "Internal server error during draft generation", "error": str(e)})
        }
