# Smart Categories & Wizard — Implementation Design

**Status:** Draft. Translates `SMART_CATEGORIES_DESIGN.md`'s object design into concrete DynamoDB schema, API contracts, and a frontend component breakdown. Still a design document — no code written yet, per the usual staging (this → team review → actual code).

## 1. DynamoDB changes

### `Maily-CategoryTypes` (existing table, item shape changes)

Hash `userId`, range `categoryTypeId` — unchanged. Built-in types (`delivery`, etc.) stay as hardcoded Python constants (not written to this table), just redefined in the new `metadata`/`body` shape and merged at read time exactly like today's `get_category_type_catalog`. Only user-created types live in the table:

```
{
  userId, categoryTypeId,
  isBuiltIn: false,
  createdAt, updatedAt,
  metadata: { name, description, icon, classifierHint, key: {mode, fields}, lifecycle: {...}, display: {...} },
  body: { fields: [...] }
}
```

No new GSI needed — listing a user's custom types is already a plain `Query` on `userId`.

### `Maily-CategoryItems` (existing table, add one attribute)

Add `manualState: null | "done" | "trashed"`. Everything else unchanged. Existing GSI `categoryType-index` (`userId` + `categoryType`) still serves the `?type=` filter; add a `FilterExpression` on `manualState` for the Active/Done views (fine at this scale — no new GSI needed for that).

### `Maily-CategoryItemKeys` — new table (the match-key index)

Hash `userId`, range `matchKey` (string) → `{ itemId, categoryType }`.

This is what makes OR and AND resolve to the same lookup shape (per design doc §4b): one row per lookup-able key value, all pointing at the same `itemId`.

- **OR mode**: for each field in `key.fields` that has a value, write one row where `matchKey = normalize(value)` — the normalized value itself, no hashing needed since it's already a single scalar.
- **AND mode**: write exactly one row where `matchKey = sha256(normalize(field1) + "" + normalize(field2) + ...)`.

Key fields should be forced `sticky: true` by the sanitizer — if a key field's value could change later, its index rows would go stale. This is a good sanitizer rule to add regardless of mode.

## 2. Matching pipeline (replaces today's `matchKeys`-based logic)

On each extracted candidate for category type `T`:

1. Extract fields per `T.body.fields` (existing `_extract_fields_with_schema_chunk`, unchanged).
2. Compute candidate `matchKey`(s) from `T.metadata.key` as above.
3. Look up `Maily-CategoryItemKeys` for `(userId, matchKey)` — try each candidate in turn for OR mode, the single hash for AND mode.
4. **Row found** → fetch the `CategoryItem`:
   - `manualState == "trashed"` → stop. Don't merge fields, don't touch `contributingEmailIds`. Just stamp the source email's `categoryItemId` so it isn't re-evaluated as unmatched on the next sync.
   - otherwise (`null` or `"done"`) → merge fields per each field's `sticky` flag, append to `contributingEmailIds`, bump `updatedAt`/`lastUpdatedFromEmailAt`. **Never touch `manualState`** — this is what keeps a done card in the archive even as its data keeps updating.
5. **No row found** → create a new `CategoryItem` (`manualState: null`), then write the matchKey index row(s) for it (all present OR-field values, or the one AND hash).

This replaces `match_and_save_category_item`'s current matchKeys-list scan with a direct index lookup, and folds `ai_match_category_item` (today's AI fallback matcher) into a last-resort path only when no schema key value was extractable at all — same fallback role it plays today.

## 3. API contract changes

| Endpoint | Change |
|---|---|
| `GET /category-types` | Response items become the full schema object (`categoryTypeId`, `isBuiltIn`, `ownerId`, `createdAt`, `updatedAt`, `metadata`, `body`) instead of today's flatter shape. |
| `POST /category-types/generate` | Same request shape (`description` or `currentDraft`+`instruction`, optional `referenceEmailIds`). Response adds a `validation` block: `{ keyOk: bool, lifecycleOk: bool, warnings: string[] }` — the gate-check results from the design doc, surfaced so the Wizard chat can show them conversationally. |
| `POST /category-types` | Body = the approved draft. Since gates are a **soft block**, the client can send `forceApprove: true` to create despite outstanding warnings; without it, a failing gate still returns the create (this is a UX nudge, not a hard server-side rejection — the "gate" lives in the Wizard conversation, not as a validation error). |
| `PUT` / `PATCH` / `DELETE /category-types` | Same surface, new body shape. `DELETE` stays rejected for `isBuiltIn: true`. |
| `GET /smart-categories` | Add optional `state=active\|done` query param (default `active`). No `state=trashed` — trashed items are never returned by this endpoint, matching "never rendered anywhere." |
| `PATCH /smart-category?itemId=...` **(new)** | Body `{ manualState: "done" \| "trashed" \| null }`. Sets or clears the manual override. The handler itself stays permissive about all transitions (including `trashed → null`) — "no restore from trash" is enforced by simply not rendering that button today, so turning it on later is a frontend-only change, not a backend one. |

## 4. Frontend component breakdown

New `maily-web/src/components/fields/` — one small component per widget from the design doc's dispatch table: `TextField`, `LinkField`, `NumberField`, `CurrencyField`, `PercentField`, `DateField`, `RelativeDateField`, `BooleanField`, `StatusBadgeField`. Each takes `{ fieldDef, value }`. A single `renderCategoryField(fieldDef, value)` dispatcher (switch on `type` + `format`) picks the right one — this is the piece that doesn't exist today.

`CategoryItemCard` (`App.tsx` today, could move into `components/` as part of this work):
- Renders fields through the new dispatcher instead of plain text.
- Adds an action row: **Mark done** (shown when active), **Restore to active** (shown when done), **Trash** (shown when active or done) — each calls the new `PATCH /smart-category`.
- Categories view gets an Active/Done toggle or tabs, driving the `state` query param. No trashed view.

`CategoryWizard.tsx`:
- Key editor: OR/AND mode toggle + multi-select of which `body` fields participate in the key.
- Lifecycle editor: `completionRule`/`atRiskRule` pickers updated to the two agreed rule shapes (`status_equals`: field + value dropdown constrained to that field's `values`; `date_passed_without_status`: dateField + statusField + targetValue).
- Per-field `format` dropdown, options filtered by that field's `type` (`currency`/`percent` only for `number`, `url` only for `string`, `relative-date` only for `date`).
- Review stage renders `validation.warnings` as chat messages with a "create anyway" affordance next to "let me fix it," matching the soft-block design.

## 5. Suggested build sequencing

1. **Infra** (Dolev): add `Maily-CategoryItemKeys` table; add `manualState` attribute to `Maily-CategoryItems` (no migration needed, just start writing it going forward).
2. **Backend** (Tomer): rewrite the matching pipeline around the new key table; redefine the built-in `delivery` type in the new `metadata`/`body` shape; update the sanitizer and generation prompt for `key.mode`, the lifecycle rule shapes, and the `format` enum; add the `manualState` PATCH endpoint and the `state` filter on list.
3. **Frontend** (Daniel): build the field-widget dispatcher + card action buttons first, tested against the existing `delivery` category as a real end-to-end case; then update `CategoryWizard.tsx` for key-mode/format editing.
4. Only once the generic system is proven on `delivery` end-to-end, add the other roadmap-approved built-in types (bills, travel, events, subscriptions) *through* it — per the original roadmap's own sequencing note, this avoids hand-building four bespoke card layouts.

## Next step

This document is the last planning stage before code. Once it's reviewed, actual implementation would proceed per the sequencing above — starting with infra/backend since the frontend work depends on the new endpoint shapes.
