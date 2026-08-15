import json
import os
import time
import uuid
import base64
import urllib.parse
import boto3
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr
from botocore.exceptions import ClientError

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table('Maily-Emails')
labels_table = dynamodb.Table('Maily-Labels')
category_items_table = dynamodb.Table('Maily-CategoryItems')

_secrets_cache = None

PRESET_LABELS = [
    {"id": "work",        "name": "Work",        "description": "Emails related to the user's job, colleagues, meetings, work projects, or professional communication.", "color": "#6366f1"},
    {"id": "finance",     "name": "Finance",     "description": "Emails about banking, bills, invoices, payments, statements, or financial accounts.", "color": "#10b981"},
    {"id": "shopping",    "name": "Shopping",    "description": "Emails about online purchases, order confirmations, or promotions from retailers. An email can be both Shopping and, separately, tracked as a Delivery if it also has shipment/tracking/order-status content.", "color": "#f59e0b"},
    {"id": "travel",      "name": "Travel",      "description": "Emails about flight, hotel, or rental bookings, itineraries, or travel confirmations.", "color": "#0ea5e9"},
    {"id": "social",      "name": "Social",      "description": "Emails from social networks, event invitations, or personal correspondence with friends/family.", "color": "#f43f5e"},
    {"id": "newsletters", "name": "Newsletters", "description": "Recurring newsletters, digests, or subscription content the user opted into.", "color": "#a855f7"},
    {"id": "urgent",      "name": "Urgent",      "description": "Emails that require prompt action or a response, such as deadlines, alerts, or time-sensitive requests.", "color": "#ef4444"},
    {"id": "receipts",    "name": "Receipts",    "description": "Order receipts, payment confirmations, or proof-of-purchase emails.", "color": "#64748b"},
]

# Every category type declares its structured fields, a matching key for deduplication, and generic
# rules (completionRule/atRiskRule) that drive the "done" / "at risk" badges without any per-category
# UI or engine code — adding a new category type later is purely additive to this dict.
CATEGORY_TYPES = {
    "delivery": {
        "label": "Delivery",
        "icon": "\U0001F4E6",
        "classifierDescription": (
            "emails about a package/order shipment: purchase or shipping confirmations, tracking/carrier "
            "updates, out-for-delivery or delivered notices, delay or delivery-exception notices, and also "
            "'your order will be auto-completed/closed soon' or 'please confirm you received your order' / "
            "'apply for a return or refund' notices — these last ones imply the package has likely already "
            "arrived and should still be treated as a delivery update"
        ),
        "fields": [
            {"key": "orderNumber",       "label": "Order Number",       "type": "string",
             "hint": "The merchant's order/confirmation number referenced across this order's emails (e.g. "
                     "'Order 1121596074575068'). Distinct from a carrier tracking number, but it's often the "
                     "only identifier present in every email about this order (including early/late-lifecycle "
                     "emails that don't mention a tracking number) — extract it whenever present."},
            {"key": "trackingNumber",    "label": "Tracking Number",    "type": "string"},
            {"key": "carrier",           "label": "Carrier",            "type": "string"},
            {"key": "status",            "label": "Status",             "type": "enum",
             "values": ["ordered", "shipped", "out_for_delivery", "delivered", "delayed", "exception"],
             "hint": "If the email says the order will be auto-completed/closed soon, asks you to confirm "
                     "receipt, or invites you to apply for a return/refund if the package is missing or wrong, "
                     "treat that as status=delivered — the package has very likely already arrived."},
            {"key": "estimatedDelivery", "label": "Estimated Delivery", "type": "date"},
            {"key": "actualDelivery",    "label": "Actual Delivery",    "type": "date"},
            {"key": "merchant",          "label": "Merchant",           "type": "string"},
            {"key": "orderDescription",  "label": "Order Description",  "type": "string"},
        ],
        # orderNumber checked first: it's the identifier most likely to be present across an order's whole
        # lifecycle, whereas trackingNumber may be absent from early ("ordered") and late ("awaiting your
        # confirmation") emails that don't mention a carrier at all.
        "matchKeys": ["orderNumber", "trackingNumber"],
        "titleTemplate": "{merchant} — {orderDescription}",
        "primaryDateField": "estimatedDelivery",
        "completionRule": {"type": "field_equals", "field": "status", "values": ["delivered"]},
        "atRiskRule": {"type": "date_passed_without", "dateField": "estimatedDelivery",
                       "field": "status", "values": ["delivered"]},
    },
}

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
        'scope': 'openid profile email offline_access https://graph.microsoft.com/User.Read https://graph.microsoft.com/Mail.Read https://graph.microsoft.com/Mail.Send'
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

def api_request(user_id, account, url, method='POST', payload=None):
    """Call a provider API with JSON, refreshing the account token once after a 401."""
    def _do(token):
        data = json.dumps(payload).encode('utf-8') if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header('Authorization', f'Bearer {token}')
        if payload is not None:
            req.add_header('Content-Type', 'application/json')
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            return json.loads(raw.decode('utf-8')) if raw else None

    try:
        return _do(account['access_token'])
    except urllib.error.HTTPError as e:
        refresh_token = account.get('refresh_token')
        if e.code == 401 and refresh_token:
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

def get_label_catalog(user_id):
    """This user's full label set: app-wide presets plus their own custom labels."""
    custom = labels_table.query(
        KeyConditionExpression=boto3.dynamodb.conditions.Key('userId').eq(user_id)
    ).get('Items', [])
    return PRESET_LABELS + [
        {"id": c['labelId'], "name": c.get('name', ''), "description": c.get('description', ''), "color": c.get('color', '#94a3b8')}
        for c in custom
    ]

# Structural/system Gmail labels that don't belong in a "labels applied to this email" badge list —
# UNREAD/status is already tracked separately via the `status` field.
_GMAIL_STRUCTURAL_LABELS = {'INBOX', 'UNREAD', 'SENT', 'DRAFT', 'TRASH', 'SPAM', 'CHAT'}

def get_gmail_label_names(user_id, account):
    """Resolves opaque Gmail Label_xxx ids to their human-readable names. One call per account per
    sync, only made lazily (see derive_gmail_provider_labels) since most emails only carry system labels."""
    data = api_get(user_id, account, 'https://gmail.googleapis.com/gmail/v1/users/me/labels')
    return {l['id']: l['name'] for l in data.get('labels', [])}

def derive_gmail_provider_labels(label_ids, user_id, account, label_name_cache):
    """Turns a Gmail message's raw labelIds into human-readable provider label badges."""
    result = []
    needs_resolution = False
    for label_id in label_ids:
        if label_id in _GMAIL_STRUCTURAL_LABELS:
            continue
        if label_id in ('IMPORTANT', 'STARRED'):
            result.append(label_id.title())
        elif label_id.startswith('CATEGORY_'):
            result.append(label_id[len('CATEGORY_'):].replace('_', ' ').title())
        else:
            needs_resolution = True

    if needs_resolution:
        if label_name_cache.get('map') is None:
            label_name_cache['map'] = get_gmail_label_names(user_id, account)
        for label_id in label_ids:
            if label_id in _GMAIL_STRUCTURAL_LABELS or label_id in ('IMPORTANT', 'STARRED') or label_id.startswith('CATEGORY_'):
                continue
            name = label_name_cache['map'].get(label_id)
            if name:
                result.append(name)

    return result

def _rule_field_equals(fields, rule):
    return fields.get(rule['field']) in rule['values']

def _rule_date_passed_without(fields, rule):
    """True when dateField is in the past and `field` hasn't reached one of `values` yet."""
    date_str = fields.get(rule['dateField'])
    if not date_str:
        return False
    if fields.get(rule['field']) in rule['values']:
        return False
    try:
        target = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return False
    return target < datetime.now(timezone.utc)

def evaluate_category_rule(rule, fields):
    if not rule:
        return False
    if rule['type'] == 'field_equals':
        return _rule_field_equals(fields, rule)
    if rule['type'] == 'date_passed_without':
        return _rule_date_passed_without(fields, rule)
    return False

def evaluate_category_item_state(category_type, fields):
    """Returns (isComplete, isAtRisk) for a tracked category item, driven entirely by that
    category type's completionRule/atRiskRule — no per-category-type logic needed here."""
    schema = CATEGORY_TYPES.get(category_type, {})
    is_complete = evaluate_category_rule(schema.get('completionRule'), fields)
    is_at_risk = (not is_complete) and evaluate_category_rule(schema.get('atRiskRule'), fields)
    return is_complete, is_at_risk

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

def classify_emails_batch(items, label_catalog):
    """Summarizes + assigns labels + flags a smart-category type for multiple emails (each
    {'subject':, 'snippet':}) in as few OpenAI calls as possible, chunked into groups of
    _SUMMARY_BATCH_SIZE. Returns a list of {'summary','labels','smartCategory'} in the same order as items.
    Combined into one call (rather than a separate call per concern) since the dominant cost is the
    email content itself, already being sent for summarization."""
    results = []
    for i in range(0, len(items), _SUMMARY_BATCH_SIZE):
        results.extend(_classify_batch_chunk(items[i:i + _SUMMARY_BATCH_SIZE], label_catalog))
    return results

def _classify_batch_chunk(items, label_catalog):
    if not items:
        return []

    api_key = get_secrets()['OPENAI_API_KEY']

    numbered_emails = "\n\n".join(
        f"Email {i + 1}:\nSubject: {item['subject']}\nContent: {item['snippet']}"
        for i, item in enumerate(items)
    )
    label_lines = "\n".join(f"- {l['id']}: {l['description']}" for l in label_catalog)
    category_lines = "\n".join(f"- {key}: {schema['classifierDescription']}" for key, schema in CATEGORY_TYPES.items())

    prompt = (
        f"You are classifying and summarizing a batch of {len(items)} emails.\n\n"
        f"Available labels (assign zero or more that clearly apply):\n{label_lines}\n\n"
        f"Available smart categories (assign at most one that clearly applies, or null if none do):\n{category_lines}\n\n"
        f"Labels and the smart category are independent judgments, not alternatives to each other — an "
        f"email can both get a label AND be assigned a smart category (e.g. a purchase-related email can "
        f"be labeled 'Shopping' while also being a 'delivery' smart category update). Decide each "
        f"separately; do not treat assigning one as a reason to skip the other.\n\n"
        f"{numbered_emails}\n\n"
        f'Respond with a JSON object of the form {{"results": [{{"summary": "1-2 sentence summary", '
        f'"labels": ["label_id", ...], "smartCategory": "category_key_or_null"}}, ...]}}. '
        f"Return exactly {len(items)} results, in the same order as the emails above."
    )

    body = json.dumps({
        "model": "gpt-4.1-nano",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": min(180 * len(items), 4096),
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
        results = json.loads(raw_content).get('results', [])
    except (json.JSONDecodeError, AttributeError):
        results = []

    valid_label_ids = {l['id'] for l in label_catalog}

    # Defensive: the model isn't guaranteed to return exactly len(items) well-formed entries
    while len(results) < len(items):
        results.append({})
    results = results[:len(items)]

    normalized = []
    for r in results:
        if not isinstance(r, dict):
            r = {}
        summary = r.get('summary') or '(Summary unavailable)'
        labels = [l for l in (r.get('labels') or []) if l in valid_label_ids]
        smart_category = r.get('smartCategory')
        if smart_category not in CATEGORY_TYPES:
            smart_category = None
        normalized.append({'summary': summary, 'labels': labels, 'smartCategory': smart_category})
    return normalized

_EXTRACTION_BATCH_SIZE = 20

def extract_category_fields_batch(items, category_type):
    """Extracts a smart category's structured fields (e.g. tracking number, carrier, status for
    "delivery") from each email, chunked the same way as classify_emails_batch. Only ever called on
    the subset of a sync batch already flagged with this category_type — not every email."""
    results = []
    for i in range(0, len(items), _EXTRACTION_BATCH_SIZE):
        results.extend(_extract_category_fields_chunk(items[i:i + _EXTRACTION_BATCH_SIZE], category_type))
    return results

def _extract_category_fields_chunk(items, category_type):
    if not items:
        return []

    schema = CATEGORY_TYPES[category_type]
    fields = schema['fields']
    field_keys = [f['key'] for f in fields]
    api_key = get_secrets()['OPENAI_API_KEY']

    field_desc = ", ".join(
        f"{f['key']} ({f['type']}" + (f", one of: {', '.join(f['values'])}" if f['type'] == 'enum' else '') + ")"
        + (f" [{f['hint']}]" if f.get('hint') else '')
        for f in fields
    )
    numbered_emails = "\n\n".join(
        f"Email {i + 1}:\nSubject: {item['subject']}\nContent: {item['snippet']}"
        for i, item in enumerate(items)
    )
    example_obj = "{" + ", ".join(f'"{k}": "..."' for k in field_keys) + "}"
    prompt = (
        f"Extract these fields from each of the following {len(items)} emails about a {schema['label'].lower()}: "
        f"{field_desc}. Use null for anything not present in the email.\n\n"
        f"{numbered_emails}\n\n"
        f'Respond with a JSON object of the form {{"results": [{example_obj}, ...]}}. '
        f"Return exactly {len(items)} results, in the same order as the emails above."
    )

    body = json.dumps({
        "model": "gpt-4.1-nano",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": min(150 * len(items), 4096),
        "response_format": {"type": "json_object"}
    }).encode('utf-8')

    req = urllib.request.Request('https://api.openai.com/v1/chat/completions', data=body, method='POST')
    req.add_header('Authorization', f'Bearer {api_key}')
    req.add_header('Content-Type', 'application/json')

    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read().decode('utf-8'))

    raw_content = result['choices'][0]['message']['content'].strip()
    try:
        results = json.loads(raw_content).get('results', [])
    except (json.JSONDecodeError, AttributeError):
        results = []

    while len(results) < len(items):
        results.append({})
    results = results[:len(items)]

    # Drop hallucinated/invalid enum values (e.g. a status the model invented outside the allowed set)
    # rather than storing garbage that can never satisfy a category type's completionRule/atRiskRule.
    enum_allowed = {f['key']: set(f['values']) for f in fields if f.get('type') == 'enum'}
    normalized = []
    for r in results:
        row = {}
        if isinstance(r, dict):
            for k in field_keys:
                v = r.get(k)
                if not v:
                    continue
                if k in enum_allowed and v not in enum_allowed[k]:
                    continue
                row[k] = v
        normalized.append(row)
    return normalized

def ai_match_category_item(extracted, summary, existing_items, category_type):
    """Small, non-batched fallback match call — only used when deterministic matchKeys don't apply
    (e.g. an order-confirmation email with no tracking number yet), and only when the user has at
    least one existing open item of this category type. Returns the matched item dict, or None."""
    schema = CATEGORY_TYPES[category_type]
    api_key = get_secrets()['OPENAI_API_KEY']

    candidate_lines = "\n".join(
        f"- {e['itemId']}: {json.dumps(e.get('fields', {}), default=str)}"
        for e in existing_items
    )
    prompt = (
        f"A new email about a {schema['label'].lower()} was just processed with this extracted data: "
        f"{json.dumps(extracted, default=str)} (summary: {summary}).\n\n"
        f"Here are the user's existing open {schema['label'].lower()} items:\n{candidate_lines}\n\n"
        f"Does this new email update one of these existing items, or none of them? "
        f'Respond with JSON: {{"matchId": "<itemId>" or null}}.'
    )

    body = json.dumps({
        "model": "gpt-4.1-nano",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 60,
        "response_format": {"type": "json_object"}
    }).encode('utf-8')

    req = urllib.request.Request('https://api.openai.com/v1/chat/completions', data=body, method='POST')
    req.add_header('Authorization', f'Bearer {api_key}')
    req.add_header('Content-Type', 'application/json')

    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read().decode('utf-8'))

    raw_content = result['choices'][0]['message']['content'].strip()
    try:
        match_id = json.loads(raw_content).get('matchId')
    except (json.JSONDecodeError, AttributeError):
        match_id = None

    return next((e for e in existing_items if e['itemId'] == match_id), None)


def lambda_handler(event, context):
    # Detect EventBridge scheduled event (not an HTTP request)
    if event.get('source') == 'aws.events' or event.get('detail-type') == 'Scheduled Event':
        print("Received scheduled sync event")
        return handle_scheduled_sync()

    request_context = event.get('requestContext', {})
    http_method = event.get('httpMethod') or request_context.get('http', {}).get('method', '')
    path = event.get('path') or event.get('rawPath', '')
    print("Received HTTP request:", json.dumps({
        "requestId": request_context.get('requestId'),
        "method": http_method,
        "path": path,
    }))

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
    elif http_method == 'POST' and path == '/send':
        return handle_send_email(event)
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
    elif http_method == 'GET' and path == '/labels':
        return handle_list_labels(event)
    elif http_method == 'POST' and path == '/labels':
        return handle_create_label(event)
    elif http_method == 'PUT' and path == '/labels':
        return handle_update_label(event)
    elif http_method == 'DELETE' and path == '/labels':
        return handle_delete_label(event)
    elif http_method == 'GET' and path == '/smart-categories':
        return handle_list_category_items(event)
    elif http_method == 'GET' and path == '/smart-category':
        return handle_get_category_item(event)
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
        has_limit = 'email_fetch_limit' in body
        has_signature = 'signature' in body
        if not has_limit and not has_signature:
            return {
                "statusCode": 400,
                "body": json.dumps({"error": "No supported settings were provided"})
            }

        updates = []
        values = {}
        if has_limit:
            try:
                limit = int(body.get('email_fetch_limit'))
            except (ValueError, TypeError):
                return {"statusCode": 400, "body": json.dumps({"error": "email_fetch_limit must be an integer"})}
            if not (1 <= limit <= 100):
                return {"statusCode": 400, "body": json.dumps({"error": "email_fetch_limit must be between 1 and 100"})}
            updates.append('email_fetch_limit = :limit')
            values[':limit'] = limit

        if has_signature:
            signature = str(body.get('signature') or '').strip()
            if len(signature) > 2000:
                return {"statusCode": 400, "body": json.dumps({"error": "Signature must be 2000 characters or less"})}
            updates.append('signature = :signature')
            values[':signature'] = signature

        users_table = dynamodb.Table('Maily-Users')
        users_table.update_item(
            Key={'userId': user_id},
            UpdateExpression=f"SET {', '.join(updates)}",
            ExpressionAttributeValues=values
        )

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"message": "Settings saved."})
        }

    except Exception as e:
        print(f"Error saving settings: {str(e)}")
        return {
            "statusCode": 500,
            "body": json.dumps({"message": "Internal server error while saving settings"})
        }

def get_user_emails(user_id, account_filter=None, label_filter=None):
    """Queries every stored email for a user (one paginated Query on the userId partition key —
    cheap regardless of item count, unlike N individual GetItems). Shared by the /hello listing and
    the /sync response, which both need "the user's current full inbox" as opposed to just what
    changed in a given sync."""
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
    if label_filter:
        items = [e for e in items if label_filter in (e.get('labels') or [])]

    return items

def handle_get_emails(event):
    try:
        user_id = get_authorized_user_id(event)
        if not user_id:
            return {
                "statusCode": 401,
                "body": json.dumps({"error": "Unauthorized. Could not identify user."})
            }

        # Optional filters: ?account=user@gmail.com, ?label=work
        query_params = event.get('queryStringParameters') or {}
        items = get_user_emails(user_id, query_params.get('account'), query_params.get('label'))

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"message": "Data fetched successfully!", "emails": items}, ensure_ascii=False, cls=DecimalEncoder)
        }
    except Exception as e:
        print(f"Error reading from DynamoDB: {str(e)}")
        return {
            "statusCode": 500,
            "body": json.dumps({"message": "Failed to fetch data."})
        }

def sync_single_gmail_account(user_id, account, fetch_limit, label_catalog):
    """Fetch and store new emails for one connected Gmail account.
    Instead of checking every fetched message against DynamoDB, we keep a per-account watermark
    (last_synced_message_id/last_synced_at, mutated in place on `account` \u2014 sync_user_emails persists
    it) and ask Gmail only for messages after it. The result is already newest-first, so we just walk
    it until we hit the watermark id and stop; everything above that point is new. Existing rows are
    never re-touched \u2014 status is user-driven going forward, not re-derived from the provider."""
    provider_email = account['email']
    last_synced_at = account.get('last_synced_at')
    last_synced_message_id = account.get('last_synced_message_id')

    list_url = f'https://gmail.googleapis.com/gmail/v1/users/me/messages?maxResults={fetch_limit}'
    if last_synced_at:
        # Gmail's after: operator is only documented to guarantee day-level precision, so buffer back
        # a full day to make sure the watermark message is still included in the results \u2014 the actual
        # cutoff is enforced below by matching last_synced_message_id, not by this filter.
        buffered_ts = int(datetime.fromisoformat(last_synced_at).timestamp()) - 86400
        list_url += f'&q=after:{buffered_ts}'

    list_data = api_get(user_id, account, list_url)
    messages = list_data.get('messages', [])

    if not messages:
        return [], 0

    new_message_ids = []
    for msg in messages:
        if msg['id'] == last_synced_message_id:
            break
        new_message_ids.append(msg['id'])

    if not new_message_ids:
        return [], 0

    # format=full so we can pull attachment metadata from payload.parts.
    # The body content it returns is used only to derive attachments/snippet and then discarded \u2014 not stored.
    label_name_cache = {'map': None}  # lazily resolved, shared across this account's messages
    pending = []
    newest_received_at = None
    for idx, raw_id in enumerate(new_message_ids):
        email_id = f"gmail#{provider_email}#{raw_id}"  # provider-prefixed, keeps emailId unique across accounts/providers
        email_data = api_get(
            user_id, account,
            f'https://gmail.googleapis.com/gmail/v1/users/me/messages/{raw_id}?format=full'
        )

        payload = email_data.get('payload', {})
        headers = payload.get('headers', [])
        subject = next((h['value'] for h in headers if h['name'] == 'Subject'), '(No Subject)')
        sender  = next((h['value'] for h in headers if h['name'] == 'From'), '(Unknown Sender)')
        from_address = parseaddr(sender)[1]
        to_addresses = [address for _, address in getaddresses([
            next((h['value'] for h in headers if h['name'].lower() == 'to'), '')
        ]) if address]
        cc_addresses = [address for _, address in getaddresses([
            next((h['value'] for h in headers if h['name'].lower() == 'cc'), '')
        ]) if address]
        snippet = email_data.get('snippet', '').replace('\u034f', '').strip()
        label_ids = email_data.get('labelIds', [])
        is_unread = 'UNREAD' in label_ids
        provider_labels = derive_gmail_provider_labels(label_ids, user_id, account, label_name_cache)
        attachments = extract_gmail_attachments(payload)
        in_reply_to = next((h['value'] for h in headers if h['name'] == 'In-Reply-To'), '')
        message_id = next((h['value'] for h in headers if h['name'].lower() == 'message-id'), '')
        received_at = gmail_internal_date_to_iso(email_data.get('internalDate'))
        if idx == 0:
            newest_received_at = received_at  # new_message_ids[0] is always the newest \u2014 messages is newest-first
        body_text, body_html = extract_gmail_bodies(payload)  # delivered in the response only, never persisted

        pending.append({
            'userId':        user_id,
            'emailId':       email_id,
            'subject':       subject,
            'from':          sender,
            'fromAddress':   from_address,
            'to':            to_addresses,
            'cc':            cc_addresses,
            'content':       snippet,
            'status':        'unread' if is_unread else 'read',
            'provider':      'gmail',
            'providerEmail': provider_email,
            'providerLabels': provider_labels,
            'attachments':   attachments,
            'threadId':      email_data.get('threadId', ''),
            'inReplyTo':     in_reply_to,
            'messageId':     message_id,
            'receivedAt':    received_at,
            'bodyText':      body_text,
            'bodyHtml':      body_html
        })

    new_emails = finalize_batch(user_id, pending, label_catalog)
    if new_emails:
        account['last_synced_message_id'] = new_message_ids[0]
        account['last_synced_at'] = newest_received_at
    return new_emails, len(new_emails)

def sync_single_outlook_account(user_id, account, fetch_limit, label_catalog):
    """Fetch and store new emails for one connected Outlook account via Microsoft Graph.
    Same watermark approach as the Gmail path — see its docstring. Graph's receivedDateTime filter is
    reliably second-precise (unlike Gmail's day-level after:), but we still buffer it and confirm the
    cutoff by matching last_synced_message_id, for the same clock-skew/edge-case safety margin."""
    provider_email = account['email']
    last_synced_at = account.get('last_synced_at')
    last_synced_message_id = account.get('last_synced_message_id')

    list_url = (
        f'https://graph.microsoft.com/v1.0/me/messages?$top={fetch_limit}'
        f'&$orderby=receivedDateTime%20desc'
        f'&$select=subject,from,toRecipients,ccRecipients,replyTo,internetMessageId,bodyPreview,isRead,conversationId,receivedDateTime,hasAttachments,internetMessageHeaders,body,categories'
    )
    if last_synced_at:
        # urllib.request rejects literal spaces in URLs ("URL can't contain control characters"),
        # so the OData filter's spaces have to be percent-encoded rather than written literally.
        buffered = (datetime.fromisoformat(last_synced_at) - timedelta(minutes=1)).strftime('%Y-%m-%dT%H:%M:%SZ')
        list_url += f'&$filter=receivedDateTime%20ge%20{buffered}'

    list_data = api_get(user_id, account, list_url)
    messages = list_data.get('value', [])

    if not messages:
        return [], 0

    new_messages = []
    for msg in messages:
        if msg['id'] == last_synced_message_id:
            break
        new_messages.append(msg)

    if not new_messages:
        return [], 0

    pending = []
    for msg in new_messages:
        email_id = f"outlook#{provider_email}#{msg['id']}"
        subject = msg.get('subject') or '(No Subject)'
        from_address = msg.get('from', {}).get('emailAddress', {})
        sender = from_address.get('name') or from_address.get('address') or '(Unknown Sender)'
        to_addresses = [recipient.get('emailAddress', {}).get('address') for recipient in msg.get('toRecipients', [])]
        cc_addresses = [recipient.get('emailAddress', {}).get('address') for recipient in msg.get('ccRecipients', [])]
        snippet = (msg.get('bodyPreview') or '').strip()
        is_unread = not msg.get('isRead', True)
        provider_labels = msg.get('categories') or []  # Graph returns ready-to-use category strings

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
            'fromAddress':   from_address.get('address', ''),
            'to':            [address for address in to_addresses if address],
            'cc':            [address for address in cc_addresses if address],
            'content':       snippet,
            'status':        'unread' if is_unread else 'read',
            'provider':      'outlook',
            'providerEmail': provider_email,
            'providerLabels': provider_labels,
            'attachments':   attachments,
            'threadId':      msg.get('conversationId', ''),
            'inReplyTo':     in_reply_to,
            'messageId':     msg.get('internetMessageId', ''),
            'receivedAt':    msg.get('receivedDateTime', ''),
            'bodyText':      body_text,
            'bodyHtml':      body_html
        })

    new_emails = finalize_batch(user_id, pending, label_catalog)
    if new_emails:
        account['last_synced_message_id'] = new_messages[0]['id']
        account['last_synced_at'] = new_messages[0].get('receivedDateTime') or account.get('last_synced_at')
    return new_emails, len(new_emails)

def finalize_batch(user_id, pending, label_catalog):
    """Shared tail for both Gmail and Outlook sync: classifies each new email (summary + Maily labels
    + smart-category flag) in one combined batched call, persists it, then runs whatever got flagged
    for a smart category through extraction + matching/merging (see process_smart_category_candidates)."""
    if not pending:
        return []

    classifications = classify_emails_batch(
        [{'subject': p['subject'], 'snippet': p['content']} for p in pending], label_catalog
    )

    new_emails = []
    candidates = []
    for item, result in zip(pending, classifications):
        item['summary'] = result['summary']
        item['labels'] = result['labels']
        put_email_item(item)
        new_emails.append(item)
        # Logged so a "why didn't this email get linked to a card" question can be answered from
        # CloudWatch alone: this is the only record of whether the classifier even considered it
        # a smart-category candidate in the first place.
        print(f"classify emailId={item['emailId']} subject={item['subject']!r} labels={result['labels']} smartCategory={result['smartCategory']}")
        if result['smartCategory']:
            candidates.append((item, result['smartCategory']))

    if candidates:
        process_smart_category_candidates(user_id, candidates)

    return new_emails

def sync_single_account(user_id, account, fetch_limit, label_catalog):
    """Fetch and store emails for one connected email account, dispatching by provider."""
    if account.get('provider') == 'outlook':
        return sync_single_outlook_account(user_id, account, fetch_limit, label_catalog)
    return sync_single_gmail_account(user_id, account, fetch_limit, label_catalog)


def sync_user_emails(user_id, user_record):
    """Syncs all connected email accounts (Gmail + Outlook) for a user. Used by both /sync and EventBridge.
    Returns new_count — how many emails were newly processed this run. Each provider sync mutates its
    account dict's last_synced_message_id/last_synced_at watermark in place; if anything advanced, we
    persist the whole email_accounts list back in a single write rather than one write per account."""
    accounts = user_record.get('email_accounts', [])
    fetch_limit = int(user_record.get('email_fetch_limit', 10))
    label_catalog = get_label_catalog(user_id)  # computed once per user, reused across all their accounts

    new_count = 0
    watermark_advanced = False
    for account in accounts:
        if not account.get('access_token'):
            continue
        before = (account.get('last_synced_message_id'), account.get('last_synced_at'))
        _, count = sync_single_account(user_id, account, fetch_limit, label_catalog)
        new_count += count
        if (account.get('last_synced_message_id'), account.get('last_synced_at')) != before:
            watermark_advanced = True

    if watermark_advanced:
        dynamodb.Table('Maily-Users').update_item(
            Key={'userId': user_id},
            UpdateExpression='SET email_accounts = :accounts',
            ExpressionAttributeValues={':accounts': accounts}
        )

    return new_count

def process_smart_category_candidates(user_id, candidates):
    """Extracts structured fields for each smart-category-flagged email and merges it into a tracked
    Maily-CategoryItems row (creating one if nothing existing matches). Grouped by category type,
    processed oldest-email-first so e.g. an "order confirmed" email creates the item before a later
    "shipped" email tries to merge into it. See CATEGORY_TYPES for the per-category schema/matchKeys."""
    by_type = {}
    for item, category_type in candidates:
        by_type.setdefault(category_type, []).append(item)

    for category_type, items in by_type.items():
        items.sort(key=lambda i: i.get('receivedAt', ''))

        extracted_list = extract_category_fields_batch(
            [{'subject': i['subject'], 'snippet': i['content']} for i in items], category_type
        )

        existing_items = category_items_table.query(
            IndexName='categoryType-index',
            KeyConditionExpression=boto3.dynamodb.conditions.Key('userId').eq(user_id) &
                                    boto3.dynamodb.conditions.Key('categoryType').eq(category_type)
        ).get('Items', [])

        match_keys = CATEGORY_TYPES[category_type]['matchKeys']

        for item, extracted in zip(items, extracted_list):
            matched = _find_deterministic_match(existing_items, extracted, match_keys)
            match_method = 'deterministic' if matched else None
            if not matched and existing_items:
                matched = ai_match_category_item(extracted, item.get('summary', ''), existing_items, category_type)
                match_method = 'ai' if matched else 'ai-no-match'

            print(f"category-match emailId={item['emailId']} extracted={extracted} "
                  f"result={('existing:' + matched['itemId']) if matched else 'new-item'} via={match_method or 'no-existing-items'}")

            now = datetime.now(timezone.utc).isoformat()
            received_at = item.get('receivedAt') or now

            if matched:
                _merge_into_category_item(matched, extracted, item['emailId'], received_at, now)
                target = matched
            else:
                target = {
                    'userId': user_id,
                    'itemId': f"{category_type}#{uuid.uuid4().hex[:12]}",
                    'categoryType': category_type,
                    'fields': {k: v for k, v in extracted.items() if v},
                    'contributingEmailIds': [item['emailId']],
                    'createdAt': now,
                    'updatedAt': now,
                    'lastUpdatedFromEmailAt': received_at,
                }
                existing_items.append(target)  # so later emails in this same batch can also match against it

            category_items_table.put_item(Item=target)
            table.update_item(
                Key={'userId': user_id, 'emailId': item['emailId']},
                UpdateExpression='SET categoryItemId = :c',
                ExpressionAttributeValues={':c': target['itemId']}
            )

def _find_deterministic_match(existing_items, extracted, match_keys):
    for key in match_keys:
        value = extracted.get(key)
        if not value:
            continue
        for existing in existing_items:
            if existing.get('fields', {}).get(key) == value:
                return existing
    return None

def _merge_into_category_item(target, extracted, email_id, received_at, now):
    # Only let this email's values overwrite the item's fields if it's at least as recent as
    # whatever last updated it — guards against an out-of-order email regressing e.g. delivered -> shipped.
    if received_at >= target.get('lastUpdatedFromEmailAt', ''):
        fields = target.setdefault('fields', {})
        for key, value in extracted.items():
            if value:
                fields[key] = value
        target['lastUpdatedFromEmailAt'] = received_at
    if email_id not in target.get('contributingEmailIds', []):
        target.setdefault('contributingEmailIds', []).append(email_id)
    target['updatedAt'] = now


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
            new_count = sync_user_emails(user_id, user)
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

        new_count = sync_user_emails(user_id, user_record)
        all_emails = get_user_emails(user_id)

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
                "error": "Google authentication failed"
            })
        }
    except Exception as e:
        print(f"Error in sync process: {str(e)}")
        return {
            "statusCode": 500,
            "body": json.dumps({"message": "Internal server error during sync"})
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
        return {"statusCode": e.code, "body": json.dumps({"error": "Provider authentication failed"})}
    except Exception as e:
        print(f"Error fetching email body: {str(e)}")
        return {"statusCode": 500, "body": json.dumps({"message": "Internal server error while fetching email body"})}

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
        return {"statusCode": e.code, "body": json.dumps({"error": "Provider authentication failed"})}
    except Exception as e:
        print(f"Error fetching attachment: {str(e)}")
        return {"statusCode": 500, "body": json.dumps({"message": "Internal server error while fetching attachment"})}

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
        return {"statusCode": 500, "body": json.dumps({"message": "Internal server error while fetching thread"})}

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
        return {"statusCode": 500, "body": json.dumps({"message": "Internal server error while summarizing email"})}

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
        return {"statusCode": 500, "body": json.dumps({"message": "Internal server error while marking email as read"})}

def handle_list_labels(event):
    try:
        user_id = get_authorized_user_id(event)
        if not user_id:
            return {"statusCode": 401, "body": json.dumps({"error": "Unauthorized. Could not identify user."})}

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"labels": get_label_catalog(user_id)}, ensure_ascii=False)
        }
    except Exception as e:
        print(f"Error listing labels: {str(e)}")
        return {"statusCode": 500, "body": json.dumps({"message": "Internal server error while listing labels"})}

def handle_create_label(event):
    try:
        user_id = get_authorized_user_id(event)
        if not user_id:
            return {"statusCode": 401, "body": json.dumps({"error": "Unauthorized. Could not identify user."})}

        body = json.loads(event.get('body', '{}'))
        name = (body.get('name') or '').strip()
        description = (body.get('description') or '').strip()
        color = body.get('color') or '#94a3b8'

        if not name or not description:
            return {"statusCode": 400, "body": json.dumps({"error": "name and description are required"})}

        label_id = f"custom#{uuid.uuid4().hex[:12]}"
        labels_table.put_item(Item={
            'userId': user_id,
            'labelId': label_id,
            'name': name,
            'description': description,
            'color': color,
            'createdAt': datetime.now(timezone.utc).isoformat()
        })

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"id": label_id, "name": name, "description": description, "color": color})
        }
    except Exception as e:
        print(f"Error creating label: {str(e)}")
        return {"statusCode": 500, "body": json.dumps({"message": "Internal server error while creating label"})}

def handle_update_label(event):
    try:
        user_id = get_authorized_user_id(event)
        if not user_id:
            return {"statusCode": 401, "body": json.dumps({"error": "Unauthorized. Could not identify user."})}

        body = json.loads(event.get('body', '{}'))
        label_id = body.get('labelId')
        if not label_id or not label_id.startswith('custom#'):
            return {"statusCode": 400, "body": json.dumps({"error": "labelId must refer to a custom label"})}

        updates = {k: body[k] for k in ('name', 'description', 'color') if body.get(k)}
        if not updates:
            return {"statusCode": 400, "body": json.dumps({"error": "Nothing to update"})}

        labels_table.update_item(
            Key={'userId': user_id, 'labelId': label_id},
            UpdateExpression='SET ' + ', '.join(f'#{k} = :{k}' for k in updates),
            ExpressionAttributeNames={f'#{k}': k for k in updates},
            ExpressionAttributeValues={f':{k}': v for k, v in updates.items()}
        )

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"id": label_id, **updates})
        }
    except Exception as e:
        print(f"Error updating label: {str(e)}")
        return {"statusCode": 500, "body": json.dumps({"message": "Internal server error while updating label"})}

def handle_delete_label(event):
    """Deletes a custom label definition. Known v1 limitation: does not retroactively strip the label
    id from already-classified Maily-Emails rows — those simply reference a label that no longer exists."""
    try:
        user_id = get_authorized_user_id(event)
        if not user_id:
            return {"statusCode": 401, "body": json.dumps({"error": "Unauthorized. Could not identify user."})}

        body = json.loads(event.get('body', '{}'))
        label_id = body.get('labelId')
        if not label_id or not label_id.startswith('custom#'):
            return {"statusCode": 400, "body": json.dumps({"error": "labelId must refer to a custom label"})}

        labels_table.delete_item(Key={'userId': user_id, 'labelId': label_id})

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"message": "Label deleted", "id": label_id})
        }
    except Exception as e:
        print(f"Error deleting label: {str(e)}")
        return {"statusCode": 500, "body": json.dumps({"message": "Internal server error while deleting label"})}

def _serialize_category_item(item):
    is_complete, is_at_risk = evaluate_category_item_state(item.get('categoryType'), item.get('fields', {}))
    return {**item, 'isComplete': is_complete, 'isAtRisk': is_at_risk}

def handle_list_category_items(event):
    """Lists this user's tracked smart-category items, optionally filtered to one category type."""
    try:
        user_id = get_authorized_user_id(event)
        if not user_id:
            return {"statusCode": 401, "body": json.dumps({"error": "Unauthorized. Could not identify user."})}

        query_params = event.get('queryStringParameters') or {}
        category_type = query_params.get('type')

        if category_type:
            key_condition = boto3.dynamodb.conditions.Key('userId').eq(user_id) & \
                             boto3.dynamodb.conditions.Key('categoryType').eq(category_type)
            response = category_items_table.query(IndexName='categoryType-index', KeyConditionExpression=key_condition)
        else:
            response = category_items_table.query(KeyConditionExpression=boto3.dynamodb.conditions.Key('userId').eq(user_id))

        items = response.get('Items', [])
        while 'LastEvaluatedKey' in response:
            kwargs = {'ExclusiveStartKey': response['LastEvaluatedKey']}
            if category_type:
                response = category_items_table.query(IndexName='categoryType-index', KeyConditionExpression=key_condition, **kwargs)
            else:
                response = category_items_table.query(KeyConditionExpression=boto3.dynamodb.conditions.Key('userId').eq(user_id), **kwargs)
            items.extend(response.get('Items', []))

        items = [_serialize_category_item(i) for i in items]

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"items": items}, ensure_ascii=False, cls=DecimalEncoder)
        }
    except Exception as e:
        print(f"Error listing category items: {str(e)}")
        return {"statusCode": 500, "body": json.dumps({"message": "Internal server error while listing smart categories"})}

def handle_get_category_item(event):
    """Returns one tracked smart-category item plus the full email rows it was built from, so the
    frontend's detail view is self-contained (card fields + contributing emails in one response)."""
    try:
        user_id = get_authorized_user_id(event)
        if not user_id:
            return {"statusCode": 401, "body": json.dumps({"error": "Unauthorized. Could not identify user."})}

        query_params = event.get('queryStringParameters') or {}
        item_id = query_params.get('itemId')
        if not item_id:
            return {"statusCode": 400, "body": json.dumps({"error": "Missing required query param: itemId"})}

        item = category_items_table.get_item(Key={'userId': user_id, 'itemId': item_id}).get('Item')
        if not item:
            return {"statusCode": 404, "body": json.dumps({"error": "Smart category item not found"})}

        emails = []
        for email_id in item.get('contributingEmailIds', []):
            email_item = table.get_item(Key={'userId': user_id, 'emailId': email_id}).get('Item')
            if email_item:
                emails.append(email_item)
        emails.sort(key=lambda e: e.get('receivedAt', ''))

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"item": _serialize_category_item(item), "emails": emails}, ensure_ascii=False, cls=DecimalEncoder)
        }
    except Exception as e:
        print(f"Error fetching category item: {str(e)}")
        return {"statusCode": 500, "body": json.dumps({"message": "Internal server error while fetching smart category item"})}

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
            "body": json.dumps({"message": "Internal server error during stats"})
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
        freeform_prompt = str(body.get('prompt', '')).strip()

        if not summary and not content and not freeform_prompt:
            return {
                "statusCode": 400,
                "body": json.dumps({"error": "No email content provided to draft a reply for."})
            }

        api_key = get_secrets()['OPENAI_API_KEY']
        if freeform_prompt:
            prompt = (
                "You are a helpful email assistant. Draft a concise, professional email body from the "
                f"following instructions:\n\n{freeform_prompt}\n\n"
                "Write only the email body, without a subject line."
            )
        else:
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
            "body": json.dumps({"message": "Internal server error during draft generation"})
        }

def _email_addresses(value):
    if not isinstance(value, list):
        return []
    addresses = []
    for address in value:
        normalized = str(address).strip()
        if normalized and '@' in normalized and '\n' not in normalized and '\r' not in normalized:
            addresses.append(normalized)
    return addresses

def _decode_compose_attachments(raw_attachments):
    if not isinstance(raw_attachments, list) or len(raw_attachments) > 10:
        raise ValueError("A maximum of 10 attachments is allowed")

    attachments = []
    total_size = 0
    for raw in raw_attachments:
        filename = str(raw.get('filename', '')).strip()
        mime_type = str(raw.get('mimeType') or 'application/octet-stream')
        if not filename or '/' in filename or '\\' in filename:
            raise ValueError("Each attachment must have a valid filename")
        try:
            content = base64.b64decode(raw.get('content', ''), validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Attachment {filename} is not valid base64") from exc
        total_size += len(content)
        if total_size > 3 * 1024 * 1024:
            raise ValueError("Attachments must total 3 MB or less")
        attachments.append({
            'filename': filename,
            'mimeType': mime_type,
            'content': content,
        })
    return attachments

def _send_gmail_message(user_id, account, message_data, attachments):
    message = EmailMessage()
    message['From'] = account['email']
    message['To'] = ', '.join(message_data['to'])
    if message_data['cc']:
        message['Cc'] = ', '.join(message_data['cc'])
    if message_data['bcc']:
        message['Bcc'] = ', '.join(message_data['bcc'])
    if message_data['replyTo']:
        message['Reply-To'] = message_data['replyTo']
    if message_data.get('inReplyTo'):
        message['In-Reply-To'] = message_data['inReplyTo']
        message['References'] = message_data['inReplyTo']
    message['Subject'] = message_data['subject']
    message.set_content(message_data['body'])

    for attachment in attachments:
        mime_parts = attachment['mimeType'].split('/', 1)
        maintype = mime_parts[0] if len(mime_parts) == 2 else 'application'
        subtype = mime_parts[1] if len(mime_parts) == 2 else 'octet-stream'
        message.add_attachment(
            attachment['content'],
            maintype=maintype,
            subtype=subtype,
            filename=attachment['filename']
        )

    payload = {'raw': base64.urlsafe_b64encode(message.as_bytes()).decode('ascii').rstrip('=')}
    if message_data.get('threadId'):
        payload['threadId'] = message_data['threadId']
    return api_request(
        user_id,
        account,
        'https://gmail.googleapis.com/gmail/v1/users/me/messages/send',
        payload=payload
    )

def _graph_recipients(addresses):
    return [{'emailAddress': {'address': address}} for address in addresses]

def _send_outlook_message(user_id, account, message_data, attachments):
    message = {
        'subject': message_data['subject'],
        'body': {'contentType': 'Text', 'content': message_data['body']},
        'toRecipients': _graph_recipients(message_data['to']),
        'ccRecipients': _graph_recipients(message_data['cc']),
        'bccRecipients': _graph_recipients(message_data['bcc']),
    }
    if message_data['replyTo']:
        message['replyTo'] = _graph_recipients([message_data['replyTo']])
    graph_attachments = [{
            '@odata.type': '#microsoft.graph.fileAttachment',
            'name': attachment['filename'],
            'contentType': attachment['mimeType'],
            'contentBytes': base64.b64encode(attachment['content']).decode('ascii'),
        } for attachment in attachments]

    mode = message_data.get('mode', 'new')
    original_id = message_data.get('originalMessageId')
    if mode in ('reply', 'replyAll', 'forward') and original_id:
        action = {'reply': 'createReply', 'replyAll': 'createReplyAll', 'forward': 'createForward'}[mode]
        encoded_id = urllib.parse.quote(original_id, safe='')
        draft = api_request(
            user_id,
            account,
            f'https://graph.microsoft.com/v1.0/me/messages/{encoded_id}/{action}',
            payload={}
        )
        draft_id = urllib.parse.quote(draft['id'], safe='')
        api_request(
            user_id,
            account,
            f'https://graph.microsoft.com/v1.0/me/messages/{draft_id}',
            method='PATCH',
            payload=message
        )
        for attachment in graph_attachments:
            api_request(
                user_id,
                account,
                f'https://graph.microsoft.com/v1.0/me/messages/{draft_id}/attachments',
                payload=attachment
            )
        return api_request(
            user_id,
            account,
            f'https://graph.microsoft.com/v1.0/me/messages/{draft_id}/send',
            payload=None
        )

    if graph_attachments:
        message['attachments'] = graph_attachments

    return api_request(
        user_id,
        account,
        'https://graph.microsoft.com/v1.0/me/sendMail',
        payload={'message': message, 'saveToSentItems': True}
    )

def handle_send_email(event):
    try:
        user_id = get_authorized_user_id(event)
        if not user_id:
            return {"statusCode": 401, "body": json.dumps({"error": "Unauthorized"})}

        body = json.loads(event.get('body', '{}'))
        sender_email = str(body.get('senderEmail', '')).strip()
        provider = body.get('provider')
        to = _email_addresses(body.get('to'))
        cc = _email_addresses(body.get('cc'))
        bcc = _email_addresses(body.get('bcc'))
        subject = str(body.get('subject', '')).strip()
        content = str(body.get('body', '')).strip()
        reply_to = str(body.get('replyTo', '')).strip()

        if provider not in ('gmail', 'outlook') or not sender_email:
            return {"statusCode": 400, "body": json.dumps({"error": "A connected sender account is required"})}
        if not to:
            return {"statusCode": 400, "body": json.dumps({"error": "At least one valid recipient is required"})}
        if not subject or not content:
            return {"statusCode": 400, "body": json.dumps({"error": "Subject and body are required"})}
        if reply_to and not _email_addresses([reply_to]):
            return {"statusCode": 400, "body": json.dumps({"error": "Reply-To must be a valid email address"})}

        users_table = dynamodb.Table('Maily-Users')
        user_record = users_table.get_item(Key={'userId': user_id}).get('Item', {})
        account = next((item for item in user_record.get('email_accounts', [])
                        if item.get('email') == sender_email and item.get('provider') == provider), None)
        if not account:
            return {"statusCode": 404, "body": json.dumps({"error": "Sender account is not connected"})}

        signature = str(user_record.get('signature', '')).strip()
        if signature and not content.endswith(signature):
            content = f"{content}\n\n{signature}"
        attachments = _decode_compose_attachments(body.get('attachments', []))
        message_data = {
            'to': to,
            'cc': cc,
            'bcc': bcc,
            'subject': subject,
            'body': content,
            'replyTo': reply_to,
            'threadId': str(body.get('threadId', '')).strip(),
            'inReplyTo': str(body.get('inReplyTo', '')).strip(),
            'mode': body.get('mode', 'new'),
            'originalMessageId': str(body.get('originalMessageId', '')).strip(),
        }

        if provider == 'outlook':
            _send_outlook_message(user_id, account, message_data, attachments)
        else:
            _send_gmail_message(user_id, account, message_data, attachments)

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"message": "Email sent successfully"})
        }
    except ValueError as e:
        return {"statusCode": 400, "body": json.dumps({"error": str(e)})}
    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8', errors='replace')
        print(f"Provider send error ({e.code}): {error_body}")
        return {"statusCode": 502, "body": json.dumps({"error": "Email provider rejected the message"})}
    except Exception as e:
        print(f"Error sending email: {str(e)}")
        return {"statusCode": 500, "body": json.dumps({"error": "Internal server error while sending email"})}

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
            "body": json.dumps({"message": "Internal server error during export"})
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
            "body": json.dumps({
                "accounts": safe_accounts,
                "settings": {
                    "email_fetch_limit": int(result.get('Item', {}).get('email_fetch_limit', 10)),
                    "signature": result.get('Item', {}).get('signature', '')
                }
            })
        }
    except Exception as e:
        print(f"Error getting accounts: {str(e)}")
        return {"statusCode": 500, "body": json.dumps({"error": "Internal server error while getting accounts"})}


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
        return {"statusCode": 500, "body": json.dumps({"error": "Internal server error while disconnecting account"})}
