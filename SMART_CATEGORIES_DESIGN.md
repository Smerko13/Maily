# Smart Categories & Wizard — Object Design

**Status:** Draft for review. Builds on the confirmed top-level architecture (see prior planning session). This document is still a **design doc, not an implementation spec** — no API contracts or DynamoDB schema changes yet. That's the next stage, after this is agreed.

## Two related objects, not one

The earlier discussion asked for "one object whose top level is the same for every category." That requirement applies to the **Category Type Schema** — the definition a category has (its rules), which the Wizard produces once per category type. Every category type's schema — `delivery`, `bills`, a custom `job-applications` — has the identical top-level shape; only the contents of `body` differ.

The **Category Item** — the actual card built from real emails — is a second, simpler object: it just holds extracted values that conform to a schema's field list. It doesn't need its own `metadata`/`body` split; it references a schema (`categoryType`) and lets that schema's `metadata` govern how it's evaluated (key matching, lifecycle rules) and rendered.

This mirrors what's already built (`CATEGORY_TYPES`/`Maily-CategoryTypes` = schema, `Maily-CategoryItems` = instances) — the design below refines the schema's shape, it doesn't invent a new split.

## 1. Category Type Schema — the Wizard's output

```json
{
  "categoryTypeId": "delivery",
  "isBuiltIn": true,
  "ownerId": null,
  "createdAt": "2026-08-01T00:00:00Z",
  "updatedAt": "2026-08-01T00:00:00Z",

  "metadata": {
    "name": "Package Delivery",
    "description": "Tracks a purchased item from order confirmation through delivery.",
    "icon": "📦",
    "classifierHint": "Order confirmations, shipping notifications, and delivery updates for a purchased item.",

    "key": {
      "mode": "OR",
      "fields": ["trackingNumber", "orderNumber"]
    },

    "lifecycle": {
      "completionRule": { "type": "status_equals", "field": "status", "value": "delivered" },
      "atRiskRule": {
        "type": "date_passed_without_status",
        "dateField": "estimatedDelivery",
        "statusField": "status",
        "targetValue": "delivered"
      }
    },

    "display": {
      "titleTemplate": "{merchant} — {trackingNumber}",
      "primaryDateField": "estimatedDelivery",
      "cardFields": ["status", "estimatedDelivery", "merchant", "trackingNumber"]
    }
  },

  "body": {
    "fields": [
      { "key": "merchant", "label": "Merchant", "type": "string" },
      { "key": "trackingNumber", "label": "Tracking #", "type": "string", "sticky": true },
      { "key": "orderNumber", "label": "Order #", "type": "string", "sticky": true },
      {
        "key": "status", "label": "Status", "type": "enum",
        "values": ["ordered", "shipped", "out_for_delivery", "delivered"],
        "hint": "Classify the shipment's current stage from the email content"
      },
      { "key": "estimatedDelivery", "label": "Est. Delivery", "type": "date" },
      { "key": "amount", "label": "Amount", "type": "number", "format": "currency" }
    ]
  }
}
```

**`metadata`** — universal, every category type has these, regardless of what it tracks:
- `name`, `description`, `icon`, `classifierHint` (identity + what the AI looks for when deciding an email belongs to this category at all, distinct from field-level extraction)
- `key` — the matching key. `mode: "OR"` (any listed field matching = same card, today's behavior) or `mode: "AND"` (all listed fields must match together — the new capability for cases like `companyDomain` + `roleTitle` where neither alone is unique)
- `lifecycle` — `completionRule`/`atRiskRule`, each either `null` or one of the two rule types agreed on: `status_equals` (a field reaches a value) or `date_passed_without_status` (a date passed without a target status). Evaluated at read time against a card's fields, not stored as a flag — matches today's `evaluate_category_item_state`.
- `display` — how the card shell renders this category: title template, which field drives the primary date, which fields show on the compact card.

**`body.fields`** — category-specific, this is what differs per category:
- `key`, `label`, `type` (`string` | `number` | `date` | `boolean` | `enum`)
- `hint` — free-text instruction to the extraction AI on how to find this field's value in an email
- `values` — required for `enum`, the closed set of valid classifications
- `sticky` — once set, never overwritten by a later email (today's merge behavior — needed for things like `trackingNumber` that shouldn't flip if a later email is ambiguous)
- `format` — optional refinement on top of `type`, drives which UI widget renders it (see below)

## 2. Field type → card widget dispatch

The card shell picks a sub-component per field based on `type` (+ optional `format`):

| `type` | `format` | Widget |
|---|---|---|
| `string` | — | Text |
| `string` | `url` | Link |
| `number` | — | Number |
| `number` | `currency` | Currency (formatted with symbol) |
| `number` | `percent` | Percent |
| `date` | — | Date (absolute) |
| `date` | `relative-date` | Date (relative — "in 2 days" / "3 days ago") |
| `boolean` | — | Boolean (check/cross or yes/no) |
| `enum` | — | Status badge (colored pill, color keyed off the value) |

This is the piece that doesn't exist today — `CategoryItemCard` currently renders every value as plain text regardless of `type`.

## 3. Category Item — a card's extracted data

```json
{
  "itemId": "delivery#a1b2c3d4e5f6",
  "categoryType": "delivery",
  "userId": "user-456",
  "fields": {
    "merchant": "Amazon",
    "trackingNumber": "1Z999AA10123456784",
    "status": "out_for_delivery",
    "estimatedDelivery": "2026-08-22",
    "amount": 42.99
  },
  "contributingEmailIds": ["email-1", "email-2"],
  "createdAt": "2026-08-15T10:00:00Z",
  "updatedAt": "2026-08-20T09:00:00Z",
  "lastUpdatedFromEmailAt": "2026-08-20T09:00:00Z"
}
```

`fields` is a flat key→value map keyed by the schema's `body.fields[].key`. `isComplete`/`isAtRisk` stay computed, not stored, by evaluating the schema's `metadata.lifecycle` rules against `fields` — **unless overridden manually**, see below.

## 3b. Manual card controls: mark-done, trash, and anti-recreation

Two manual actions sit on top of the automatic `completionRule`/`atRiskRule` evaluation, for cases like "the package physically arrived but the delivery email hasn't come in yet." One new field on the Category Item:

```json
{
  "itemId": "delivery#a1b2c3d4e5f6",
  ...
  "manualState": null
}
```

`manualState` is `null` | `"done"` | `"trashed"`.

- **Effective card state** = `manualState` if set, otherwise the live computed state from `metadata.lifecycle` (today's rule evaluation). This is why forcing a card to "done" works immediately even for a date-based rule whose date hasn't passed yet — the manual flag just short-circuits the rule check, same end result as if the rule had fired.
- **Mark as done button** → sets `manualState = "done"`. Card moves to a Done/Archive view.
- **Trash button** → sets `manualState = "trashed"`. Card disappears from the UI entirely. Note this is a **soft delete** — the row stays in the database, only hidden from display. That's necessary for the next point.
- **New matching email arrives after a card is done or trashed:**
  - If `manualState == "done"`: the email still matches the card's key and still merges into `fields`/`contributingEmailIds` as normal (per each field's `sticky` rule) — so no data is lost and no duplicate card gets created. Merging **never** resets `manualState` back to `null`, so the card stays in Done even though its data just changed.
  - If `manualState == "trashed"`: matching still finds the row (its key fields are untouched by the soft delete), but the app stops there — it does **not** merge the new email's fields and does **not** create a new card. This is the minimum "we don't display it, but we remember enough to behave correctly": the row's key fields plus `manualState: "trashed"` are enough to block recreation; nothing else needs to keep updating on a trashed card.

UI-wise: **active** cards show on the normal board, **done** cards show in a Done/Archive view (still real, still viewable), **trashed** cards never render anywhere again.

**Restore:** a Done card can be moved back to Active (clears `manualState` back to `null`, so the card is governed by the computed lifecycle rule again — if the rule still evaluates true it'll just show as done again on next read, which is fine). A Trashed card has **no restore path in the UI** for now — the data is kept (soft delete, not purged), but there's no button to bring it back. This could be added later; not building it now.

## 4. Composite-key example (why AND matters)

```json
{
  "categoryTypeId": "job-applications-x7y8z9",
  "isBuiltIn": false,
  "ownerId": "user-456",
  "metadata": {
    "name": "Job Applications",
    "key": { "mode": "AND", "fields": ["companyDomain", "roleTitle"] },
    "lifecycle": {
      "completionRule": { "type": "status_equals", "field": "status", "value": "offer" },
      "atRiskRule": null
    },
    "display": {
      "titleTemplate": "{roleTitle} @ {companyDomain}",
      "primaryDateField": "appliedDate",
      "cardFields": ["status", "appliedDate", "companyDomain"]
    }
  },
  "body": {
    "fields": [
      { "key": "companyDomain", "label": "Company", "type": "string", "sticky": true },
      { "key": "roleTitle", "label": "Role", "type": "string", "sticky": true },
      { "key": "appliedDate", "label": "Applied", "type": "date", "sticky": true },
      { "key": "status", "label": "Status", "type": "enum", "values": ["applied", "interview", "rejected", "offer"] }
    ]
  }
}
```

`companyDomain` alone isn't unique (many roles per company); `roleTitle` alone isn't unique (same title at many companies). Together, they are — this is what today's OR-only `matchKeys` can't express.

## 4b. How AND-matching actually works (composite key hashing)

When `key.mode` is `"AND"`, at write time the app computes one derived string — a **composite key** — by concatenating the current values of every field listed in `key.fields`, normalized, joined with a delimiter that can't appear in normal values, then hashed into a single opaque string:

```
matchKey = sha256(normalize(companyDomain) + "" + normalize(roleTitle))
```

That `matchKey` is stored as a single attribute on the item and looked up the same way an OR-mode key already is — "does an item already exist with this exact `matchKey` value." The only difference between OR and AND is *how many* `matchKey` values map to one item, and how each is computed:
- **OR mode**: one item is reachable under **multiple** `matchKey` values — one per alternate field (today's `matchKeys` behavior).
- **AND mode**: one item has exactly **one** `matchKey` value, computed by hashing all required fields together.

So OR and AND don't need two different query shapes — both resolve to "look up an item by a `matchKey` string," just computed differently beforehand. That keeps the matching code path unified.

**Normalization matters**: if `companyDomain` extracts as `"Acme.com"` from one email and `"acme.com "` from another, hashing the raw strings produces two different keys and wrongly creates two cards. The extraction step must normalize (trim, lowercase, consistent stringification for numbers/dates) before hashing, every time.

## 5. Wizard validation gates

Before the Wizard lets the user approve a new category, it must confirm (per the earlier "must be able to extract a working key and a viable lifecycle rule" requirement):

1. Every field referenced by `metadata.key.fields` exists in `body.fields` and the AI is confident it's reliably extractable (not something vague like "any mention of a number"). Example: proposing `companyName` alone as the key for "job applications" isn't reliable if it shows up inconsistently across emails ("Acme Inc" vs "Acme" vs missing) — the Wizard should catch that a single card could get fragmented into several, and suggest strengthening the key (e.g. add `roleTitle`, switch to AND-mode) instead of approving a category that will silently misbehave.
2. `metadata.lifecycle` has at least one non-null rule, and every field/value it references exists in `body.fields` (e.g. `completionRule.value` must be one of that field's `values` if it's an enum).
3. Every `enum` field has a non-empty `values` list; every field has a usable `hint`.

If any gate fails, the Wizard should surface it conversationally at the review stage ("I couldn't find a reliable way to tell these apart — want to add a field for X?") rather than silently create a broken category. This is a **soft block, not a hard one** — the user can approve anyway after being warned. If a category ends up behaving badly in practice, they can always delete it entirely (built-in categories aren't deletable, but user-defined ones are).

## Open items for the next stage (API/DynamoDB design)

- **Item storage — resolved.** One Category Item row per card, mutated in place as matching emails arrive (already today's behavior via `_merge_into_category_item`) — a new matching email never creates a second row, it updates the existing one's `fields`/`contributingEmailIds`/`updatedAt`. Whether the schema's `metadata`/`body` split is stored as literal nested DynamoDB attributes or flattened-and-reassembled is a pure storage-layer detail with no behavioral consequence — deferred to the implementation-design stage, low stakes either way.
- **AND-key matching — resolved**, see §4b above: composite key = hash of the normalized, concatenated required fields, stored as one `matchKey` value per item. OR and AND both resolve to the same "look up by `matchKey`" query shape.
- **`format` — resolved: fixed enum.** The sanitizer only accepts a known list; anything else is rejected at creation time so the frontend always has a matching widget. Starting list, kept deliberately small and extended only when a real category needs a new one:
  - `currency` (on `number` fields)
  - `percent` (on `number` fields)
  - `url` (on `string` fields)
  - `relative-date` (on `date` fields — renders as "in 2 days" / "3 days ago" instead of an absolute date)
