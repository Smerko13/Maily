import json
import os
import time
import base64
import urllib.parse
import boto3
import urllib.request
import urllib.error
from datetime import datetime, timezone
from decimal import Decimal
from botocore.exceptions import ClientError

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table('Maily-Emails')

_secrets_cache = None

class DecimalEncoder(json.JSONEncoder):
    """boto3's DynamoDB resource API always returns numeric attributes as Decimal, which json.dumps
    can't serialize by default (e.g. attachments[].size). Whole numbers become int, otherwise float."""
    def default(self, o):
        if isinstance(o, Decimal):
            return int(o) if o % 1 == 0 else float(o)
        return super().default(o)

def get_secrets():
    global _secrets_cache
    if _secrets_cache is None:
        client = boto3.client('secretsmanager')
        secret_name = os.environ['SECRET_NAME']
        response = client.get_secret_value(SecretId=secret_name)
        _secrets_cache = json.loads(response['SecretString'])
    return _secrets_cache

def _update_account_token(user_id, provider, provider_email, new_access_token, expires_in=3600):
    """Persists a refreshed access token back into the matching entry of email_accounts."""
    users_table = dynamodb.Table('Maily-Users')
    result = users_table.get_item(Key={'userId': user_id})
    accounts = result.get('Item', {}).get('email_accounts', [])
    for account in accounts:
        if account.get('email') == provider_email and account.get('provider') == provider:
            account['access_token'] = new_access_token
            account['token_expires_at'] = int(time.time()) + expires_in
            break
    users_table.update_item(
        Key={'userId': user_id},
        UpdateExpression='SET email_accounts = :accounts',
        ExpressionAttributeValues={':accounts': accounts}
    )

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
    _update_account_token(user_id, 'gmail', google_email, new_access_token)
    return new_access_token

def refresh_microsoft_access_token(user_id, outlook_email, refresh_token):
    secrets = get_secrets()
    client_id = secrets['MICROSOFT_CLIENT_ID']
    client_secret = secrets['MICROSOFT_CLIENT_SECRET']

    data = urllib.parse.urlencode({
        'client_id': client_id,
        'client_secret': client_secret,
        'refresh_token': refresh_token,
        'grant_type': 'refresh_token',
        'scope': 'openid profile email offline_access https://graph.microsoft.com/User.Read https://graph.microsoft.com/Mail.Read'
    }).encode('utf-8')

    req = urllib.request.Request('https://login.microsoftonline.com/common/oauth2/v2.0/token', data=data, method='POST')
    req.add_header('Content-Type', 'application/x-www-form-urlencoded')

    with urllib.request.urlopen(req) as resp:
        token_data = json.loads(resp.read().decode('utf-8'))

    new_access_token = token_data['access_token']
    _update_account_token(user_id, 'outlook', outlook_email, new_access_token, token_data.get('expires_in', 3600))
    return new_access_token

def api_get(user_id, account, url):
    """GET url using account's access token, refreshing it once on a 401 via the account's provider.
    Mutates account['access_token'] in place so subsequent calls in the same sync reuse it."""
    def _do(token):
        req = urllib.request.Request(url)
        req.add_header('Authorization', f'Bearer {token}')
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode('utf-8'))

    try:
        return _do(account['access_token'])
    except urllib.error.HTTPError as e:
        refresh_token = account.get('refresh_token')
        if e.code == 401 and refresh_token:
            print(f"Token expired for {account['email']} ({account['provider']}), refreshing...")
            if account['provider'] == 'outlook':
                new_token = refresh_microsoft_access_token(user_id, account['email'], refresh_token)
            else:
                new_token = refresh_google_access_token(user_id, account['email'], refresh_token)
            account['access_token'] = new_token
            return _do(new_token)
        raise

def _b64url_decode(data):
    """Decode Gmail's base64url (no padding) encoding into raw bytes."""
    padded = data + '=' * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded)

def extract_gmail_attachments(payload):
    """Walk a Gmail message payload for parts that are attachments (have a filename + attachmentId)."""
    attachments = []

    def walk(part):
        filename = part.get('filename')
        body = part.get('body', {})
        if filename and body.get('attachmentId'):
            attachments.append({
                'id':       body['attachmentId'],
                'filename': filename,
                'mimeType': part.get('mimeType', 'application/octet-stream'),
                'size':     body.get('size', 0)
            })
        for sub_part in part.get('parts', []):
            walk(sub_part)

    walk(payload)
    return attachments

def extract_gmail_bodies(payload):
    """Walk a Gmail message payload for the first text/plain and text/html parts."""
    text_body = None
    html_body = None

    def walk(part):
        nonlocal text_body, html_body
        mime = part.get('mimeType', '')
        data = part.get('body', {}).get('data')
        if data and mime == 'text/plain' and text_body is None:
            text_body = _b64url_decode(data).decode('utf-8', errors='replace')
        elif data and mime == 'text/html' and html_body is None:
            html_body = _b64url_decode(data).decode('utf-8', errors='replace')
        for sub_part in part.get('parts', []):
            walk(sub_part)

    walk(payload)
    return text_body, html_body

def put_email_item(item):
    """Persists an email item to DynamoDB, excluding bodyText/bodyHtml — the full body is delivered
    directly in the sync API response for that session only and is never stored server-side."""
    persisted = {k: v for k, v in item.items() if k not in ('bodyText', 'bodyHtml')}
    table.put_item(Item=persisted)

def gmail_internal_date_to_iso(internal_date_ms):
    """Gmail's internalDate is milliseconds-since-epoch as a string."""
    if not internal_date_ms:
        return ''
    return datetime.fromtimestamp(int(internal_date_ms) / 1000, tz=timezone.utc).isoformat()

def fetch_outlook_attachment_metadata(user_id, account, message_id):
    """Lists attachment metadata for a Graph message (no bytes downloaded here)."""
    data = api_get(
        user_id, account,
        f'https://graph.microsoft.com/v1.0/me/messages/{message_id}/attachments?$select=id,name,contentType,size'
    )
    return [
        {
            'id':       att['id'],
            'filename': att.get('name', 'attachment'),
            'mimeType': att.get('contentType', 'application/octet-stream'),
            'size':     att.get('size', 0)
        }
        for att in data.get('value', [])
    ]

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

_SUMMARY_BATCH_SIZE = 20  # keeps each OpenAI prompt/response a reasonable size and limits blast radius of one failed call

def summarize_emails_batch(items):
    """Summarizes multiple emails (each {'subject':, 'snippet':}) in as few OpenAI calls as possible,
    chunked into groups of _SUMMARY_BATCH_SIZE. Returns summaries in the same order as items."""
    summaries = []
    for i in range(0, len(items), _SUMMARY_BATCH_SIZE):
        summaries.extend(_summarize_batch_chunk(items[i:i + _SUMMARY_BATCH_SIZE]))
    return summaries

def _summarize_batch_chunk(items):
    if not items:
        return []

    api_key = get_secrets()['OPENAI_API_KEY']

    numbered_emails = "\n\n".join(
        f"Email {i + 1}:\nSubject: {item['subject']}\nContent: {item['snippet']}"
        for i, item in enumerate(items)
    )
    prompt = (
        f"Summarize each of the following {len(items)} emails in 1-2 sentences each.\n\n"
        f"{numbered_emails}\n\n"
        f'Respond with a JSON object of the form {{"summaries": ["summary for email 1", "summary for email 2", ...]}}. '
        f"Return exactly {len(items)} summaries, in the same order as the emails above."
    )

    body = json.dumps({
        "model": "gpt-4.1-nano",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": min(120 * len(items), 4096),
        "response_format": {"type": "json_object"}
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

    raw_content = result['choices'][0]['message']['content'].strip()
    try:
        summaries = json.loads(raw_content).get('summaries', [])
    except (json.JSONDecodeError, AttributeError):
        summaries = []

    # Defensive: the model isn't guaranteed to return exactly len(items) entries
    while len(summaries) < len(items):
        summaries.append('(Summary unavailable)')
    return summaries[:len(items)]


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
    elif http_method == 'DELETE' and path == '/auth/account':
        return handle_disconnect_account(event)
    elif http_method == 'GET' and path == '/stats':
        return handle_get_stats(event)
    elif http_method == 'POST' and path == '/draft':
        return handle_draft_email(event)
    elif http_method == 'POST' and path == '/export':
        return handle_export(event)
    elif http_method == 'POST' and path == '/settings':
        return handle_save_settings(event)
    elif http_method == 'GET' and path == '/email-body':
        return handle_get_email_body(event)
    elif http_method == 'GET' and path == '/attachment':
        return handle_get_attachment(event)
    elif http_method == 'GET' and path == '/thread':
        return handle_get_thread(event)
    elif http_method == 'POST' and path == '/summarize':
        return handle_summarize_email(event)
    elif http_method == 'POST' and path == '/mark-read':
        return handle_mark_read(event)
    else:
        return {
            "statusCode": 404,
            "body": json.dumps({"message": f"Route not found! Method: {http_method}, Path: {path}"})
        }

def handle_save_settings(event):
    try:
        user_id = get_authorized_user_id(event)
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
        user_id = get_authorized_user_id(event)
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
            items = [e for e in items if e.get('providerEmail') == account_filter]

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"message": "Data fetched successfully!", "emails": items}, ensure_ascii=False, cls=DecimalEncoder)
        }
    except Exception as e:
        print(f"Error reading from DynamoDB: {str(e)}")
        return {
            "statusCode": 500,
            "body": json.dumps({"message": "Failed to fetch data.", "error": str(e)})
        }

def sync_single_gmail_account(user_id, account, fetch_limit):
    """Fetch and store emails for one connected Gmail account.
    Emails already present (by emailId) are known to have been processed already \u2014 summarizing is
    the expensive step, so we skip re-fetching details and re-summarizing for anything we've already
    stored, and only touch genuinely new messages. Existing rows (including their status) are left
    exactly as-is; status is user-driven going forward, not re-derived from the provider on every sync."""
    provider_email = account['email']

    list_data = api_get(
        user_id, account,
        f'https://gmail.googleapis.com/gmail/v1/users/me/messages?maxResults={fetch_limit}'
    )
    messages = list_data.get('messages', [])

    if not messages:
        return [], 0

    already_synced = []
    new_message_ids = []
    for msg in messages:
        email_id = f"gmail#{provider_email}#{msg['id']}"  # provider-prefixed, keeps emailId unique across accounts/providers
        existing = table.get_item(Key={'userId': user_id, 'emailId': email_id}).get('Item')
        if existing:
            already_synced.append(existing)
        else:
            new_message_ids.append((msg['id'], email_id))

    # format=full so we can pull attachment metadata from payload.parts.
    # The body content it returns is used only to derive attachments/snippet and then discarded \u2014 not stored.
    pending = []
    for raw_id, email_id in new_message_ids:
        email_data = api_get(
            user_id, account,
            f'https://gmail.googleapis.com/gmail/v1/users/me/messages/{raw_id}?format=full'
        )

        payload = email_data.get('payload', {})
        headers = payload.get('headers', [])
        subject = next((h['value'] for h in headers if h['name'] == 'Subject'), '(No Subject)')
        sender  = next((h['value'] for h in headers if h['name'] == 'From'), '(Unknown Sender)')
        snippet = email_data.get('snippet', '').replace('\u034f', '').strip()
        is_unread = 'UNREAD' in email_data.get('labelIds', [])
        attachments = extract_gmail_attachments(payload)
        in_reply_to = next((h['value'] for h in headers if h['name'] == 'In-Reply-To'), '')
        received_at = gmail_internal_date_to_iso(email_data.get('internalDate'))
        body_text, body_html = extract_gmail_bodies(payload)  # delivered in the response only, never persisted

        pending.append({
            'userId':        user_id,
            'emailId':       email_id,
            'subject':       subject,
            'from':          sender,
            'content':       snippet,
            'status':        'unread' if is_unread else 'read',
            'provider':      'gmail',
            'providerEmail': provider_email,
            'attachments':   attachments,
            'threadId':      email_data.get('threadId', ''),
            'inReplyTo':     in_reply_to,
            'receivedAt':    received_at,
            'bodyText':      body_text,
            'bodyHtml':      body_html
        })

    summaries = summarize_emails_batch([{'subject': p['subject'], 'snippet': p['content']} for p in pending])

    new_emails = []
    for item, summary in zip(pending, summaries):
        item['summary'] = summary
        put_email_item(item)
        new_emails.append(item)

    return already_synced + new_emails, len(new_emails)

def sync_single_outlook_account(user_id, account, fetch_limit):
    """Fetch and store emails for one connected Outlook account via Microsoft Graph.
    Same skip-if-known + batch-summarize approach as the Gmail path — see its docstring."""
    provider_email = account['email']

    list_data = api_get(
        user_id, account,
        f'https://graph.microsoft.com/v1.0/me/messages?$top={fetch_limit}'
        f'&$select=subject,from,bodyPreview,isRead,conversationId,receivedDateTime,hasAttachments,internetMessageHeaders,body'
    )
    messages = list_data.get('value', [])

    if not messages:
        return [], 0

    already_synced = []
    new_messages = []
    for msg in messages:
        email_id = f"outlook#{provider_email}#{msg['id']}"
        existing = table.get_item(Key={'userId': user_id, 'emailId': email_id}).get('Item')
        if existing:
            already_synced.append(existing)
        else:
            new_messages.append((msg, email_id))

    pending = []
    for msg, email_id in new_messages:
        subject = msg.get('subject') or '(No Subject)'
        from_address = msg.get('from', {}).get('emailAddress', {})
        sender = from_address.get('name') or from_address.get('address') or '(Unknown Sender)'
        snippet = (msg.get('bodyPreview') or '').strip()
        is_unread = not msg.get('isRead', True)

        attachments = fetch_outlook_attachment_metadata(user_id, account, msg['id']) if msg.get('hasAttachments') else []

        headers = msg.get('internetMessageHeaders') or []
        in_reply_to = next((h['value'] for h in headers if h.get('name', '').lower() == 'in-reply-to'), '')

        # delivered in the response only, never persisted
        body_field = msg.get('body', {})
        body_content = body_field.get('content')
        body_content_type = body_field.get('contentType', 'text')
        body_text = body_content if body_content_type == 'text' else None
        body_html = body_content if body_content_type == 'html' else None

        pending.append({
            'userId':        user_id,
            'emailId':       email_id,
            'subject':       subject,
            'from':          sender,
            'content':       snippet,
            'status':        'unread' if is_unread else 'read',
            'provider':      'outlook',
            'providerEmail': provider_email,
            'attachments':   attachments,
            'threadId':      msg.get('conversationId', ''),
            'inReplyTo':     in_reply_to,
            'receivedAt':    msg.get('receivedDateTime', ''),
            'bodyText':      body_text,
            'bodyHtml':      body_html
        })

    summaries = summarize_emails_batch([{'subject': p['subject'], 'snippet': p['content']} for p in pending])

    new_emails = []
    for item, summary in zip(pending, summaries):
        item['summary'] = summary
        put_email_item(item)
        new_emails.append(item)

    return already_synced + new_emails, len(new_emails)

def sync_single_account(user_id, account, fetch_limit):
    """Fetch and store emails for one connected email account, dispatching by provider."""
    if account.get('provider') == 'outlook':
        return sync_single_outlook_account(user_id, account, fetch_limit)
    return sync_single_gmail_account(user_id, account, fetch_limit)


def sync_user_emails(user_id, user_record):
    """Syncs all connected email accounts (Gmail + Outlook) for a user. Used by both /sync and EventBridge.
    Returns (all_emails, new_count) — all_emails is everything currently in the fetch window (previously
    synced + newly synced), new_count is how many of those were newly processed this run."""
    accounts = user_record.get('email_accounts', [])
    fetch_limit = int(user_record.get('email_fetch_limit', 10))

    all_emails = []
    new_count = 0
    for account in accounts:
        if not account.get('access_token'):
            continue
        emails, count = sync_single_account(user_id, account, fetch_limit)
        all_emails.extend(emails)
        new_count += count

    return all_emails, new_count


def handle_scheduled_sync():
    """Called by EventBridge every 15 minutes. Syncs Gmail + Outlook for every connected user."""
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
        if not user_id or not user.get('email_accounts'):
            continue  # skip users who haven't connected any email account
        try:
            _, new_count = sync_user_emails(user_id, user)
            print(f"Synced {new_count} new email(s) for user {user_id}")
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
        user_id = get_authorized_user_id(event)
        if not user_id:
            return {"statusCode": 400, "body": json.dumps({"message": "Could not find user ID"})}

        users_table = dynamodb.Table('Maily-Users')
        result = users_table.get_item(Key={'userId': user_id})
        user_record = result.get('Item')

        if not user_record or not user_record.get('email_accounts'):
            return {
                "statusCode": 400,
                "body": json.dumps({"message": "No email accounts connected. Please connect Gmail or Outlook in Settings."})
            }

        all_emails, new_count = sync_user_emails(user_id, user_record)

        if not all_emails:
            return {
                "statusCode": 200,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"message": "No emails found in your inbox."})
            }

        if new_count == 0:
            message = f"You're all caught up — {len(all_emails)} email(s) already synced."
        else:
            message = f"Synced {new_count} new email(s) ({len(all_emails)} total)."

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"message": message, "emails": all_emails}, ensure_ascii=False, cls=DecimalEncoder)
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

def get_authorized_user_id(event):
    authorizer = event.get('requestContext', {}).get('authorizer', {})
    if 'jwt' in authorizer and 'claims' in authorizer['jwt']:
        return authorizer['jwt']['claims'].get('sub')
    elif 'claims' in authorizer:
        return authorizer['claims'].get('sub')
    return None

def find_account(user_id, provider, provider_email):
    users_table = dynamodb.Table('Maily-Users')
    accounts = users_table.get_item(Key={'userId': user_id}).get('Item', {}).get('email_accounts', [])
    return next((a for a in accounts if a.get('email') == provider_email and a.get('provider') == provider), None)

def parse_email_id(email_id):
    """emailId format: <provider>#<providerEmail>#<messageId>. Returns None if malformed."""
    if not email_id or email_id.count('#') != 2:
        return None
    provider, provider_email, message_id = email_id.split('#', 2)
    if provider not in ('gmail', 'outlook'):
        return None
    return provider, provider_email, message_id

def handle_get_email_body(event):
    """Fetches the full body (text + html) of one email directly from the provider. Never stored in DynamoDB."""
    try:
        user_id = get_authorized_user_id(event)
        if not user_id:
            return {"statusCode": 401, "body": json.dumps({"error": "Unauthorized. Could not identify user."})}

        query_params = event.get('queryStringParameters') or {}
        parsed = parse_email_id(query_params.get('emailId'))
        if not parsed:
            return {"statusCode": 400, "body": json.dumps({"error": "Missing or invalid emailId"})}
        provider, provider_email, message_id = parsed

        account = find_account(user_id, provider, provider_email)
        if not account:
            return {"statusCode": 404, "body": json.dumps({"error": f"{provider} account {provider_email} is not connected"})}

        if provider == 'outlook':
            message = api_get(
                user_id, account,
                f'https://graph.microsoft.com/v1.0/me/messages/{message_id}?$select=body'
            )
            body = message.get('body', {})
            content = body.get('content')
            content_type = body.get('contentType', 'text')
            text_body = content if content_type == 'text' else None
            html_body = content if content_type == 'html' else None
        else:
            email_data = api_get(
                user_id, account,
                f'https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}?format=full'
            )
            text_body, html_body = extract_gmail_bodies(email_data.get('payload', {}))

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"emailId": query_params.get('emailId'), "text": text_body, "html": html_body}, ensure_ascii=False)
        }
    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8')
        print(f"Provider API Error: {error_body}")
        return {"statusCode": e.code, "body": json.dumps({"error": "Provider authentication failed", "details": error_body})}
    except Exception as e:
        print(f"Error fetching email body: {str(e)}")
        return {"statusCode": 500, "body": json.dumps({"message": "Internal server error while fetching email body", "error": str(e)})}

def handle_get_attachment(event):
    """Fetches one attachment's bytes from the provider, stages it in S3, and returns a short-lived presigned URL.
    If it's already staged in S3 from a previous request (within the bucket's 1-day lifecycle window),
    skips the provider fetch entirely and just reissues a presigned URL against the existing object."""
    try:
        user_id = get_authorized_user_id(event)
        if not user_id:
            return {"statusCode": 401, "body": json.dumps({"error": "Unauthorized. Could not identify user."})}

        query_params = event.get('queryStringParameters') or {}
        email_id = query_params.get('emailId')
        attachment_id = query_params.get('attachmentId')
        parsed = parse_email_id(email_id)
        if not parsed or not attachment_id:
            return {"statusCode": 400, "body": json.dumps({"error": "Missing or invalid emailId/attachmentId"})}
        provider, provider_email, message_id = parsed

        account = find_account(user_id, provider, provider_email)
        if not account:
            return {"statusCode": 404, "body": json.dumps({"error": f"{provider} account {provider_email} is not connected"})}

        email_item = table.get_item(Key={'userId': user_id, 'emailId': email_id}).get('Item', {})
        attachment_meta = next((a for a in email_item.get('attachments', []) if a.get('id') == attachment_id), None)
        filename = attachment_meta['filename'] if attachment_meta else attachment_id
        content_type = attachment_meta['mimeType'] if attachment_meta else 'application/octet-stream'

        s3 = boto3.client('s3')
        bucket = os.environ['EXPORTS_BUCKET_NAME']
        key = f"attachments/{user_id}/{email_id}/{attachment_id}-{filename}"

        try:
            s3.head_object(Bucket=bucket, Key=key)
            already_staged = True
        except ClientError as e:
            if e.response.get('Error', {}).get('Code') == '404':
                already_staged = False
            else:
                raise

        if not already_staged:
            if provider == 'outlook':
                attachment_data = api_get(
                    user_id, account,
                    f'https://graph.microsoft.com/v1.0/me/messages/{message_id}/attachments/{attachment_id}'
                )
                file_bytes = base64.b64decode(attachment_data['contentBytes'])
            else:
                attachment_data = api_get(
                    user_id, account,
                    f'https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}/attachments/{attachment_id}'
                )
                file_bytes = _b64url_decode(attachment_data['data'])

            s3.put_object(Bucket=bucket, Key=key, Body=file_bytes, ContentType=content_type)

        presigned_url = s3.generate_presigned_url(
            'get_object',
            Params={
                'Bucket': bucket,
                'Key': key,
                'ResponseContentDisposition': f'attachment; filename="{filename}"'
            },
            ExpiresIn=900
        )

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"download_url": presigned_url, "filename": filename})
        }
    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8')
        print(f"Provider API Error: {error_body}")
        return {"statusCode": e.code, "body": json.dumps({"error": "Provider authentication failed", "details": error_body})}
    except Exception as e:
        print(f"Error fetching attachment: {str(e)}")
        return {"statusCode": 500, "body": json.dumps({"message": "Internal server error while fetching attachment", "error": str(e)})}

def handle_get_thread(event):
    """Returns all emails sharing a threadId for the logged-in user, ordered oldest-first."""
    try:
        user_id = get_authorized_user_id(event)
        if not user_id:
            return {"statusCode": 401, "body": json.dumps({"error": "Unauthorized. Could not identify user."})}

        query_params = event.get('queryStringParameters') or {}
        thread_id = query_params.get('threadId')
        if not thread_id:
            return {"statusCode": 400, "body": json.dumps({"error": "Missing required query param: threadId"})}

        key_condition = boto3.dynamodb.conditions.Key('userId').eq(user_id) & boto3.dynamodb.conditions.Key('threadId').eq(thread_id)

        items = []
        response = table.query(IndexName='threadId-index', KeyConditionExpression=key_condition)
        items.extend(response.get('Items', []))
        while 'LastEvaluatedKey' in response:
            response = table.query(
                IndexName='threadId-index',
                KeyConditionExpression=key_condition,
                ExclusiveStartKey=response['LastEvaluatedKey']
            )
            items.extend(response.get('Items', []))

        items.sort(key=lambda e: e.get('receivedAt', ''))

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"threadId": thread_id, "emails": items}, ensure_ascii=False, cls=DecimalEncoder)
        }
    except Exception as e:
        print(f"Error fetching thread: {str(e)}")
        return {"statusCode": 500, "body": json.dumps({"message": "Internal server error while fetching thread", "error": str(e)})}

def handle_summarize_email(event):
    """Re-generates and persists the AI summary for one already-synced email. Sync itself skips
    re-summarizing known emails for efficiency — this is the manual escape hatch for when it's needed anyway."""
    try:
        user_id = get_authorized_user_id(event)
        if not user_id:
            return {"statusCode": 401, "body": json.dumps({"error": "Unauthorized. Could not identify user."})}

        body = json.loads(event.get('body', '{}'))
        email_id = body.get('emailId')
        if not email_id:
            return {"statusCode": 400, "body": json.dumps({"error": "Missing required field: emailId"})}

        email_item = table.get_item(Key={'userId': user_id, 'emailId': email_id}).get('Item')
        if not email_item:
            return {"statusCode": 404, "body": json.dumps({"error": "Email not found"})}

        summary = summarize_email(email_item.get('subject', ''), email_item.get('content', ''))

        table.update_item(
            Key={'userId': user_id, 'emailId': email_id},
            UpdateExpression='SET summary = :s',
            ExpressionAttributeValues={':s': summary}
        )

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"emailId": email_id, "summary": summary}, ensure_ascii=False)
        }
    except Exception as e:
        print(f"Error summarizing email: {str(e)}")
        return {"statusCode": 500, "body": json.dumps({"message": "Internal server error while summarizing email", "error": str(e)})}

def handle_mark_read(event):
    """Marks one email as read in our own stored status. This only updates Maily's record — it does
    not call back to Gmail/Graph to mark it read on the provider's mailbox."""
    try:
        user_id = get_authorized_user_id(event)
        if not user_id:
            return {"statusCode": 401, "body": json.dumps({"error": "Unauthorized. Could not identify user."})}

        body = json.loads(event.get('body', '{}'))
        email_id = body.get('emailId')
        if not email_id:
            return {"statusCode": 400, "body": json.dumps({"error": "Missing required field: emailId"})}

        table.update_item(
            Key={'userId': user_id, 'emailId': email_id},
            UpdateExpression='SET #s = :r',
            ExpressionAttributeNames={'#s': 'status'},
            ExpressionAttributeValues={':r': 'read'}
        )

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"emailId": email_id, "status": "read"})
        }
    except Exception as e:
        print(f"Error marking email as read: {str(e)}")
        return {"statusCode": 500, "body": json.dumps({"message": "Internal server error while marking email as read", "error": str(e)})}

def handle_get_stats(event):
    try:
        user_id = get_authorized_user_id(event)
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
        user_id = get_authorized_user_id(event)
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
        user_id = get_authorized_user_id(event)
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
    """Returns the list of connected email accounts (Gmail + Outlook) for the logged-in user (no tokens)."""
    try:
        user_id = get_authorized_user_id(event)
        if not user_id:
            return {"statusCode": 401, "body": json.dumps({"error": "Unauthorized"})}

        users_table = dynamodb.Table('Maily-Users')
        result = users_table.get_item(Key={'userId': user_id})
        accounts = result.get('Item', {}).get('email_accounts', [])

        # Never send tokens to the frontend
        safe_accounts = [{'email': a['email'], 'provider': a.get('provider', 'gmail')} for a in accounts if a.get('email')]

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"accounts": safe_accounts})
        }
    except Exception as e:
        print(f"Error getting accounts: {str(e)}")
        return {"statusCode": 500, "body": json.dumps({"error": str(e)})}


def handle_disconnect_account(event):
    """Removes an email account (Gmail or Outlook) from the user's list and deletes their associated emails."""
    try:
        user_id = get_authorized_user_id(event)
        if not user_id:
            return {"statusCode": 401, "body": json.dumps({"error": "Unauthorized"})}

        body = json.loads(event.get('body', '{}'))
        email = body.get('email')
        provider = body.get('provider')

        if not email or not provider:
            return {"statusCode": 400, "body": json.dumps({"error": "Missing required fields: email, provider"})}

        # Remove account from the list
        users_table = dynamodb.Table('Maily-Users')
        result = users_table.get_item(Key={'userId': user_id})
        accounts = result.get('Item', {}).get('email_accounts', [])
        updated_accounts = [a for a in accounts if not (a.get('email') == email and a.get('provider') == provider)]
        users_table.update_item(
            Key={'userId': user_id},
            UpdateExpression='SET email_accounts = :accounts',
            ExpressionAttributeValues={':accounts': updated_accounts}
        )

        # Delete all emails that belong to this account
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
            for item in all_emails:
                if item.get('providerEmail') == email and item.get('provider') == provider:
                    batch.delete_item(Key={'userId': user_id, 'emailId': item['emailId']})

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"message": f"Disconnected {email} and removed their emails."})
        }
    except Exception as e:
        print(f"Error disconnecting account: {str(e)}")
        return {"statusCode": 500, "body": json.dumps({"error": str(e)})}
