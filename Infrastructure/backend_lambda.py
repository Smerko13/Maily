import json
import os
import time
import urllib.parse
import boto3
import urllib.request
import urllib.error

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table('Maily-Emails')

_secrets_cache = None

def get_secrets():
    global _secrets_cache
    if _secrets_cache is None:
        client = boto3.client('secretsmanager')
        secret_name = os.environ['SECRET_NAME']
        response = client.get_secret_value(SecretId=secret_name)
        _secrets_cache = json.loads(response['SecretString'])
    return _secrets_cache

def refresh_google_access_token(user_id, google_email, refresh_token):
    secrets = get_secrets()
    client_id = secrets['GOOGLE_CLIENT_ID']
    client_secret = secrets['GOOGLE_CLIENT_SECRET']

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

    # Update just this account's token inside the google_accounts list
    users_table = dynamodb.Table('Maily-Users')
    result = users_table.get_item(Key={'userId': user_id})
    accounts = result.get('Item', {}).get('google_accounts', [])
    for account in accounts:
        if account.get('email') == google_email:
            account['access_token'] = new_access_token
            account['token_expires_at'] = int(time.time()) + 3600
            break
    users_table.update_item(
        Key={'userId': user_id},
        UpdateExpression='SET google_accounts = :accounts',
        ExpressionAttributeValues={':accounts': accounts}
    )

    return new_access_token

def summarize_email(subject, snippet):
    api_key = get_secrets()['OPENAI_API_KEY']
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

    # Detect EventBridge scheduled event (not an HTTP request)
    if event.get('source') == 'aws.events' or event.get('detail-type') == 'Scheduled Event':
        return handle_scheduled_sync()

    http_method = event.get('httpMethod') or event.get('requestContext', {}).get('http', {}).get('method', '')
    path = event.get('path') or event.get('rawPath', '')

    if http_method == 'GET' and path == '/hello': 
        return handle_get_emails(event)
    elif http_method == 'POST' and path == '/sync': 
        return handle_sync_emails(event)
    elif http_method == 'GET' and path == '/accounts':
        return handle_get_accounts(event)
    elif http_method == 'DELETE' and path == '/auth/google':
        return handle_disconnect_google(event)
    elif http_method == 'GET' and path == '/stats':
        return handle_get_stats(event)
    elif http_method == 'POST' and path == '/draft':
        return handle_draft_email(event)
    elif http_method == 'POST' and path == '/export':
        return handle_export(event)
    elif http_method == 'POST' and path == '/settings':
        return handle_save_settings(event)
    else:
        return {
            "statusCode": 404,
            "body": json.dumps({"message": f"Route not found! Method: {http_method}, Path: {path}"})
        }

def handle_save_settings(event):
    try:
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

        body = json.loads(event.get('body', '{}'))
        raw_limit = body.get('email_fetch_limit')

        if raw_limit is None:
            return {
                "statusCode": 400,
                "body": json.dumps({"error": "Missing required field: email_fetch_limit"})
            }

        try:
            limit = int(raw_limit)
        except (ValueError, TypeError):
            return {
                "statusCode": 400,
                "body": json.dumps({"error": "email_fetch_limit must be an integer"})
            }

        if not (1 <= limit <= 100):
            return {
                "statusCode": 400,
                "body": json.dumps({"error": "email_fetch_limit must be between 1 and 100"})
            }

        users_table = dynamodb.Table('Maily-Users')
        users_table.update_item(
            Key={'userId': user_id},
            UpdateExpression='SET email_fetch_limit = :l',
            ExpressionAttributeValues={':l': limit}
        )

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"message": f"Settings saved. Fetch limit set to {limit}."})
        }

    except Exception as e:
        print(f"Error saving settings: {str(e)}")
        return {
            "statusCode": 500,
            "body": json.dumps({"message": "Internal server error while saving settings", "error": str(e)})
        }

def handle_get_emails(event):
    try:
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

        # Optional filter: ?account=user@gmail.com
        query_params = event.get('queryStringParameters') or {}
        account_filter = query_params.get('account')

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

        if account_filter:
            items = [e for e in items if e.get('google_email') == account_filter]

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"message": "Data fetched successfully!", "emails": items}, ensure_ascii=False)
        }
    except Exception as e:
        print(f"Error reading from DynamoDB: {str(e)}")
        return {
            "statusCode": 500,
            "body": json.dumps({"message": "Failed to fetch data.", "error": str(e)})
        }

def sync_single_account(user_id, account, fetch_limit):
    """Fetch and store emails for one connected Google account."""
    google_email = account['email']
    access_token = account['access_token']
    refresh_token = account.get('refresh_token')

    def gmail_get(url, token):
        req = urllib.request.Request(url)
        req.add_header('Authorization', f'Bearer {token}')
        return req

    try:
        list_req = gmail_get(
            f'https://gmail.googleapis.com/gmail/v1/users/me/messages?maxResults={fetch_limit}',
            access_token
        )
        with urllib.request.urlopen(list_req) as resp:
            messages = json.loads(resp.read().decode('utf-8')).get('messages', [])
    except urllib.error.HTTPError as e:
        if e.code == 401 and refresh_token:
            print(f"Token expired for {google_email}, refreshing...")
            access_token = refresh_google_access_token(user_id, google_email, refresh_token)
            list_req = gmail_get(
                f'https://gmail.googleapis.com/gmail/v1/users/me/messages?maxResults={fetch_limit}',
                access_token
            )
            with urllib.request.urlopen(list_req) as resp:
                messages = json.loads(resp.read().decode('utf-8')).get('messages', [])
        else:
            raise

    if not messages:
        return [], 0

    saved_count = 0
    saved_emails = []
    for msg in messages:
        raw_id = msg['id']
        email_id = f"{google_email}#{raw_id}"  # prefix keeps emailId unique across accounts

        detail_req = gmail_get(
            f'https://gmail.googleapis.com/gmail/v1/users/me/messages/{raw_id}'
            f'?format=metadata&metadataHeaders=Subject&metadataHeaders=From',
            access_token
        )
        with urllib.request.urlopen(detail_req) as resp:
            email_data = json.loads(resp.read().decode('utf-8'))

        headers = email_data.get('payload', {}).get('headers', [])
        subject = next((h['value'] for h in headers if h['name'] == 'Subject'), '(No Subject)')
        sender  = next((h['value'] for h in headers if h['name'] == 'From'), '(Unknown Sender)')
        snippet = email_data.get('snippet', '').replace('\u034f', '').strip()
        is_unread = 'UNREAD' in email_data.get('labelIds', [])

        summary = summarize_email(subject, snippet)

        table.put_item(Item={
            'userId':       user_id,
            'emailId':      email_id,
            'subject':      subject,
            'from':         sender,
            'content':      snippet,
            'summary':      summary,
            'status':       'unread' if is_unread else 'read',
            'google_email': google_email
        })
        saved_emails.append({
            'emailId':      email_id,
            'subject':      subject,
            'from':         sender,
            'content':      snippet,
            'summary':      summary,
            'status':       'unread' if is_unread else 'read',
            'google_email': google_email
        })
        saved_count += 1

    return saved_emails, saved_count


def sync_user_emails(user_id, user_record):
    """Syncs all connected Google accounts for a user. Used by both /sync and EventBridge."""
    accounts = user_record.get('google_accounts', [])
    fetch_limit = int(user_record.get('email_fetch_limit', 10))

    all_emails = []
    total_count = 0
    for account in accounts:
        if not account.get('access_token'):
            continue
        emails, count = sync_single_account(user_id, account, fetch_limit)
        all_emails.extend(emails)
        total_count += count

    return all_emails, total_count


def handle_scheduled_sync():
    """Called by EventBridge every 15 minutes. Syncs Gmail for every connected user."""
    print("Starting scheduled sync for all users...")
    users_table = dynamodb.Table('Maily-Users')

    response = users_table.scan()
    users = response.get('Items', [])
    while 'LastEvaluatedKey' in response:
        response = users_table.scan(ExclusiveStartKey=response['LastEvaluatedKey'])
        users.extend(response.get('Items', []))

    success_count = 0
    fail_count = 0
    for user in users:
        user_id = user.get('userId')
        if not user_id or not user.get('google_accounts'):
            continue  # skip users who haven't connected Google
        try:
            _, count = sync_user_emails(user_id, user)
            print(f"Synced {count} emails for user {user_id}")
            success_count += 1
        except Exception as e:
            print(f"Failed to sync user {user_id}: {e}")
            fail_count += 1

    print(f"Scheduled sync complete. Success: {success_count}, Failed: {fail_count}")
    return {
        "statusCode": 200,
        "body": json.dumps({"message": f"Scheduled sync complete. Success: {success_count}, Failed: {fail_count}"})
    }


def handle_sync_emails(event):
    try:
        request_context = event.get('requestContext', {})
        authorizer = request_context.get('authorizer', {})

        user_id = None
        if 'jwt' in authorizer and 'claims' in authorizer['jwt']:
            user_id = authorizer['jwt']['claims'].get('sub')
        elif 'claims' in authorizer:
            user_id = authorizer['claims'].get('sub')

        if not user_id:
            return {"statusCode": 400, "body": json.dumps({"message": "Could not find user ID"})}

        users_table = dynamodb.Table('Maily-Users')
        result = users_table.get_item(Key={'userId': user_id})
        user_record = result.get('Item')

        if not user_record or not user_record.get('google_accounts'):
            return {
                "statusCode": 400,
                "body": json.dumps({"message": "No Google accounts connected. Please connect a Google account in Settings."})
            }

        saved_emails, saved_count = sync_user_emails(user_id, user_record)

        if saved_count == 0:
            return {
                "statusCode": 200,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"message": "No emails found in your Gmail inbox."})
            }

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"message": f"Successfully synced {saved_count} emails!", "emails": saved_emails}, ensure_ascii=False)
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

        total = len(emails)
        unread = sum(1 for e in emails if e.get('status') == 'unread')
        read = total - unread

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

        body = json.loads(event.get('body', '{}'))
        subject = body.get('subject', '(No Subject)')
        summary = body.get('summary', '')
        content = body.get('content', '')

        if not summary and not content:
            return {
                "statusCode": 400,
                "body": json.dumps({"error": "No email content provided to draft a reply for."})
            }

        api_key = get_secrets()['OPENAI_API_KEY']
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

def handle_export(event):
    try:
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
                "statusCode": 404,
                "body": json.dumps({"error": "No emails found to export. Sync your inbox first."})
            }

        export_data = [
            {
                "subject": e.get("subject", ""),
                "from":    e.get("from", ""),
                "status":  e.get("status", ""),
                "summary": e.get("summary", ""),
                "content": e.get("content", "")
            }
            for e in emails
        ]

        s3 = boto3.client('s3')
        bucket = os.environ['EXPORTS_BUCKET_NAME']
        key = f"exports/{user_id}/email-summaries.json"

        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=json.dumps(export_data, indent=2, ensure_ascii=False).encode('utf-8'),
            ContentType='application/json'
        )

        presigned_url = s3.generate_presigned_url(
            'get_object',
            Params={'Bucket': bucket, 'Key': key},
            ExpiresIn=900  
        )

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({
                "message": f"Exported {len(export_data)} emails successfully.",
                "download_url": presigned_url
            })
        }

    except Exception as e:
        print(f"Error during export: {str(e)}")
        return {
            "statusCode": 500,
            "body": json.dumps({"message": "Internal server error during export", "error": str(e)})
        }


def handle_get_accounts(event):
    """Returns the list of connected Google accounts for the logged-in user (emails only, no tokens)."""
    try:
        authorizer = event.get('requestContext', {}).get('authorizer', {})
        user_id = None
        if 'jwt' in authorizer and 'claims' in authorizer['jwt']:
            user_id = authorizer['jwt']['claims'].get('sub')
        elif 'claims' in authorizer:
            user_id = authorizer['claims'].get('sub')

        if not user_id:
            return {"statusCode": 401, "body": json.dumps({"error": "Unauthorized"})}

        users_table = dynamodb.Table('Maily-Users')
        result = users_table.get_item(Key={'userId': user_id})
        accounts = result.get('Item', {}).get('google_accounts', [])

        # Never send tokens to the frontend
        safe_accounts = [{'email': a['email']} for a in accounts if a.get('email')]

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"accounts": safe_accounts})
        }
    except Exception as e:
        print(f"Error getting accounts: {str(e)}")
        return {"statusCode": 500, "body": json.dumps({"error": str(e)})}


def handle_disconnect_google(event):
    """Removes a Google account from the user's list and deletes their associated emails."""
    try:
        authorizer = event.get('requestContext', {}).get('authorizer', {})
        user_id = None
        if 'jwt' in authorizer and 'claims' in authorizer['jwt']:
            user_id = authorizer['jwt']['claims'].get('sub')
        elif 'claims' in authorizer:
            user_id = authorizer['claims'].get('sub')

        if not user_id:
            return {"statusCode": 401, "body": json.dumps({"error": "Unauthorized"})}

        body = json.loads(event.get('body', '{}'))
        google_email = body.get('email')

        if not google_email:
            return {"statusCode": 400, "body": json.dumps({"error": "Missing required field: email"})}

        # Remove account from the list
        users_table = dynamodb.Table('Maily-Users')
        result = users_table.get_item(Key={'userId': user_id})
        accounts = result.get('Item', {}).get('google_accounts', [])
        updated_accounts = [a for a in accounts if a.get('email') != google_email]
        users_table.update_item(
            Key={'userId': user_id},
            UpdateExpression='SET google_accounts = :accounts',
            ExpressionAttributeValues={':accounts': updated_accounts}
        )

        # Delete all emails that belong to this Google account
        all_emails = []
        response = table.query(
            KeyConditionExpression=boto3.dynamodb.conditions.Key('userId').eq(user_id)
        )
        all_emails.extend(response.get('Items', []))
        while 'LastEvaluatedKey' in response:
            response = table.query(
                KeyConditionExpression=boto3.dynamodb.conditions.Key('userId').eq(user_id),
                ExclusiveStartKey=response['LastEvaluatedKey']
            )
            all_emails.extend(response.get('Items', []))

        with table.batch_writer() as batch:
            for email in all_emails:
                if email.get('google_email') == google_email:
                    batch.delete_item(Key={'userId': user_id, 'emailId': email['emailId']})

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"message": f"Disconnected {google_email} and removed their emails."})
        }
    except Exception as e:
        print(f"Error disconnecting account: {str(e)}")
        return {"statusCode": 500, "body": json.dumps({"error": str(e)})}
