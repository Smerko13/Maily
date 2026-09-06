import json
import os
import re
import html
import time
import uuid
import base64
import urllib.parse
import boto3
import urllib.request
import urllib.error
import concurrent.futures
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr
from botocore.exceptions import ClientError

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table('Maily-Emails')
labels_table = dynamodb.Table('Maily-Labels')
category_items_table = dynamodb.Table('Maily-CategoryItems')
category_types_table = dynamodb.Table('Maily-CategoryTypes')
travel_trips_table = dynamodb.Table('Maily-TravelTrips')

_secrets_cache = None

# Bounds how many of one account's messages/attachments we fetch from the provider at once. Kept
# per-account (never shared across accounts — each sync_single_*_account call gets its own pool),
# same boundary the AI classification batching already uses (see finalize_batch/classify_emails_batch).
_PROVIDER_FETCH_CONCURRENCY = 8

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
        "keyMode": "OR",
        "titleTemplate": "{merchant} — {orderDescription}",
        "primaryDateField": "estimatedDelivery",
        "cardFields": ["trackingNumber", "carrier"],
        "completionRule": {"type": "field_equals", "field": "status", "values": ["delivered"]},
        "atRiskRule": {"type": "date_passed_without", "dateField": "estimatedDelivery",
                       "field": "status", "values": ["delivered"]},
    },
    # Behaves like any other category (same fields/matchKeys/rules mechanism) for extraction/matching/
    # rendering — the one thing layered on top is purely a frontend grouping concept (see /travel-trips):
    # a user-created "trip" is just a name + date range, and a travel item belongs to a trip if its
    # startDate falls inside that range. Nothing about that grouping is stored on the item or the
    # category schema, so it works with zero backend changes beyond the trip CRUD endpoints themselves.
    "travel": {
        "label": "Travel",
        "icon": "✈️",
        "classifierDescription": (
            "emails about a trip booking: flight confirmations/boarding passes/itinerary changes, hotel "
            "or other lodging reservations, car rental confirmations, and activity/attraction/tour/event "
            "tickets tied to travel — not a routine local event, specifically something booked as part of "
            "a trip away from home"
        ),
        "fields": [
            {"key": "itemType", "label": "Type", "type": "enum",
             "values": ["flight", "hotel", "car_rental", "activity", "other"],
             "hint": "What kind of travel booking this email is about"},
            {"key": "title", "label": "Title", "type": "string",
             "hint": "A short human name for this booking, e.g. 'Flight to Rome' or 'Hotel Roma Central'"},
            {"key": "provider", "label": "Provider", "type": "string",
             "hint": "Airline, hotel chain/property, rental company, or activity operator name"},
            {"key": "location", "label": "Location", "type": "string",
             "hint": "City/airport/address relevant to this booking"},
            {"key": "startDate", "label": "Start", "type": "date",
             "hint": "Check-in date, departure date, pickup date, or the activity's date"},
            {"key": "endDate", "label": "End", "type": "date",
             "hint": "Check-out date, return date, or drop-off date. If this booking has no separate end "
                     "(e.g. a single-day activity or a one-way flight), use the same value as startDate — "
                     "never leave this empty when startDate is present."},
            {"key": "confirmationNumber", "label": "Confirmation #", "type": "string", "sticky": True},
            {"key": "details", "label": "Details", "type": "string",
             "hint": "Other useful specifics: seat/room, passenger/guest names, price, terms"},
        ],
        "matchKeys": ["confirmationNumber"],
        "keyMode": "OR",
        "titleTemplate": "{title} — {provider}",
        "primaryDateField": "startDate",
        "cardFields": ["itemType", "startDate"],
        "completionRule": {"type": "date_passed", "dateField": "endDate"},
        "atRiskRule": None,
    },
}

# The 5 field types a category schema (built-in or user-defined) can declare. "enum" fields carry a
# fixed `values` list (rendered/validated as a closed set); "boolean" fields are validated to exactly
# "true"/"false"; "number"/"date"/"string" are trusted as free-form strings once extracted.
CATEGORY_FIELD_TYPES = {"string", "number", "date", "enum", "boolean"}
# The 3 rule types evaluate_category_rule understands — a user-defined schema's completionRule/atRiskRule
# must be one of these (or None). "date_passed" is unconditional (e.g. an event's date has simply gone
# by); "date_passed_without" additionally requires a status field to have NOT reached some value yet
# (e.g. a delivery is at risk once its estimate has passed without reaching "delivered").
CATEGORY_RULE_TYPES = {"field_equals", "date_passed_without", "date_passed"}
# Optional display-hint per field type, driving which widget the frontend renders it with (e.g. a
# "number" field with format "currency" renders as $42.99 instead of a bare 42.99). A field's format
# must be valid for its own type — a "currency" format on a string field is meaningless and dropped.
CATEGORY_FIELD_FORMATS = {
    "number": {"currency", "percent"},
    "string": {"url"},
    "date": {"relative-date"},
}
# "OR" (default): any single matchKeys field matching an existing item's value is enough to consider it
# the same item (e.g. delivery's trackingNumber OR orderNumber). "AND": every matchKeys field must match
# together (e.g. job applications: companyDomain + roleTitle, since neither alone is unique).
CATEGORY_KEY_MODES = {"OR", "AND"}

def _category_type_row_to_schema(row):
    """Converts a Maily-CategoryTypes DynamoDB row into the same schema shape as a CATEGORY_TYPES entry,
    so downstream code (classify/extract/match/rules) never has to distinguish built-in vs. custom."""
    return {
        "label": row.get("label", ""),
        "icon": row.get("icon") or "🏷️",
        "classifierDescription": row.get("classifierDescription", ""),
        "fields": row.get("fields", []),
        "matchKeys": row.get("matchKeys", []) or [],
        "keyMode": row.get("keyMode") if row.get("keyMode") in CATEGORY_KEY_MODES else "OR",
        "titleTemplate": row.get("titleTemplate", ""),
        "primaryDateField": row.get("primaryDateField", ""),
        "cardFields": row.get("cardFields", []) or [],
        "completionRule": row.get("completionRule") or None,
        "atRiskRule": row.get("atRiskRule") or None,
        "automations": row.get("automations", []) or [],
        "schemaVersion": row.get("schemaVersion", 1),
    }

def get_category_type_catalog(user_id):
    """This user's full category-type set: built-in types plus their own custom ones, keyed by id."""
    custom = category_types_table.query(
        KeyConditionExpression=boto3.dynamodb.conditions.Key('userId').eq(user_id)
    ).get('Items', [])
    catalog = dict(CATEGORY_TYPES)
    for c in custom:
        catalog[c['categoryTypeId']] = _category_type_row_to_schema(c)
    return catalog

_FIELD_KEY_RE = re.compile(r'^[a-zA-Z][a-zA-Z0-9]*$')

def _sanitize_rule(rule, field_keys):
    """A rule is only ever trusted if it's one of the 3 known types and every field it references
    actually exists on the schema — never partially trusted (e.g. a rule pointing at a since-removed
    field is dropped entirely, not left referencing nothing)."""
    if not isinstance(rule, dict):
        return None
    if rule.get('type') not in CATEGORY_RULE_TYPES:
        # The AI occasionally wraps a rule by its type name instead of using a "type" key, e.g.
        # {"date_passed": {"dateField": "eventDate"}} instead of {"type": "date_passed", "dateField":
        # "eventDate"}. Both mean the same thing, so normalize rather than reject — the shape is
        # unambiguous either way since only one rule-type key can be present.
        for rule_type in CATEGORY_RULE_TYPES:
            wrapped = rule.get(rule_type)
            if isinstance(wrapped, dict):
                rule = {**wrapped, 'type': rule_type}
                break
    if rule.get('type') not in CATEGORY_RULE_TYPES:
        return None
    if rule['type'] == 'date_passed':
        date_field = rule.get('dateField')
        if date_field not in field_keys:
            return None
        return {'type': 'date_passed', 'dateField': date_field}
    values = [str(v) for v in (rule.get('values') or []) if str(v).strip()]
    if not values:
        return None
    if rule['type'] == 'field_equals':
        field = rule.get('field')
        if field not in field_keys:
            return None
        return {'type': 'field_equals', 'field': field, 'values': values}
    date_field, field = rule.get('dateField'), rule.get('field')
    if date_field not in field_keys or field not in field_keys:
        return None
    return {'type': 'date_passed_without', 'dateField': date_field, 'field': field, 'values': values}

def _sanitize_category_draft(raw):
    """Validates/coerces an AI-generated or client-submitted category draft into a safe schema dict —
    this eventually drives DynamoDB writes, OpenAI prompts, and generic frontend rendering, so nothing
    from it (field keys, types, rule references) is trusted outright. Returns (schema, warnings);
    anything corrected/dropped is surfaced as a warning rather than failing the request outright."""
    warnings = []
    if not isinstance(raw, dict):
        raw = {}

    label = str(raw.get('label') or '').strip()[:60] or 'Untitled Category'
    icon = str(raw.get('icon') or '').strip()[:8] or '🏷️'
    classifier_description = str(raw.get('classifierDescription') or '').strip()[:500]
    if not classifier_description:
        warnings.append('classifierDescription was empty — this category will rarely be auto-detected until one is added.')

    seen_keys = set()
    fields = []
    for f in (raw.get('fields') or [])[:12]:
        if not isinstance(f, dict):
            continue
        key = str(f.get('key') or '').strip()
        if not _FIELD_KEY_RE.match(key) or key in seen_keys:
            continue

        raw_type = f.get('type')
        field_type = raw_type if raw_type in CATEGORY_FIELD_TYPES else 'string'
        if raw_type not in CATEGORY_FIELD_TYPES:
            warnings.append(f'Field "{key}" had an unrecognized type — defaulted to string.')

        field = {'key': key, 'label': str(f.get('label') or key).strip()[:60], 'type': field_type}
        hint = str(f.get('hint') or '').strip()[:300]
        if hint:
            field['hint'] = hint
        if field_type == 'enum':
            values = [str(v).strip() for v in (f.get('values') or []) if str(v).strip()][:20]
            if not values:
                warnings.append(f'Enum field "{key}" had no values — dropped.')
                continue
            field['values'] = values
        if f.get('sticky'):
            field['sticky'] = True
        raw_format = f.get('format')
        if raw_format:
            if raw_format in CATEGORY_FIELD_FORMATS.get(field_type, set()):
                field['format'] = raw_format
            else:
                warnings.append(f'Field "{key}" had a format not valid for its type ({field_type}) — dropped.')

        seen_keys.add(key)
        fields.append(field)

    if not fields:
        warnings.append('No valid fields were produced — add at least one field manually.')

    field_keys = {f['key'] for f in fields}
    date_field_keys = {f['key'] for f in fields if f['type'] == 'date'}

    key_mode = raw.get('keyMode') if raw.get('keyMode') in CATEGORY_KEY_MODES else 'OR'
    match_keys = [k for k in (raw.get('matchKeys') or []) if k in field_keys][:4]
    if key_mode == 'AND' and not match_keys:
        # AND with nothing to AND together is meaningless — fall back rather than silently break matching.
        key_mode = 'OR'
        warnings.append('keyMode was "AND" but no valid matchKeys were given — fell back to "OR".')
    if match_keys:
        # A key field's value can't be allowed to change later without corrupting matching — force it
        # sticky regardless of what the draft said, rather than trust the AI/user got this right.
        key_field_set = set(match_keys)
        for f in fields:
            if f['key'] in key_field_set:
                f['sticky'] = True

    card_fields = [k for k in (raw.get('cardFields') or []) if k in field_keys][:2]
    if not card_fields and fields:
        # Never let a card render blank: default to the first non-enum field(s), falling back to
        # whatever fields exist at all if every field happens to be an enum.
        card_fields = [f['key'] for f in fields if f['type'] != 'enum'][:2] or [f['key'] for f in fields[:2]]

    primary_date_field = raw.get('primaryDateField')
    if primary_date_field not in date_field_keys:
        primary_date_field = next(iter(date_field_keys), '')

    title_template = str(raw.get('titleTemplate') or '').strip()[:200] or label

    completion_rule = _sanitize_rule(raw.get('completionRule'), field_keys)
    at_risk_rule = _sanitize_rule(raw.get('atRiskRule'), field_keys)
    if raw.get('completionRule') and not completion_rule:
        warnings.append('completionRule was invalid and was dropped.')
    if raw.get('atRiskRule') and not at_risk_rule:
        warnings.append('atRiskRule was invalid and was dropped.')

    schema = {
        'label': label,
        'icon': icon,
        'classifierDescription': classifier_description,
        'fields': fields,
        'matchKeys': match_keys,
        'keyMode': key_mode,
        'titleTemplate': title_template,
        'primaryDateField': primary_date_field,
        'cardFields': card_fields,
        'completionRule': completion_rule,
        'atRiskRule': at_risk_rule,
        'automations': [],
    }
    return schema, warnings

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

class ReauthRequiredError(Exception):
    """Raised when a provider rejects a refresh_token outright (invalid_grant) rather than just
    expiring the access token — the account needs the user to reconnect, not just retry."""
    pass

def _mark_account_needs_reauth(user_id, provider, provider_email):
    """Persists needsReauth=True on the matching account so Settings can surface a 'Reconnect'
    prompt. Written directly here (not left for the caller to save) because the scheduled/manual
    sync loop only writes email_accounts back when a sync actually advances a watermark — a failed
    account never would, so this flag would otherwise be set in memory and immediately lost."""
    users_table = dynamodb.Table('Maily-Users')
    result = users_table.get_item(Key={'userId': user_id})
    accounts = result.get('Item', {}).get('email_accounts', [])
    for account in accounts:
        if account.get('email') == provider_email and account.get('provider') == provider:
            account['needsReauth'] = True
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

    try:
        with urllib.request.urlopen(req) as resp:
            token_data = json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8')
        if e.code == 400 and 'invalid_grant' in error_body:
            print(f"Refresh token for {google_email} (gmail) is no longer valid, flagging for reconnect: {error_body}")
            _mark_account_needs_reauth(user_id, 'gmail', google_email)
            raise ReauthRequiredError(f"Gmail account {google_email} needs to be reconnected") from e
        raise

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

    try:
        with urllib.request.urlopen(req) as resp:
            token_data = json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8')
        if e.code == 400 and 'invalid_grant' in error_body:
            print(f"Refresh token for {outlook_email} (outlook) is no longer valid, flagging for reconnect: {error_body}")
            _mark_account_needs_reauth(user_id, 'outlook', outlook_email)
            raise ReauthRequiredError(f"Outlook account {outlook_email} needs to be reconnected") from e
        raise

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

_HTML_TAG_RE = re.compile(r'<[^>]+>')
_HTML_WHITESPACE_RE = re.compile(r'[ \t]+')
_URL_RE = re.compile(r'<?https?://\S+>?')
_AI_INPUT_MAX_CHARS = 4000

def _strip_urls(text):
    """Replaces raw/bracketed URLs with a short placeholder. Transactional emails — especially
    click-tracking services like SendGrid — often have plain-text bodies dominated by huge encoded
    redirect URLs (Gmail renders links in text/plain as "link text <https://...huge-tracking-url>"),
    which add no semantic value for extraction and can derail or overflow the AI's attention budget."""
    return _URL_RE.sub('[link]', text)

def html_to_text(html_content):
    """Minimal HTML→plaintext for feeding an email body to the AI — doesn't need to be pixel-perfect,
    just readable enough for extraction/classification. Used as a fallback when a message has no
    text/plain part (common for HTML-only marketing/ticket-confirmation style emails)."""
    if not html_content:
        return ''
    text = re.sub(r'(?is)<(script|style)[^>]*>.*?</\1>', ' ', html_content)
    text = re.sub(r'(?i)<br\s*/?>', '\n', text)
    text = re.sub(r'(?i)</(p|div|tr|li)>', '\n', text)
    text = _HTML_TAG_RE.sub(' ', text)
    text = html.unescape(text)
    text = _HTML_WHITESPACE_RE.sub(' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

def _email_ai_text(item):
    """The richest text available for AI classification/extraction — prefers the actual body (already
    fetched during sync via format=full/$select=body at no extra API cost) over the provider's short
    auto-generated preview (Gmail's `snippet` / Outlook's `bodyPreview`), which is often too truncated
    to contain details a category schema needs (e.g. a venue address further down the email). Capped
    to bound token cost — a category's fields rarely need more than the opening portion of an email."""
    body = (item.get('bodyText') or html_to_text(item.get('bodyHtml') or '')).strip()
    text = _strip_urls(body or item.get('content', ''))
    return text[:_AI_INPUT_MAX_CHARS]

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

def _is_date_field_passed(fields, date_field_key):
    date_str = fields.get(date_field_key)
    if not date_str:
        return False
    try:
        target = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return False
    return target < datetime.now(timezone.utc)

def _rule_date_passed(fields, rule):
    """True once dateField is in the past — unconditional, no companion status field (e.g. an event
    counts as done once its date has gone by, regardless of any other field)."""
    return _is_date_field_passed(fields, rule['dateField'])

def _rule_date_passed_without(fields, rule):
    """True when dateField is in the past AND `field` hasn't reached one of `values` yet."""
    if fields.get(rule['field']) in rule['values']:
        return False
    return _is_date_field_passed(fields, rule['dateField'])

def evaluate_category_rule(rule, fields):
    if not rule:
        return False
    if rule['type'] == 'field_equals':
        return _rule_field_equals(fields, rule)
    if rule['type'] == 'date_passed_without':
        return _rule_date_passed_without(fields, rule)
    if rule['type'] == 'date_passed':
        return _rule_date_passed(fields, rule)
    return False

def evaluate_category_item_state(category_type, fields, category_catalog):
    """Returns (isComplete, isAtRisk) for a tracked category item, driven entirely by that
    category type's completionRule/atRiskRule — no per-category-type logic needed here."""
    schema = category_catalog.get(category_type, {})
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

def classify_emails_batch(items, label_catalog, category_catalog):
    """Summarizes + assigns labels + flags a smart-category type for multiple emails (each
    {'subject':, 'snippet':}) in as few OpenAI calls as possible, chunked into groups of
    _SUMMARY_BATCH_SIZE. Returns a list of {'summary','labels','smartCategory'} in the same order as items.
    Combined into one call (rather than a separate call per concern) since the dominant cost is the
    email content itself, already being sent for summarization."""
    results = []
    for i in range(0, len(items), _SUMMARY_BATCH_SIZE):
        results.extend(_classify_batch_chunk(items[i:i + _SUMMARY_BATCH_SIZE], label_catalog, category_catalog))
    return results

def _classify_batch_chunk(items, label_catalog, category_catalog):
    if not items:
        return []

    api_key = get_secrets()['OPENAI_API_KEY']

    numbered_emails = "\n\n".join(
        f"Email {i + 1}:\nSubject: {item['subject']}\nContent: {item['snippet']}"
        for i, item in enumerate(items)
    )
    label_lines = "\n".join(f"- {l['id']}: {l['description']}" for l in label_catalog)
    category_lines = "\n".join(f"- {key}: {schema['classifierDescription']}" for key, schema in category_catalog.items())

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
        if smart_category not in category_catalog:
            smart_category = None
        normalized.append({'summary': summary, 'labels': labels, 'smartCategory': smart_category})
    return normalized

_BACKFILL_CLASSIFY_BATCH_SIZE = 20

def _classify_for_category_batch(items, schema):
    """Batched yes/no check: does each email match this ONE category? Lighter than classify_emails_batch
    (no labels, no picking among several categories) — used only for backfilling a newly created
    category against already-synced mail, where re-touching labels isn't wanted."""
    results = []
    for i in range(0, len(items), _BACKFILL_CLASSIFY_BATCH_SIZE):
        results.extend(_classify_for_category_chunk(items[i:i + _BACKFILL_CLASSIFY_BATCH_SIZE], schema))
    return results

def _classify_for_category_chunk(items, schema):
    if not items:
        return []
    api_key = get_secrets()['OPENAI_API_KEY']
    numbered_emails = "\n\n".join(
        f"Email {i + 1}:\nSubject: {item['subject']}\nContent: {item['snippet']}"
        for i, item in enumerate(items)
    )
    prompt = (
        f"Does each of these {len(items)} emails clearly match this category: "
        f"{schema['classifierDescription']}\n\n{numbered_emails}\n\n"
        f'Respond with a JSON object {{"results": [true or false, ...]}}. '
        f"Return exactly {len(items)} results, in the same order as the emails above."
    )
    body = json.dumps({
        "model": "gpt-4.1-nano",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": min(10 * len(items), 500),
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
        results.append(False)
    return [bool(r) for r in results[:len(items)]]

def backfill_category_type(user_id, category_type_id, schema):
    """Scans this user's already-synced emails that aren't already linked to some tracked item against
    a newly created category, so cards populate immediately from mail that predates the category —
    not just future syncs. Mirrors the sync-time pipeline (classify → extract → match/merge) but scoped
    to one category and one user's existing mail instead of a fresh sync batch. Returns how many
    emails got linked."""
    response = table.query(KeyConditionExpression=boto3.dynamodb.conditions.Key('userId').eq(user_id))
    emails = response.get('Items', [])
    while 'LastEvaluatedKey' in response:
        response = table.query(
            KeyConditionExpression=boto3.dynamodb.conditions.Key('userId').eq(user_id),
            ExclusiveStartKey=response['LastEvaluatedKey']
        )
        emails.extend(response.get('Items', []))

    candidates = [e for e in emails if not e.get('categoryItemId')]
    if not candidates:
        return 0

    texts = [_richer_email_text(user_id, e) for e in candidates]
    snippets = [{'subject': e.get('subject', ''), 'snippet': t} for e, t in zip(candidates, texts)]
    matches = _classify_for_category_batch(snippets, schema)

    matched = [(e, t) for e, t, is_match in zip(candidates, texts, matches) if is_match]
    if not matched:
        return 0
    matched.sort(key=lambda pair: pair[0].get('receivedAt', ''))

    extracted_list = _extract_fields_with_schema(
        [{'subject': e['subject'], 'snippet': t} for e, t in matched], schema
    )
    existing_items = category_items_table.query(
        IndexName='categoryType-index',
        KeyConditionExpression=boto3.dynamodb.conditions.Key('userId').eq(user_id) &
                                boto3.dynamodb.conditions.Key('categoryType').eq(category_type_id)
    ).get('Items', [])
    for (email_item, _text), extracted in zip(matched, extracted_list):
        match_and_save_category_item(user_id, email_item, extracted, category_type_id, schema, existing_items)

    return len(matched)

_EXTRACTION_BATCH_SIZE = 20

def extract_category_fields_batch(items, category_type, category_catalog):
    """Looks up category_type's schema in the catalog and delegates to _extract_fields_with_schema.
    Only ever called on the subset of a sync batch already flagged with this category_type."""
    return _extract_fields_with_schema(items, category_catalog[category_type])

def _extract_fields_with_schema(items, schema):
    """Extracts a category schema's structured fields (e.g. tracking number, carrier, status for
    "delivery") from each email, chunked the same way as classify_emails_batch. Takes the schema
    directly (rather than a category_type id) so it also works for an unpersisted wizard draft that
    has no catalog entry yet — used both by sync-time extraction and the wizard's live preview."""
    results = []
    for i in range(0, len(items), _EXTRACTION_BATCH_SIZE):
        results.extend(_extract_fields_with_schema_chunk(items[i:i + _EXTRACTION_BATCH_SIZE], schema))
    return results

def _extract_fields_with_schema_chunk(items, schema):
    if not items:
        return []

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

    return [_normalize_extracted_row(r, schema) for r in results]

def _normalize_extracted_row(raw_row, schema):
    """Validates one extracted-fields dict against a schema: drops unknown keys, hallucinated/invalid
    enum values (e.g. a status the model invented outside the allowed set — otherwise storable garbage
    that can never satisfy a completionRule/atRiskRule), and non-boolean values for boolean fields.
    Used both for fresh AI extraction output and for client-echoed extraction data (e.g. a wizard's
    reference emails, finalized at category-type creation time without a second AI call)."""
    fields = schema['fields']
    field_keys = [f['key'] for f in fields]
    enum_allowed = {f['key']: set(f['values']) for f in fields if f.get('type') == 'enum'}
    boolean_keys = {f['key'] for f in fields if f.get('type') == 'boolean'}
    row = {}
    if isinstance(raw_row, dict):
        for k in field_keys:
            v = raw_row.get(k)
            if not v:
                continue
            if k in enum_allowed and v not in enum_allowed[k]:
                continue
            if k in boolean_keys:
                v = str(v).strip().lower()
                if v not in ('true', 'false'):
                    continue
            # Every field value is stored/treated as a plain string everywhere downstream (frontend
            # types fields as Record<string, string>, formatting is done from the string on display) —
            # but the AI returns JSON, so a "number" field can come back as a native int/float. Stringify
            # unconditionally: DynamoDB's boto3 client outright rejects raw Python floats (needs Decimal),
            # so leaving one in here would crash the put_item for this item, not just look inconsistent.
            row[k] = str(v)
    return row

def ai_match_category_item(extracted, summary, existing_items, category_type, category_catalog):
    """Small, non-batched fallback match call — only used when deterministic matchKeys don't apply
    (e.g. an order-confirmation email with no tracking number yet), and only when the user has at
    least one existing open item of this category type. Returns the matched item dict, or None."""
    schema = category_catalog[category_type]
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
    elif http_method == 'PATCH' and path == '/smart-category':
        return handle_update_category_item_state(event)
    elif http_method == 'GET' and path == '/category-types':
        return handle_list_category_types(event)
    elif http_method == 'POST' and path == '/category-types/generate':
        return handle_generate_category_draft(event)
    elif http_method == 'POST' and path == '/category-types':
        return handle_create_category_type(event)
    elif http_method == 'PUT' and path == '/category-types':
        return handle_replace_category_type(event)
    elif http_method == 'PATCH' and path == '/category-types':
        return handle_patch_category_type(event)
    elif http_method == 'DELETE' and path == '/category-types':
        return handle_delete_category_type(event)
    elif http_method == 'GET' and path == '/travel-trips':
        return handle_list_travel_trips(event)
    elif http_method == 'POST' and path == '/travel-trips':
        return handle_create_travel_trip(event)
    elif http_method == 'DELETE' and path == '/travel-trips':
        return handle_delete_travel_trip(event)
    elif http_method == 'POST' and path == '/email-classify':
        return handle_email_classify(event)
    elif http_method == 'GET' and path == '/search':
        return handle_search_emails(event)
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

def get_user_emails(user_id, account_filter=None, label_filter=None, direction_filter=None):
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
    if direction_filter:
        # Emails synced before the 'direction' field existed have no such key — treat those as
        # 'received' (the historical default of "everything shows in the inbox") rather than
        # excluding them from every direction filter.
        items = [e for e in items if (e.get('direction') or 'received') == direction_filter]

    return items

def handle_get_emails(event):
    try:
        user_id = get_authorized_user_id(event)
        if not user_id:
            return {
                "statusCode": 401,
                "body": json.dumps({"error": "Unauthorized. Could not identify user."})
            }

        # Optional filters: ?account=user@gmail.com, ?label=work, ?direction=sent|received
        query_params = event.get('queryStringParameters') or {}
        items = get_user_emails(user_id, query_params.get('account'), query_params.get('label'), query_params.get('direction'))

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

class GmailHistoryExpiredError(Exception):
    """Raised when Gmail no longer has history records back to our stored startHistoryId (it only
    retains ~7-30 days of history) \u2014 the caller must fall back to a full bootstrap re-list."""
    pass

def _gmail_current_history_id(user_id, account):
    """Cheap call used to establish a fresh incremental-sync baseline (on first-ever sync for an
    account, or after a GmailHistoryExpiredError forces a re-bootstrap)."""
    profile = api_get(user_id, account, 'https://gmail.googleapis.com/gmail/v1/users/me/profile')
    return profile.get('historyId')

def _gmail_bootstrap_message_ids(user_id, account, fetch_limit):
    """First-ever sync for this account, or history too old to resume from: just grab the most
    recent fetch_limit messages, same as a fresh install would see. This does not need a watermark
    cutoff \u2014 with a fresh (or reset) baseline everything returned here is "new" by definition."""
    list_url = f'https://gmail.googleapis.com/gmail/v1/users/me/messages?maxResults={fetch_limit}'
    list_data = api_get(user_id, account, list_url)
    return [m['id'] for m in list_data.get('messages', [])]

def _gmail_history_message_ids(user_id, account, start_history_id):
    """Uses Gmail's history API to ask "what messages were added since start_history_id" directly,
    instead of re-listing the recent messages and re-diffing against a watermark on every sync (the
    old approach, which also had a latent bug: a backlog bigger than fetch_limit would never find the
    watermark and silently skip the overflow forever). Raises GmailHistoryExpiredError if Gmail has
    already discarded history that far back, in which case the caller re-bootstraps."""
    message_ids = []
    new_history_id = start_history_id
    page_token = None
    while True:
        url = (
            f'https://gmail.googleapis.com/gmail/v1/users/me/history'
            f'?startHistoryId={start_history_id}&historyTypes=messageAdded&maxResults=100'
        )
        if page_token:
            url += f'&pageToken={page_token}'
        try:
            data = api_get(user_id, account, url)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise GmailHistoryExpiredError(f"startHistoryId {start_history_id} is no longer available") from e
            raise
        for record in data.get('history', []):
            for added in record.get('messagesAdded', []):
                msg_id = added.get('message', {}).get('id')
                if msg_id:
                    message_ids.append(msg_id)
        if 'historyId' in data:
            new_history_id = data['historyId']
        page_token = data.get('nextPageToken')
        if not page_token:
            break

    # The same message can appear in more than one history record (e.g. added, then labeled) \u2014
    # de-dupe while keeping first-seen order.
    seen = set()
    deduped_ids = [mid for mid in message_ids if not (mid in seen or seen.add(mid))]
    return deduped_ids, new_history_id

def _fetch_gmail_messages_full(user_id, account, message_ids):
    """Fetches full message data for a batch of ids concurrently, scoped to this one account only
    (never mixed with another account's messages \u2014 same per-account boundary the AI classification
    batching uses, see finalize_batch). Replaces a serial one-at-a-time loop that made this the
    slowest part of every Gmail sync."""
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=_PROVIDER_FETCH_CONCURRENCY) as executor:
        future_to_id = {
            executor.submit(
                api_get, user_id, account,
                f'https://gmail.googleapis.com/gmail/v1/users/me/messages/{raw_id}?format=full'
            ): raw_id
            for raw_id in message_ids
        }
        for future in concurrent.futures.as_completed(future_to_id):
            results[future_to_id[future]] = future.result()
    # as_completed finishes in fetch-completion order, not request order \u2014 restore the caller's order.
    return [results[raw_id] for raw_id in message_ids]

def sync_single_gmail_account(user_id, account, fetch_limit, label_catalog, category_catalog):
    """Fetch and store new emails for one connected Gmail account, using Gmail's history API to ask
    directly "what's new since last time" instead of re-listing recent messages and diffing against a
    watermark. Falls back to a full bootstrap re-list on the very first sync, or if Gmail has aged out
    the stored history baseline. Existing rows are never re-touched \u2014 status is user-driven going
    forward, not re-derived from the provider."""
    provider_email = account['email']
    last_history_id = account.get('last_history_id')

    new_message_ids = None
    new_history_id = None
    if last_history_id:
        try:
            new_message_ids, new_history_id = _gmail_history_message_ids(user_id, account, last_history_id)
        except GmailHistoryExpiredError as e:
            print(f"Gmail history expired for {provider_email}, re-bootstrapping: {e}")

    if new_message_ids is None:
        new_message_ids = _gmail_bootstrap_message_ids(user_id, account, fetch_limit)
        new_history_id = _gmail_current_history_id(user_id, account)

    if not new_message_ids:
        account['last_history_id'] = new_history_id
        return [], 0

    # format=full so we can pull attachment metadata from payload.parts.
    # The body content it returns is used only to derive attachments/snippet and then discarded \u2014 not stored.
    label_name_cache = {'map': None}  # lazily resolved, shared across this account's messages
    pending = []
    email_datas = _fetch_gmail_messages_full(user_id, account, new_message_ids)
    for raw_id, email_data in zip(new_message_ids, email_datas):
        email_id = f"gmail#{provider_email}#{raw_id}"  # provider-prefixed, keeps emailId unique across accounts/providers
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
        # Gmail's SENT label is authoritative; the from-address check is just a safety net for the
        # rare message that's missing it (e.g. sent from a different client before this label existed).
        direction = 'sent' if ('SENT' in label_ids or from_address.lower() == provider_email.lower()) else 'received'
        provider_labels = derive_gmail_provider_labels(label_ids, user_id, account, label_name_cache)
        attachments = extract_gmail_attachments(payload)
        in_reply_to = next((h['value'] for h in headers if h['name'] == 'In-Reply-To'), '')
        message_id = next((h['value'] for h in headers if h['name'].lower() == 'message-id'), '')
        received_at = gmail_internal_date_to_iso(email_data.get('internalDate'))
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
            'direction':     direction,
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

    new_emails = finalize_batch(user_id, pending, label_catalog, category_catalog)
    # historyId always moves forward regardless of whether this batch found any new mail — it's our
    # sync cursor, not a "did we find anything" flag. last_synced_at is kept only for display/back-compat.
    account['last_history_id'] = new_history_id
    if new_emails:
        account['last_synced_at'] = max(p['receivedAt'] for p in pending if p.get('receivedAt'))
    return new_emails, len(new_emails)

class OutlookDeltaExpiredError(Exception):
    """Raised when Microsoft Graph rejects a stored delta link (410 Gone) — the caller must fall
    back to a full bootstrap re-list and try to re-establish a fresh delta link."""
    pass

_OUTLOOK_SELECT_FIELDS = (
    'subject,from,toRecipients,ccRecipients,replyTo,internetMessageId,bodyPreview,isRead,'
    'conversationId,receivedDateTime,hasAttachments,internetMessageHeaders,body,categories'
)

def _outlook_bootstrap_messages(user_id, account, fetch_limit, last_synced_at, last_synced_message_id):
    """First-ever sync for this account, or the stored delta link expired: re-list the most recent
    messages and cut off at the last watermark we've seen — the same approach Outlook sync has
    always used, kept as the fallback path underneath the delta-based sync below."""
    list_url = (
        f'https://graph.microsoft.com/v1.0/me/messages?$top={fetch_limit}'
        f'&$orderby=receivedDateTime%20desc'
        f'&$select={_OUTLOOK_SELECT_FIELDS}'
    )
    if last_synced_at:
        # urllib.request rejects literal spaces in URLs ("URL can't contain control characters"),
        # so the OData filter's spaces have to be percent-encoded rather than written literally.
        buffered = (datetime.fromisoformat(last_synced_at) - timedelta(minutes=1)).strftime('%Y-%m-%dT%H:%M:%SZ')
        list_url += f'&$filter=receivedDateTime%20ge%20{buffered}'

    list_data = api_get(user_id, account, list_url)
    messages = list_data.get('value', [])

    new_messages = []
    for msg in messages:
        if msg['id'] == last_synced_message_id:
            break
        new_messages.append(msg)
    return new_messages

def _outlook_try_establish_delta_link(user_id, account, last_synced_at):
    """Best-effort: seeds an incremental delta cursor scoped to roughly the same recent window as the
    bootstrap fetch, so future syncs can ask Graph "what changed" instead of re-listing everything.
    Must request the same $select as genuine delta fetches use (_OUTLOOK_SELECT_FIELDS) — a delta
    link carries forward the $select scope of the request that created it, so seeding it with a
    narrower field set would silently starve every future call through this link of the fields we
    actually need. Any pages walked through here just to reach the deltaLink token are discarded, so
    this is a one-time bootstrap cost, not a per-sync one. Not every Graph delta/$filter combination
    is guaranteed to behave identically across tenants, so any failure here is swallowed — the
    account simply keeps using the (still correct, just slower) bootstrap re-list path until this
    succeeds on a later sync."""
    try:
        url = f"https://graph.microsoft.com/v1.0/me/mailFolders('inbox')/messages/delta?$select={_OUTLOOK_SELECT_FIELDS}"
        if last_synced_at:
            buffered = (datetime.fromisoformat(last_synced_at) - timedelta(minutes=1)).strftime('%Y-%m-%dT%H:%M:%SZ')
            url += f'&$filter=receivedDateTime%20ge%20{buffered}'

        for _ in range(10):  # circuit breaker: never chase more than 10 pages trying to seed this
            data = api_get(user_id, account, url)
            delta_link = data.get('@odata.deltaLink')
            if delta_link:
                return delta_link
            url = data.get('@odata.nextLink')
            if not url:
                return None
        return None
    except Exception as e:
        print(f"Could not establish Outlook delta link for {account.get('email')}, staying on bootstrap sync: {e}")
        return None

def _outlook_delta_messages(user_id, account, delta_link):
    """Follows a stored delta link to get just what changed since last time. Graph's delta reports
    updates (e.g. read/unread toggles) as well as adds, so the caller still filters to genuinely new
    mail by received date (see sync_single_outlook_account) rather than treating every item as new."""
    messages = []
    url = delta_link
    while True:
        try:
            data = api_get(user_id, account, url)
        except urllib.error.HTTPError as e:
            if e.code == 410:
                raise OutlookDeltaExpiredError("Stored Outlook delta link is no longer valid") from e
            raise
        messages.extend(m for m in data.get('value', []) if '@removed' not in m)
        new_delta_link = data.get('@odata.deltaLink')
        if new_delta_link:
            return messages, new_delta_link
        url = data.get('@odata.nextLink')
        if not url:
            # Shouldn't happen per Graph's contract (every page ends in either nextLink or
            # deltaLink), but don't loop forever if it somehow does.
            return messages, delta_link

def _fetch_outlook_attachments_parallel(user_id, account, message_ids):
    """Fetches attachment metadata for a batch of messages concurrently, scoped to this one account
    only — same per-account boundary as _fetch_gmail_messages_full and the AI classification batching
    (finalize_batch never mixes multiple accounts' emails into one batch either)."""
    if not message_ids:
        return {}
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=_PROVIDER_FETCH_CONCURRENCY) as executor:
        future_to_id = {
            executor.submit(fetch_outlook_attachment_metadata, user_id, account, msg_id): msg_id
            for msg_id in message_ids
        }
        for future in concurrent.futures.as_completed(future_to_id):
            results[future_to_id[future]] = future.result()
    return results

def sync_single_outlook_account(user_id, account, fetch_limit, label_catalog, category_catalog):
    """Fetch and store new emails for one connected Outlook account via Microsoft Graph. Uses a
    delta link (Graph's "what changed" cursor) once one has been established, instead of re-listing
    recent messages and diffing against a watermark on every sync; falls back to that bootstrap
    re-list on the very first sync, or whenever the stored delta link has expired."""
    provider_email = account['email']
    last_synced_at = account.get('last_synced_at')
    last_synced_message_id = account.get('last_synced_message_id')
    delta_link = account.get('outlook_delta_link')

    new_messages = None
    new_delta_link = None
    if delta_link:
        try:
            candidate_messages, new_delta_link = _outlook_delta_messages(user_id, account, delta_link)
            # Delta reports updates (e.g. read/unread toggles) too, not just new mail — only keep
            # items that are actually newer than our last watermark.
            new_messages = [
                m for m in candidate_messages
                if not last_synced_at or (m.get('receivedDateTime') or '') > last_synced_at
            ]
        except OutlookDeltaExpiredError as e:
            print(f"Outlook delta link expired for {provider_email}, re-bootstrapping: {e}")

    if new_messages is None:
        new_messages = _outlook_bootstrap_messages(user_id, account, fetch_limit, last_synced_at, last_synced_message_id)
        new_delta_link = _outlook_try_establish_delta_link(user_id, account, last_synced_at)

    if not new_messages:
        if new_delta_link:
            account['outlook_delta_link'] = new_delta_link
        return [], 0

    messages_with_attachments = [m['id'] for m in new_messages if m.get('hasAttachments')]
    attachments_by_id = _fetch_outlook_attachments_parallel(user_id, account, messages_with_attachments)

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
        # Graph's /me/messages has no structural "sent" label like Gmail, so direction is derived
        # purely from whether the message's own account sent it.
        direction = 'sent' if from_address.get('address', '').lower() == provider_email.lower() else 'received'

        attachments = attachments_by_id.get(msg['id'], [])

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
            'direction':     direction,
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

    new_emails = finalize_batch(user_id, pending, label_catalog, category_catalog)
    if new_delta_link:
        account['outlook_delta_link'] = new_delta_link
    if new_emails:
        # Delta results aren't guaranteed date-ordered the way the old plain list call was, so find
        # the newest explicitly rather than assuming new_messages[0] is it.
        newest_message = max(new_messages, key=lambda m: m.get('receivedDateTime') or '')
        account['last_synced_message_id'] = newest_message['id']
        account['last_synced_at'] = newest_message.get('receivedDateTime') or account.get('last_synced_at')
    return new_emails, len(new_emails)

def finalize_batch(user_id, pending, label_catalog, category_catalog):
    """Shared tail for both Gmail and Outlook sync: classifies each new email (summary + Maily labels
    + smart-category flag) in one combined batched call, persists it, then runs whatever got flagged
    for a smart category through extraction + matching/merging (see process_smart_category_candidates)."""
    if not pending:
        return []

    classifications = classify_emails_batch(
        [{'subject': p['subject'], 'snippet': _email_ai_text(p)} for p in pending], label_catalog, category_catalog
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
        process_smart_category_candidates(user_id, candidates, category_catalog)

    return new_emails

def sync_single_account(user_id, account, fetch_limit, label_catalog, category_catalog):
    """Fetch and store emails for one connected email account, dispatching by provider."""
    if account.get('provider') == 'outlook':
        return sync_single_outlook_account(user_id, account, fetch_limit, label_catalog, category_catalog)
    return sync_single_gmail_account(user_id, account, fetch_limit, label_catalog, category_catalog)


def sync_user_emails(user_id, user_record):
    """Syncs all connected email accounts (Gmail + Outlook) for a user. Used by both /sync and EventBridge.
    Returns new_count — how many emails were newly processed this run. Each provider sync mutates its
    account dict's watermark fields in place (last_synced_message_id/last_synced_at, plus the
    incremental-sync cursors last_history_id for Gmail and outlook_delta_link for Outlook — these
    advance even on a sync that finds zero new mail, since they're just "how far we've looked," not
    a "did we find anything" flag); if anything advanced, we persist the whole email_accounts list
    back in a single write rather than one write per account."""
    accounts = user_record.get('email_accounts', [])
    fetch_limit = int(user_record.get('email_fetch_limit', 10))
    label_catalog = get_label_catalog(user_id)  # computed once per user, reused across all their accounts
    category_catalog = get_category_type_catalog(user_id)  # same idea: built-ins + this user's custom types

    def _watermark_snapshot(account):
        return (
            account.get('last_synced_message_id'),
            account.get('last_synced_at'),
            account.get('last_history_id'),
            account.get('outlook_delta_link'),
        )

    new_count = 0
    watermark_advanced = False
    for account in accounts:
        if not account.get('access_token'):
            continue
        # One account's failure (e.g. a stale refresh token that no longer covers a scope added since
        # it was connected — see refresh_google_access_token/refresh_microsoft_access_token) must not
        # block every other connected account from syncing. Isolated per account, not per user.
        try:
            before = _watermark_snapshot(account)
            _, count = sync_single_account(user_id, account, fetch_limit, label_catalog, category_catalog)
            new_count += count
            if _watermark_snapshot(account) != before:
                watermark_advanced = True
        except Exception as e:
            print(f"Failed to sync {account.get('provider')} account {account.get('email')} for user {user_id}: {e}")

    if watermark_advanced:
        dynamodb.Table('Maily-Users').update_item(
            Key={'userId': user_id},
            UpdateExpression='SET email_accounts = :accounts',
            ExpressionAttributeValues={':accounts': accounts}
        )

    return new_count

def process_smart_category_candidates(user_id, candidates, category_catalog):
    """Extracts structured fields for each smart-category-flagged email and merges it into a tracked
    Maily-CategoryItems row (creating one if nothing existing matches). Grouped by category type,
    processed oldest-email-first so e.g. an "order confirmed" email creates the item before a later
    "shipped" email tries to merge into it. See category_catalog for the per-category schema/matchKeys."""
    by_type = {}
    for item, category_type in candidates:
        by_type.setdefault(category_type, []).append(item)

    for category_type, items in by_type.items():
        schema = category_catalog.get(category_type)
        if not schema:
            continue  # category type no longer exists (e.g. deleted between classify and here)

        items.sort(key=lambda i: i.get('receivedAt', ''))
        extracted_list = _extract_fields_with_schema(
            [{'subject': i['subject'], 'snippet': _email_ai_text(i)} for i in items], schema
        )

        match_keys = schema.get('matchKeys') or []
        existing_items = []
        if match_keys:
            existing_items = category_items_table.query(
                IndexName='categoryType-index',
                KeyConditionExpression=boto3.dynamodb.conditions.Key('userId').eq(user_id) &
                                        boto3.dynamodb.conditions.Key('categoryType').eq(category_type)
            ).get('Items', [])

        for item, extracted in zip(items, extracted_list):
            match_and_save_category_item(user_id, item, extracted, category_type, schema, existing_items)

def match_and_save_category_item(user_id, item, extracted, category_type, schema, existing_items):
    """Matches one email's already-extracted fields against existing_items — a list this function may
    append a freshly-created item onto, so later calls against the same list (e.g. later emails in the
    same sync batch) can match against it too — then persists the tracked category item and sets
    categoryItemId on the source email. Shared by sync-time batch processing and manual single-email
    classification (POST /email-classify), so a manual assignment goes through the exact same real
    extraction/matching pipeline as an AI-flagged one, not just a label slapped on."""
    match_keys = schema.get('matchKeys') or []
    key_mode = schema.get('keyMode') or 'OR'
    matched = None
    match_method = 'no-match-keys'
    if match_keys:
        matched = _find_deterministic_match(existing_items, extracted, match_keys, key_mode)
        match_method = 'deterministic' if matched else None
        if not matched and existing_items:
            matched = ai_match_category_item(extracted, item.get('summary', ''), existing_items, category_type, {category_type: schema})
            match_method = 'ai' if matched else 'ai-no-match'

    print(f"category-match emailId={item['emailId']} extracted={extracted} "
          f"result={('existing:' + matched['itemId']) if matched else 'new-item'} via={match_method}")

    now = datetime.now(timezone.utc).isoformat()
    received_at = item.get('receivedAt') or now

    if matched:
        target = matched
        # A trashed item's row is a tombstone only — matching still finds it (so this email doesn't
        # spawn a duplicate card) but nothing about it changes; a done item keeps absorbing new data
        # normally, it just doesn't move back to active because of it.
        if target.get('manualState') != 'trashed':
            _merge_into_category_item(target, extracted, item['emailId'], received_at, now, schema)
            category_items_table.put_item(Item=target)
    else:
        target = {
            'userId': user_id,
            'itemId': f"{category_type}#{uuid.uuid4().hex[:12]}",
            'categoryType': category_type,
            'fields': {k: v for k, v in extracted.items() if v},
            'manualState': None,
            'contributingEmailIds': [item['emailId']],
            'createdAt': now,
            'updatedAt': now,
            'lastUpdatedFromEmailAt': received_at,
        }
        if match_keys:
            existing_items.append(target)  # so later emails in this same batch can also match against it
        category_items_table.put_item(Item=target)

    table.update_item(
        Key={'userId': user_id, 'emailId': item['emailId']},
        UpdateExpression='SET categoryItemId = :c',
        ExpressionAttributeValues={':c': target['itemId']}
    )
    return target

def _normalize_key_value(value):
    """Consistent string form for key comparison/hashing — same real value shouldn't fail to match
    just because one email had different case/whitespace than another (e.g. "Acme.com" vs "acme.com ")."""
    return str(value).strip().lower() if value not in (None, '') else None

def _composite_key(fields, match_keys):
    """Joins every match-key field's normalized value into one string, or None if any is missing —
    used for AND-mode matching, where all listed fields must be present and equal together."""
    values = [_normalize_key_value(fields.get(k)) for k in match_keys]
    if any(v is None for v in values):
        return None
    return "\x1f".join(values)

def _find_deterministic_match(existing_items, extracted, match_keys, key_mode='OR'):
    if key_mode == 'AND':
        composite = _composite_key(extracted, match_keys)
        if composite is None:
            return None
        for existing in existing_items:
            if _composite_key(existing.get('fields', {}), match_keys) == composite:
                return existing
        return None
    # OR mode (default): any single match-key field matching an existing item's value is enough.
    for key in match_keys:
        value = extracted.get(key)
        if not value:
            continue
        for existing in existing_items:
            if _normalize_key_value(existing.get('fields', {}).get(key)) == _normalize_key_value(value):
                return existing
    return None

def _merge_into_category_item(target, extracted, email_id, received_at, now, schema):
    fields = target.setdefault('fields', {})
    sticky_keys = {f['key'] for f in schema.get('fields', []) if f.get('sticky')}

    # Sticky fields are set once (first email to report a value wins) and never overwritten after
    # that, regardless of email recency — e.g. "merchant" shouldn't flip if a later email omits it.
    for key in sticky_keys:
        if not fields.get(key) and extracted.get(key):
            fields[key] = extracted[key]

    # Non-sticky fields keep the original recency-gated overwrite behavior: only let this email's
    # values in if it's at least as recent as whatever last updated the item — guards against an
    # out-of-order email regressing e.g. delivered -> shipped.
    if received_at >= target.get('lastUpdatedFromEmailAt', ''):
        for key, value in extracted.items():
            if value and key not in sticky_keys:
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
        # Not necessarily Google — could be Gmail or Outlook, this is whatever provider call was in
        # flight outside the per-account try/except in sync_user_emails (e.g. get_user_emails itself).
        error_body = e.read().decode('utf-8')
        print(f"Email provider API error: {error_body}")
        return {
            "statusCode": e.code,
            "body": json.dumps({
                "error": "Email provider authentication failed"
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

def fetch_provider_email_body(user_id, email_id):
    """Live-fetches (text, html) body for one already-synced email directly from its provider — never
    persisted, so this is the only way to get the full body back once an email is already in DynamoDB
    (rows only ever store the short auto-generated preview as `content`). Returns (None, None) if the
    emailId is malformed or its account is no longer connected — callers should fall back gracefully,
    not treat that as fatal."""
    parsed = parse_email_id(email_id)
    if not parsed:
        return None, None
    provider, provider_email, message_id = parsed

    account = find_account(user_id, provider, provider_email)
    if not account:
        return None, None

    if provider == 'outlook':
        message = api_get(
            user_id, account,
            f'https://graph.microsoft.com/v1.0/me/messages/{message_id}?$select=body'
        )
        body = message.get('body', {})
        content = body.get('content')
        content_type = body.get('contentType', 'text')
        return (content if content_type == 'text' else None), (content if content_type == 'html' else None)

    email_data = api_get(
        user_id, account,
        f'https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}?format=full'
    )
    return extract_gmail_bodies(email_data.get('payload', {}))

def _richer_email_text(user_id, email_row):
    """Best-effort richer text for an already-stored email row (which never has bodyText/bodyHtml —
    those are only present on a fresh sync's in-memory pending dict, see _email_ai_text). Live-fetches
    the body from its provider and falls back to the row's stored short preview on any failure (e.g. a
    revoked token) rather than blocking the caller — used for manual single-email classification and
    the Wizard's reference-email preview, where accuracy matters more than the extra API call cost."""
    try:
        text_body, html_body = fetch_provider_email_body(user_id, email_row.get('emailId'))
    except Exception as e:
        print(f"Could not live-fetch body for {email_row.get('emailId')}: {e}")
        text_body, html_body = None, None
    body = (text_body or html_to_text(html_body or '')).strip()
    text = _strip_urls(body or email_row.get('content', ''))
    return text[:_AI_INPUT_MAX_CHARS]

def handle_get_email_body(event):
    """Fetches the full body (text + html) of one email directly from the provider. Never stored in DynamoDB."""
    try:
        user_id = get_authorized_user_id(event)
        if not user_id:
            return {"statusCode": 401, "body": json.dumps({"error": "Unauthorized. Could not identify user."})}

        query_params = event.get('queryStringParameters') or {}
        email_id = query_params.get('emailId')
        if not parse_email_id(email_id):
            return {"statusCode": 400, "body": json.dumps({"error": "Missing or invalid emailId"})}

        provider, provider_email, _ = parse_email_id(email_id)
        if not find_account(user_id, provider, provider_email):
            return {"statusCode": 404, "body": json.dumps({"error": f"{provider} account {provider_email} is not connected"})}

        text_body, html_body = fetch_provider_email_body(user_id, email_id)

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"emailId": email_id, "text": text_body, "html": html_body}, ensure_ascii=False)
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

def _serialize_category_item(item, category_catalog):
    is_complete, is_at_risk = evaluate_category_item_state(item.get('categoryType'), item.get('fields', {}), category_catalog)
    manual_state = item.get('manualState')
    # A manual mark-done/trash always wins over the computed rule state, and never gets reset by new
    # data merging in — that's what keeps a manually-completed card in Done even as it keeps updating.
    effective_state = manual_state if manual_state in ('done', 'trashed') else ('done' if is_complete else 'active')
    return {**item, 'isComplete': is_complete, 'isAtRisk': is_at_risk, 'effectiveState': effective_state}

def handle_list_category_items(event):
    """Lists this user's tracked smart-category items, optionally filtered to one category type and to
    an active/done state (default active). Trashed items are never returned here — soft-deleted, kept
    only so future matching emails don't spawn a duplicate card, never surfaced in any list view."""
    try:
        user_id = get_authorized_user_id(event)
        if not user_id:
            return {"statusCode": 401, "body": json.dumps({"error": "Unauthorized. Could not identify user."})}

        category_catalog = get_category_type_catalog(user_id)
        query_params = event.get('queryStringParameters') or {}
        category_type = query_params.get('type')
        state_filter = query_params.get('state') if query_params.get('state') in ('active', 'done') else 'active'

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

        items = [_serialize_category_item(i, category_catalog) for i in items]
        items = [i for i in items if i['effectiveState'] == state_filter]

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"items": items}, ensure_ascii=False, cls=DecimalEncoder)
        }
    except Exception as e:
        print(f"Error listing category items: {str(e)}")
        return {"statusCode": 500, "body": json.dumps({"message": "Internal server error while listing smart categories"})}

def handle_update_category_item_state(event):
    """Manual card controls: mark done, trash, or restore to active (PATCH body {"manualState": "done"
    | "trashed" | null}). This is a soft delete only — a trashed item's row stays in the database with
    its key fields intact, so a later matching email still finds it (and doesn't spawn a duplicate card)
    even though it's excluded from every list view. Restoring from "trashed" is intentionally not
    exposed as a UI button today, but the handler itself doesn't enforce that — it's a frontend choice,
    easy to change later without a backend migration."""
    try:
        user_id = get_authorized_user_id(event)
        if not user_id:
            return {"statusCode": 401, "body": json.dumps({"error": "Unauthorized. Could not identify user."})}

        query_params = event.get('queryStringParameters') or {}
        item_id = query_params.get('itemId')
        if not item_id:
            return {"statusCode": 400, "body": json.dumps({"error": "Missing required query param: itemId"})}

        body = json.loads(event.get('body', '{}'))
        if 'manualState' not in body:
            return {"statusCode": 400, "body": json.dumps({"error": "Missing required field: manualState"})}
        manual_state = body.get('manualState')
        if manual_state not in (None, 'done', 'trashed'):
            return {"statusCode": 400, "body": json.dumps({"error": "manualState must be \"done\", \"trashed\", or null"})}

        existing = category_items_table.get_item(Key={'userId': user_id, 'itemId': item_id}).get('Item')
        if not existing:
            return {"statusCode": 404, "body": json.dumps({"error": "Smart category item not found"})}

        now = datetime.now(timezone.utc).isoformat()
        if manual_state is None:
            category_items_table.update_item(
                Key={'userId': user_id, 'itemId': item_id},
                UpdateExpression='REMOVE manualState SET updatedAt = :u',
                ExpressionAttributeValues={':u': now}
            )
        else:
            category_items_table.update_item(
                Key={'userId': user_id, 'itemId': item_id},
                UpdateExpression='SET manualState = :m, updatedAt = :u',
                ExpressionAttributeValues={':m': manual_state, ':u': now}
            )

        updated = category_items_table.get_item(Key={'userId': user_id, 'itemId': item_id}).get('Item')
        category_catalog = get_category_type_catalog(user_id)
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"item": _serialize_category_item(updated, category_catalog)}, ensure_ascii=False, cls=DecimalEncoder)
        }
    except Exception as e:
        print(f"Error updating category item state: {str(e)}")
        return {"statusCode": 500, "body": json.dumps({"message": "Internal server error while updating smart category item"})}

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

        category_catalog = get_category_type_catalog(user_id)
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"item": _serialize_category_item(item, category_catalog), "emails": emails}, ensure_ascii=False, cls=DecimalEncoder)
        }
    except Exception as e:
        print(f"Error fetching category item: {str(e)}")
        return {"statusCode": 500, "body": json.dumps({"message": "Internal server error while fetching smart category item"})}

def handle_list_category_types(event):
    """Lists this user's full category-type catalog (built-ins + their own custom ones), each tagged
    with an id and isBuiltIn — this is what the frontend uses instead of a hardcoded metadata table."""
    try:
        user_id = get_authorized_user_id(event)
        if not user_id:
            return {"statusCode": 401, "body": json.dumps({"error": "Unauthorized. Could not identify user."})}

        custom = category_types_table.query(
            KeyConditionExpression=boto3.dynamodb.conditions.Key('userId').eq(user_id)
        ).get('Items', [])

        category_types = [
            {**_category_type_row_to_schema(schema), 'id': key, 'isBuiltIn': True}
            for key, schema in CATEGORY_TYPES.items()
        ] + [
            {**_category_type_row_to_schema(row), 'id': row['categoryTypeId'], 'isBuiltIn': False}
            for row in custom
        ]

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"categoryTypes": category_types}, ensure_ascii=False, cls=DecimalEncoder)
        }
    except Exception as e:
        print(f"Error listing category types: {str(e)}")
        return {"statusCode": 500, "body": json.dumps({"message": "Internal server error while listing category types", "error": str(e)})}

def _build_category_generation_prompt(description, reference_emails, current_draft, instruction):
    type_list = ", ".join(sorted(CATEGORY_FIELD_TYPES))
    rule_help = (
        'A rule is a FLAT JSON object with a top-level "type" key set to exactly one of these three '
        'strings — never nest the rule\'s other fields under a key named after the type itself. '
        'Exactly one of these three shapes, verbatim: '
        '{"type": "field_equals", "field": "<fieldKey>", "values": ["<value>", ...]} (true once the '
        'field equals one of these values) — use for a status reaching some state, with no date '
        'involved; '
        '{"type": "date_passed", "dateField": "<fieldKey>"} (true once dateField is simply in the '
        'past, no other field involved) — use whenever "done" or "at risk" is purely about a date '
        'passing, e.g. an event\'s date has gone by, a deadline has passed; '
        'or {"type": "date_passed_without", "dateField": "<fieldKey>", "field": "<fieldKey>", '
        '"values": ["<value>", ...]} (true once dateField is in the past AND field still has not '
        'reached one of values) — use only when completion is a status, but going overdue on a date '
        'without reaching it should separately flag as at-risk, e.g. a delivery\'s estimate passing '
        'without reaching "delivered". Pick "date_passed" over "date_passed_without" whenever there is '
        'no separate status field to check — do not invent one just to fit date_passed_without\'s shape. '
        'WRONG: {"date_passed": {"dateField": "eventDate"}} — "type" must be a sibling of the other '
        'keys, not a wrapper around them.'
    )
    key_help = (
        '"matchKeys": ["fieldKey", ...] (0-4 keys that identify one tracked item — use [] if there is '
        'no natural identifier and every matching email should become its own tracked item), '
        '"keyMode": "OR" or "AND" — "OR" (default) means matching ANY ONE of matchKeys is enough to '
        'consider it the same item (e.g. a delivery\'s trackingNumber OR orderNumber, since either alone '
        'reliably identifies the order); "AND" means ALL matchKeys fields must match TOGETHER (e.g. job '
        'applications: companyDomain + roleTitle, since neither field alone is unique — many roles per '
        'company, and the same title exists at many companies). Use "AND" only when no single field is '
        'reliably unique on its own.'
    )
    format_help = (
        '"format" (optional, per field) — MUST match the field\'s own "type" or omit it entirely: '
        '"currency" or "percent" only on a "number"-type field (e.g. do not put "currency" on a '
        '"string" field — type the field as "number" instead if you want currency formatting), "url" '
        'only on a "string"-type field, "relative-date" only on a "date"-type field. Default to NOT '
        'setting a date field\'s format at all (shows the actual date) — only set "relative-date" if '
        'the user\'s own description specifically asks for a countdown/days-until framing.'
    )
    schema_shape = (
        '{"label": "...", "icon": "<one emoji>", "classifierDescription": "...", '
        '"fields": [{"key": "camelCaseKey", "label": "...", "type": "' + type_list + '", '
        '"hint": "optional", "values": ["..."] (enum fields only), "sticky": true|false (optional, '
        'means this field is set once and should never be overwritten later), ' + format_help + '}, ...], '
        + key_help + ' '
        '"titleTemplate": "e.g. {merchant} — {orderDescription}", "primaryDateField": "a date fieldKey '
        'or empty", "cardFields": ["fieldKey", ...] (1-2 keys to show on a compact summary card), '
        '"completionRule": {...} or null, "atRiskRule": {...} or null}'
    )

    no_done_field_note = (
        "Never create a field to track whether the item is done/completed/finished — that state is "
        "always computed automatically from completionRule against the other fields, and an extraction "
        "call can't reliably know it anyway (e.g. it can't know 'has this date passed yet' at the "
        "moment an email arrives). Only ask for fields whose values actually come from the email's "
        "own content."
    )

    if current_draft is not None:
        return (
            f"You are refining a draft email-category schema for a personal email assistant. "
            f"Here is the current draft:\n{json.dumps(current_draft, ensure_ascii=False)}\n\n"
            f"The user asked for this change: \"{instruction}\"\n\n"
            f"Produce a full revised draft (not a diff/patch) using at most 8 fields, chosen only from "
            f"these field types: {type_list}. Rule types, if used: {rule_help}. {no_done_field_note} "
            f'Respond with a JSON object of exactly this shape: {schema_shape}'
        )

    ref_block = ""
    if reference_emails:
        ref_lines = "\n\n".join(
            f"Example email {i + 1}:\nSubject: {e['subject']}\nContent: {e['snippet']}"
            for i, e in enumerate(reference_emails)
        )
        ref_block = f"\n\nHere are real example emails the user picked to ground this category:\n{ref_lines}"

    return (
        f"You are designing an email-category schema for a personal email assistant, from the user's "
        f"own description of what they want to track: \"{description}\"{ref_block}\n\n"
        f"Design at most 8 fields, chosen only from these field types: {type_list}. Rule types, if used "
        f"for completionRule/atRiskRule: {rule_help}. Choose matchKeys/keyMode carefully — a category "
        f"whose items can't be reliably told apart will fragment one real thing into many duplicate "
        f"cards. {no_done_field_note} "
        f'Respond with a JSON object of exactly this shape: {schema_shape}'
    )

def _call_category_generation_ai(prompt):
    api_key = get_secrets()['OPENAI_API_KEY']
    body = json.dumps({
        "model": "gpt-4.1-nano",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 1200,
        "response_format": {"type": "json_object"}
    }).encode('utf-8')
    req = urllib.request.Request('https://api.openai.com/v1/chat/completions', data=body, method='POST')
    req.add_header('Authorization', f'Bearer {api_key}')
    req.add_header('Content-Type', 'application/json')
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read().decode('utf-8'))
    raw_content = result['choices'][0]['message']['content'].strip()
    try:
        return json.loads(raw_content)
    except (json.JSONDecodeError, AttributeError):
        return {}

def handle_generate_category_draft(event):
    """Category Wizard stage 3/4: proposes a fresh draft schema from a free-text description
    (optionally grounded by up to 2 reference emails), or revises an existing draft per a follow-up
    instruction when currentDraft+instruction are given instead of description. If reference emails
    were provided, also runs real extraction against them so the wizard's live preview shows real
    data — the per-email results are returned so the client can echo them back unchanged at creation
    time (POST /category-types) without triggering a second AI call."""
    try:
        user_id = get_authorized_user_id(event)
        if not user_id:
            return {"statusCode": 401, "body": json.dumps({"error": "Unauthorized. Could not identify user."})}

        body = json.loads(event.get('body', '{}'))
        description = (body.get('description') or '').strip()
        current_draft = body.get('currentDraft')
        instruction = (body.get('instruction') or '').strip()
        reference_email_ids = (body.get('referenceEmailIds') or [])[:2]

        if not description and not (current_draft and instruction):
            return {"statusCode": 400, "body": json.dumps({"error": "Provide 'description', or 'currentDraft' + 'instruction' to refine"})}

        reference_email_rows = []
        for email_id in reference_email_ids:
            email_item = table.get_item(Key={'userId': user_id, 'emailId': email_id}).get('Item')
            if email_item:
                reference_email_rows.append(email_item)
        # Reference emails are already-synced rows with no stored body (only the short provider
        # preview) — live-fetch the real body for each so the Wizard's live preview reflects what a
        # real sync-time extraction would actually see, not a truncated snippet.
        reference_snippets = [
            {'subject': r.get('subject', ''), 'snippet': _richer_email_text(user_id, r)} for r in reference_email_rows
        ]

        prompt = _build_category_generation_prompt(description, reference_snippets, current_draft, instruction)
        raw_draft = _call_category_generation_ai(prompt)
        schema, warnings = _sanitize_category_draft(raw_draft)

        reference_results = []
        merged_preview_fields = {}
        if reference_email_rows and schema['fields']:
            extracted_list = _extract_fields_with_schema(reference_snippets, schema)
            for row, extracted in zip(reference_email_rows, extracted_list):
                reference_results.append({'emailId': row['emailId'], 'extracted': extracted})
                for k, v in extracted.items():
                    if v:
                        merged_preview_fields[k] = v

        preview_item = None
        if merged_preview_fields:
            is_complete = evaluate_category_rule(schema.get('completionRule'), merged_preview_fields)
            is_at_risk = (not is_complete) and evaluate_category_rule(schema.get('atRiskRule'), merged_preview_fields)
            preview_item = {'fields': merged_preview_fields, 'isComplete': is_complete, 'isAtRisk': is_at_risk}

        # Wizard validation gates — soft, not hard: the frontend surfaces these conversationally and
        # lets the user create anyway, it's never a server-side rejection (see handle_create_category_type,
        # which only requires at least one field).
        key_ok = bool(schema['matchKeys'])
        lifecycle_ok = bool(schema['completionRule'] or schema['atRiskRule'])
        gate_warnings = list(warnings)
        if not key_ok:
            gate_warnings.append(
                "I couldn't find a reliable way to tell separate items apart — without matchKeys, every "
                "matching email becomes its own card instead of updating one. Want to add an identifying "
                "field (or combine two fields with AND mode)?"
            )
        if not lifecycle_ok:
            gate_warnings.append(
                "This category has no completion or at-risk rule, so cards will never move to Done "
                "automatically — you can still mark them done by hand, or add a rule now."
            )
        validation = {"keyOk": key_ok, "lifecycleOk": lifecycle_ok, "warnings": gate_warnings}

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({
                "draft": schema,
                "referenceEmails": reference_results,
                "previewItem": preview_item,
                "warnings": warnings,
                "validation": validation
            }, ensure_ascii=False)
        }
    except Exception as e:
        print(f"Error generating category draft: {str(e)}")
        return {"statusCode": 500, "body": json.dumps({"message": "Internal server error while generating category draft", "error": str(e)})}

def handle_create_category_type(event):
    """Approves a wizard draft (stage 5) into a persisted custom category type. If the draft was
    generated against reference emails, the client echoes back that same generate-time extraction
    (referenceEmails, as returned by POST /category-types/generate) so those emails can be finalized
    as the new type's first tracked item(s) immediately — no extra AI call, no empty-category moment."""
    try:
        user_id = get_authorized_user_id(event)
        if not user_id:
            return {"statusCode": 401, "body": json.dumps({"error": "Unauthorized. Could not identify user."})}

        body = json.loads(event.get('body', '{}'))
        schema, warnings = _sanitize_category_draft(body.get('draft'))
        if not schema['fields']:
            return {"statusCode": 400, "body": json.dumps({"error": "Category must have at least one field", "warnings": warnings})}

        category_type_id = f"custom#{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc).isoformat()
        row = {
            'userId': user_id,
            'categoryTypeId': category_type_id,
            'schemaVersion': 1,
            'createdAt': now,
            'updatedAt': now,
            **schema,
        }
        category_types_table.put_item(Item=row)

        reference_emails = (body.get('referenceEmails') or [])[:2]
        if reference_emails:
            pairs = []
            for ref in reference_emails:
                email_id = ref.get('emailId')
                extracted = _normalize_extracted_row(ref.get('extracted'), schema)
                email_item = email_id and table.get_item(Key={'userId': user_id, 'emailId': email_id}).get('Item')
                if email_item and extracted:
                    pairs.append((email_item, extracted))
            pairs.sort(key=lambda pair: pair[0].get('receivedAt', ''))
            existing_items = []
            for email_item, extracted in pairs:
                match_and_save_category_item(user_id, email_item, extracted, category_type_id, schema, existing_items)

        # Backfill against the rest of this user's already-synced mail — without this, a brand new
        # category would only ever have the 1-2 emails picked as wizard references, even though older
        # matching mail already sitting in their inbox should show up immediately too.
        backfilled_count = backfill_category_type(user_id, category_type_id, schema)

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({
                "categoryType": {**schema, "id": category_type_id, "isBuiltIn": False, "schemaVersion": 1},
                "warnings": warnings,
                "backfilledCount": backfilled_count
            }, ensure_ascii=False)
        }
    except Exception as e:
        print(f"Error creating category type: {str(e)}")
        return {"statusCode": 500, "body": json.dumps({"message": "Internal server error while creating category type", "error": str(e)})}

def handle_replace_category_type(event):
    """Wizard 'replace' mode: overwrites a custom category type's schema in place (same id, bumped
    schemaVersion). Existing tracked Maily-CategoryItems rows of this type are left untouched — only
    future extraction/matching uses the new schema, per the immutable-structure design (a full wizard
    re-run is required for any structural change; PATCH only covers non-structural tweaks)."""
    try:
        user_id = get_authorized_user_id(event)
        if not user_id:
            return {"statusCode": 401, "body": json.dumps({"error": "Unauthorized. Could not identify user."})}

        body = json.loads(event.get('body', '{}'))
        category_type_id = body.get('categoryTypeId')
        if not category_type_id or not category_type_id.startswith('custom#'):
            return {"statusCode": 400, "body": json.dumps({"error": "categoryTypeId must refer to a custom category type"})}

        existing = category_types_table.get_item(Key={'userId': user_id, 'categoryTypeId': category_type_id}).get('Item')
        if not existing:
            return {"statusCode": 404, "body": json.dumps({"error": "Category type not found"})}

        schema, warnings = _sanitize_category_draft(body.get('draft'))
        if not schema['fields']:
            return {"statusCode": 400, "body": json.dumps({"error": "Category must have at least one field", "warnings": warnings})}

        now = datetime.now(timezone.utc).isoformat()
        new_version = int(existing.get('schemaVersion', 1)) + 1
        row = {
            'userId': user_id,
            'categoryTypeId': category_type_id,
            'schemaVersion': new_version,
            'createdAt': existing.get('createdAt', now),
            'updatedAt': now,
            **schema,
        }
        category_types_table.put_item(Item=row)

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({
                "categoryType": {**schema, "id": category_type_id, "isBuiltIn": False, "schemaVersion": new_version},
                "warnings": warnings
            }, ensure_ascii=False)
        }
    except Exception as e:
        print(f"Error replacing category type: {str(e)}")
        return {"statusCode": 500, "body": json.dumps({"message": "Internal server error while replacing category type", "error": str(e)})}

def handle_patch_category_type(event):
    """Lightweight, structure-preserving edits only: append values to an existing enum field, and/or
    append more keyword text to the classifier description or one field's hint (to help the AI catch
    more matching emails). Anything structural (add/remove/retype a field, change matchKeys/rules)
    is out of scope here by design — that requires PUT (the wizard's 'replace' mode)."""
    try:
        user_id = get_authorized_user_id(event)
        if not user_id:
            return {"statusCode": 401, "body": json.dumps({"error": "Unauthorized. Could not identify user."})}

        body = json.loads(event.get('body', '{}'))
        category_type_id = body.get('categoryTypeId')
        if not category_type_id or not category_type_id.startswith('custom#'):
            return {"statusCode": 400, "body": json.dumps({"error": "categoryTypeId must refer to a custom category type"})}

        existing = category_types_table.get_item(Key={'userId': user_id, 'categoryTypeId': category_type_id}).get('Item')
        if not existing:
            return {"statusCode": 404, "body": json.dumps({"error": "Category type not found"})}

        fields = existing.get('fields', [])
        changed = False

        add_enum_values = body.get('addEnumValues') or {}
        if add_enum_values.get('fieldKey'):
            for f in fields:
                if f['key'] == add_enum_values['fieldKey'] and f.get('type') == 'enum':
                    new_values = [str(v).strip() for v in (add_enum_values.get('values') or []) if str(v).strip()]
                    existing_values = set(f.get('values', []))
                    additions = [v for v in new_values if v not in existing_values]
                    if additions:
                        f['values'] = f.get('values', []) + additions
                        changed = True

        classifier_description = existing.get('classifierDescription', '')
        append_classifier_hint = (body.get('appendClassifierHint') or '').strip()
        if append_classifier_hint:
            classifier_description = (classifier_description + ' ' + append_classifier_hint).strip()[:500]
            changed = True

        append_field_hint = body.get('appendFieldHint') or {}
        if append_field_hint.get('fieldKey'):
            hint_text = (append_field_hint.get('hint') or '').strip()
            if hint_text:
                for f in fields:
                    if f['key'] == append_field_hint['fieldKey']:
                        f['hint'] = ((f.get('hint') or '') + ' ' + hint_text).strip()[:300]
                        changed = True

        if not changed:
            return {"statusCode": 400, "body": json.dumps({"error": "Nothing to update — PATCH only appends enum values or hint text"})}

        now = datetime.now(timezone.utc).isoformat()
        category_types_table.update_item(
            Key={'userId': user_id, 'categoryTypeId': category_type_id},
            UpdateExpression='SET #f = :f, classifierDescription = :cd, updatedAt = :u',
            ExpressionAttributeNames={'#f': 'fields'},
            ExpressionAttributeValues={':f': fields, ':cd': classifier_description, ':u': now}
        )

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"id": category_type_id, "fields": fields, "classifierDescription": classifier_description}, ensure_ascii=False)
        }
    except Exception as e:
        print(f"Error patching category type: {str(e)}")
        return {"statusCode": 500, "body": json.dumps({"message": "Internal server error while patching category type", "error": str(e)})}

def handle_delete_category_type(event):
    """Deletes a custom category type and cascades to delete all its tracked Maily-CategoryItems rows
    (mirrors handle_disconnect_account's email cascade). Emails that reference the deleted type via
    categoryItemId are left with a dangling reference — same accepted v1 limitation already documented
    for label deletion."""
    try:
        user_id = get_authorized_user_id(event)
        if not user_id:
            return {"statusCode": 401, "body": json.dumps({"error": "Unauthorized. Could not identify user."})}

        body = json.loads(event.get('body', '{}'))
        category_type_id = body.get('categoryTypeId')
        if not category_type_id or not category_type_id.startswith('custom#'):
            return {"statusCode": 400, "body": json.dumps({"error": "categoryTypeId must refer to a custom category type"})}

        category_types_table.delete_item(Key={'userId': user_id, 'categoryTypeId': category_type_id})

        items = category_items_table.query(
            IndexName='categoryType-index',
            KeyConditionExpression=boto3.dynamodb.conditions.Key('userId').eq(user_id) &
                                    boto3.dynamodb.conditions.Key('categoryType').eq(category_type_id)
        ).get('Items', [])
        with category_items_table.batch_writer() as batch:
            for item in items:
                batch.delete_item(Key={'userId': user_id, 'itemId': item['itemId']})

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"message": "Category type deleted", "id": category_type_id})
        }
    except Exception as e:
        print(f"Error deleting category type: {str(e)}")
        return {"statusCode": 500, "body": json.dumps({"message": "Internal server error while deleting category type", "error": str(e)})}

def handle_list_travel_trips(event):
    """Lists this user's trip wrappers for the built-in Travel category. Which Maily-CategoryItems rows
    belong to a trip is computed client-side (an item's startDate falling inside the trip's date range)
    — this just returns the trip definitions (name + date range) themselves."""
    try:
        user_id = get_authorized_user_id(event)
        if not user_id:
            return {"statusCode": 401, "body": json.dumps({"error": "Unauthorized. Could not identify user."})}

        trips = travel_trips_table.query(
            KeyConditionExpression=boto3.dynamodb.conditions.Key('userId').eq(user_id)
        ).get('Items', [])
        trips.sort(key=lambda t: t.get('startDate', ''))

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"trips": trips}, ensure_ascii=False, cls=DecimalEncoder)
        }
    except Exception as e:
        print(f"Error listing travel trips: {str(e)}")
        return {"statusCode": 500, "body": json.dumps({"message": "Internal server error while listing travel trips"})}

def handle_create_travel_trip(event):
    try:
        user_id = get_authorized_user_id(event)
        if not user_id:
            return {"statusCode": 401, "body": json.dumps({"error": "Unauthorized. Could not identify user."})}

        body = json.loads(event.get('body', '{}'))
        name = str(body.get('name') or '').strip()[:100]
        start_date = str(body.get('startDate') or '').strip()[:10]
        end_date = str(body.get('endDate') or '').strip()[:10]
        if not name or not start_date or not end_date:
            return {"statusCode": 400, "body": json.dumps({"error": "name, startDate, and endDate are all required"})}
        if end_date < start_date:
            return {"statusCode": 400, "body": json.dumps({"error": "endDate must not be before startDate"})}

        now = datetime.now(timezone.utc).isoformat()
        trip = {
            'userId': user_id,
            'tripId': f"trip#{uuid.uuid4().hex[:12]}",
            'name': name,
            'startDate': start_date,
            'endDate': end_date,
            'createdAt': now,
            'updatedAt': now,
        }
        travel_trips_table.put_item(Item=trip)

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"trip": trip}, ensure_ascii=False)
        }
    except Exception as e:
        print(f"Error creating travel trip: {str(e)}")
        return {"statusCode": 500, "body": json.dumps({"message": "Internal server error while creating travel trip"})}

def handle_delete_travel_trip(event):
    """Deletes a trip wrapper only — never touches the underlying Maily-CategoryItems rows. Since trip
    membership is computed (not stored), items that were grouped under this trip simply fall back into
    the "waiting for a trip" bucket on the next render, nothing to migrate."""
    try:
        user_id = get_authorized_user_id(event)
        if not user_id:
            return {"statusCode": 401, "body": json.dumps({"error": "Unauthorized. Could not identify user."})}

        body = json.loads(event.get('body', '{}'))
        trip_id = body.get('tripId')
        if not trip_id:
            return {"statusCode": 400, "body": json.dumps({"error": "Missing required field: tripId"})}

        travel_trips_table.delete_item(Key={'userId': user_id, 'tripId': trip_id})

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"message": "Trip deleted", "tripId": trip_id})
        }
    except Exception as e:
        print(f"Error deleting travel trip: {str(e)}")
        return {"statusCode": 500, "body": json.dumps({"message": "Internal server error while deleting travel trip"})}

def handle_email_classify(event):
    """Manual classification of one already-synced email. Label changes are a direct update (no AI
    call — the user picked them explicitly). A categoryType assignment runs that single email through
    the exact same real extraction+matching pipeline sync-time auto-classification uses
    (match_and_save_category_item) — never just a label slapped on. categoryType: null clears the
    email's categoryItemId (the orphaned tracked item, if any, is left as-is)."""
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

        add_labels = body.get('addLabels') or []
        remove_labels = body.get('removeLabels') or []
        if add_labels or remove_labels:
            current_labels = (set(email_item.get('labels', [])) | set(add_labels)) - set(remove_labels)
            table.update_item(
                Key={'userId': user_id, 'emailId': email_id},
                UpdateExpression='SET labels = :l',
                ExpressionAttributeValues={':l': list(current_labels)}
            )
            email_item['labels'] = list(current_labels)

        result_category_item = None
        if 'categoryType' in body:
            category_type = body.get('categoryType')
            if category_type is None:
                table.update_item(Key={'userId': user_id, 'emailId': email_id}, UpdateExpression='REMOVE categoryItemId')
            else:
                category_catalog = get_category_type_catalog(user_id)
                schema = category_catalog.get(category_type)
                if not schema:
                    return {"statusCode": 400, "body": json.dumps({"error": "Unknown categoryType"})}

                extracted_list = _extract_fields_with_schema(
                    [{'subject': email_item.get('subject', ''), 'snippet': _richer_email_text(user_id, email_item)}], schema
                )
                extracted = extracted_list[0] if extracted_list else {}

                match_keys = schema.get('matchKeys') or []
                existing_items = []
                if match_keys:
                    existing_items = category_items_table.query(
                        IndexName='categoryType-index',
                        KeyConditionExpression=boto3.dynamodb.conditions.Key('userId').eq(user_id) &
                                                boto3.dynamodb.conditions.Key('categoryType').eq(category_type)
                    ).get('Items', [])

                result_category_item = match_and_save_category_item(
                    user_id, email_item, extracted, category_type, schema, existing_items
                )

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({
                "emailId": email_id,
                "labels": email_item.get('labels', []),
                "categoryItem": result_category_item
            }, ensure_ascii=False, cls=DecimalEncoder)
        }
    except Exception as e:
        print(f"Error classifying email: {str(e)}")
        return {"statusCode": 500, "body": json.dumps({"message": "Internal server error while classifying email", "error": str(e)})}

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
        tone = body.get('tone', 'formal')
        freeform_prompt = str(body.get('prompt', '')).strip()

        if not summary and not content and not freeform_prompt:
            return {
                "statusCode": 400,
                "body": json.dumps({"error": "No email content provided to draft a reply for."})
            }

        tone_instructions = {
            "formal": "Write a professional and polite reply.",
            "friendly": "Write a warm, friendly, conversational reply, like a helpful colleague, not stiff or overly formal.",
            "brief": "Write a short, brief reply. Two to three sentences at most, straight to the point.",
        }
        tone_instruction = tone_instructions.get(tone, tone_instructions["formal"])

        api_key = get_secrets()['OPENAI_API_KEY']
        if freeform_prompt:
            prompt = (
                "You are a helpful email assistant. Draft a concise, professional email body from the "
                f"following instructions:\n\n{freeform_prompt}\n\n"
                "Write only the email body, without a subject line."
            )
        else:
            prompt = (
                f"You are a helpful email assistant. {tone_instruction}\n\n"
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

# Caps the number of emails sent to the LLM in one /search call — keeps the prompt (and cost) bounded
# regardless of inbox size. Emails are truncated to the most recently received before this limit is
# applied, since that's the most likely relevant slice for a personal inbox at this scale.
_SEARCH_CANDIDATE_LIMIT = 150

def semantic_search_emails(query, items):
    """Asks the LLM which of `items` (each a DynamoDB email record) match `query` in meaning, not just
    exact keywords, and in what order of relevance. Returns a list of emailIds, most relevant first."""
    if not items:
        return []

    api_key = get_secrets()['OPENAI_API_KEY']

    numbered_emails = "\n\n".join(
        f"Email {i + 1}:\n"
        f"Subject: {item.get('subject', '')}\n"
        f"From: {item.get('from', '')}\n"
        f"Summary: {item.get('summary') or (item.get('content') or '')[:300]}"
        for i, item in enumerate(items)
    )

    prompt = (
        f"A user is searching their email inbox for: \"{query}\"\n\n"
        "Below is a numbered list of their emails. Identify which ones are relevant to the search — "
        "consider paraphrases, synonyms, and related concepts, not just exact keyword matches "
        "(e.g. \"flight\" should match a booking confirmation from an airline even if the word "
        "\"flight\" never appears). Do not include emails that are not actually relevant.\n\n"
        f"{numbered_emails}\n\n"
        'Respond with a JSON object of the form {"matches": [3, 1, 7]} — a list of the matching email '
        "numbers, ordered from most to least relevant. Return an empty list if nothing matches."
    )

    body = json.dumps({
        "model": "gpt-4.1-nano",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 300,
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
        matches = json.loads(raw_content).get('matches', [])
    except (json.JSONDecodeError, AttributeError):
        matches = []

    email_ids = []
    for n in matches:
        if isinstance(n, int) and 1 <= n <= len(items):
            email_id = items[n - 1].get('emailId')
            if email_id:
                email_ids.append(email_id)
    return email_ids

def handle_search_emails(event):
    try:
        user_id = get_authorized_user_id(event)
        if not user_id:
            return {
                "statusCode": 401,
                "body": json.dumps({"error": "Unauthorized. Could not identify user."})
            }

        query_params = event.get('queryStringParameters') or {}
        query = (query_params.get('q') or '').strip()
        if not query:
            return {
                "statusCode": 200,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"emailIds": []})
            }

        items = get_user_emails(user_id, query_params.get('account'), query_params.get('label'))
        items.sort(key=lambda e: e.get('receivedAt') or '', reverse=True)
        items = items[:_SEARCH_CANDIDATE_LIMIT]

        email_ids = semantic_search_emails(query, items)

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"emailIds": email_ids})
        }
    except Exception as e:
        print(f"Error running semantic search: {str(e)}")
        return {
            "statusCode": 500,
            "body": json.dumps({"message": "Internal server error while searching emails"})
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
        safe_accounts = [
            {
                'email': a['email'],
                'provider': a.get('provider', 'gmail'),
                'isPrimary': a.get('isPrimary', False),
                'needsReauth': a.get('needsReauth', False)
            }
            for a in accounts if a.get('email')
        ]

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
