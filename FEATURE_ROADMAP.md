# Maily — Next-Phase Feature Roadmap

**Status:** All features below are approved for the next phase of work (2026-08-08). This document is the "what and why" for each — it deliberately stops short of implementation detail (data model, API contracts, endpoint specs). That level of design happens per-feature in a follow-up document once we're ready to start building each one.

## Context

Maily currently handles the **read side** of email well: multi-account Gmail+Outlook sync, thread view, on-demand body fetch, attachment download, AI summarization/labels/smart-categories, and a reply-text generator. It has **no write side at all** — no compose, no send, no CC/BCC, no reply/forward, and no search. This roadmap closes that gap so Maily functions as a real mail client, with AI integrated into the new features where it adds value, not just kept in the existing summarization/labeling flow.

Guardrails: realistic/core scope — calendar integration, delegation/shared mailboxes, offline mode, and account-security-control build-out are explicitly parked (see "Out of scope" below); Compose starts plain-text/minimal-formatting before rich text; basic keyword search ships before AI natural-language search; no hard deadline, so the order below follows dependency/priority, not a compressed timeline.

## Current state (verified against code)

**Built:** Gmail + Outlook multi-account sync (`Infrastructure/backend_lambda.py`), thread view (`/thread`), on-demand live body fetch (`/email-body`), attachment download via presigned S3 URL (`/attachment`), custom + preset labels with AI classification (`/labels`), Smart Categories delivery-tracking extraction (`/smart-categories`, currently **one hardcoded type: `delivery`**), stats (`/stats`), summary export (`/export`), fetch-limit setting, 5 themes, DB-only mark-read, EventBridge auto-sync every 15 min. AI: OpenAI `gpt-4.1-nano` used for summarization, label classification, smart-category field extraction/matching, and reply-text drafting (`/draft` — generates text only today, no send, no To/Subject, copy-to-clipboard UX).

**Not built at all:** compose UI, send (Gmail `messages.send` / Graph `sendMail`), CC, BCC, Reply/Reply-All/Forward, rich text, inline images, hyperlinks in body, signatures, quoted-reply text, draft auto-save, search (no endpoint, no UI), archive/delete/trash, star/flag, filters/rules, spam handling, sort views, snooze, bulk actions, schedule send, undo send, contacts/address book, templates, provider-side mark read/unread (today it's DB-only), pagination, additional smart-category types, and any way for a user to define their own category type.

## Roadmap — by feature, priority order

### 1. Compose & Send 
The foundational unlock — everything else that involves writing mail depends on this.

- **Stage A — Core send:** Compose UI (To, Subject, Body — plain text/minimal formatting); Send (Gmail `messages.send` + Graph `sendMail`, needs new OAuth scopes `gmail.send` / `Mail.Send`); CC + BCC together (same UI row, same param shape); attachment upload (Compose needs this to be a real replacement for existing clients); signature (plain-text, auto-appended, configurable in Settings); recipient autocomplete sourced from already-synced senders/recipients (no new contacts system needed).
- **Stage B — Reply / Forward:** Reply, Reply-All, Forward — pre-fill Compose from an existing thread message (recipients, quoted original text, `Re:`/`Fwd:` subject prefix); quoted original text; draft auto-save (reuses Compose state); Reply-To address override.
- **Stage C — AI wiring:** wire the existing `/draft` reply-text generator directly into the Reply compose box (pre-fill body instead of today's copy-to-clipboard flow) — nearly free since the endpoint already exists; extend `/draft` to accept a free-form prompt so it can draft **new** emails, not just replies to an existing one.

*Why one feature, not split:* Stages A/B/C all touch the same new Compose component and the same new send-path backend code — reopening those files feature-by-feature instead of stage-by-stage would just be repeated churn.

### 2. Search 
Independent track — different backend surface (new `/search` endpoint + inbox UI), no dependency on Compose, so it's a good candidate to build in parallel with Feature 1 by a different person.

- **Stage A:** keyword/full-text search across subject/body/sender; operators (`from:`, `subject:`, `has:attachment`, `before:`/`after:`, `is:unread`, `label:`); pagination of results (also fixes the existing gap where `/hello` returns everything unpaginated); sort/filter views (date, sender, unread, has-attachment) on the same query surface.
- **Stage B — AI:** natural-language "ask your inbox" search — a chat-style query gets translated (via the existing OpenAI integration) into the structured filter object from Stage A and run through the same endpoint. Deliberately sequenced after Stage A so the AI layer only needs to produce a filter object the UI already knows how to execute.

### 3. Rich Compose Editor 
Upgrade of Feature 1's Compose component — deferred until after Compose & Send is solid, since it's an editor-library swap rather than new plumbing.
- Rich text formatting (bold/italic/underline/lists/alignment/colors), hyperlinks, inline images, multiple/HTML signatures.

### 4. Mailbox Organization / Triage Parity
Same theme throughout: real provider-side actions using the Gmail/Graph API clients already built for sync.
- Archive; Delete → Trash → restore (Gmail `modify`/`trash`, Graph move-to-folder); real read/unread sync to the provider (today it's DB-only); Star (Gmail) / Flag (Outlook); bulk actions (thin UI layer over the same calls, multi-select + apply).
- **AI:** extend the existing sync-time classifier (`classify_emails_batch`) to also emit a priority/importance signal per email — marginal cost, since it already reads every email once for labels/summary; can drive auto-triage surfacing of high-priority mail.

### 5. Smart Categories Expansion
Two distinct pieces of work — both build on the existing `CATEGORY_TYPES` system (`Infrastructure/backend_lambda.py`), which today is a single hardcoded `delivery` schema.

- **Stage A — More built-in category types (all four confirmed):**
  - **Bills / Invoices** — due date, amount, payee, paid status
  - **Travel / Bookings** — confirmation number, dates, provider
  - **Events / RSVP** — event name/date, location, RSVP status
  - **Subscriptions / Receipts** — merchant, amount, billing cycle/renewal date

  Each is backend-mostly (new `CATEGORY_TYPES` entry + extraction prompt) plus frontend work to render its field set (today's `CategoryItemCard`/`CATEGORY_TYPE_META` only knows `delivery`, so each new type needs its own card layout unless Stage B's generic renderer lands first — worth sequencing Stage B before building all four card layouts by hand, see note below).
- **Stage B — Smart Categories Wizard:** let a user define their **own** category type (name, which fields to extract, matching keys, completion/at-risk rules) instead of relying on hardcoded types. This is the bigger lift: category schemas move from a hardcoded Python dict to per-user schema storage (new DynamoDB table/attribute), the AI extraction call needs to work generically against an arbitrary user-defined field list instead of a fixed prompt per type, and the frontend needs a **generic** field-renderer driven by the schema the backend returns (instead of one hardcoded card component per type).
- **AI angle for the Wizard specifically:** let the user describe what they want to track in a sentence (e.g. "job applications I've sent, with company, role, and status") and have AI propose the field schema, plus offer a "test this category against a sample email" preview during setup — both reuse the same OpenAI integration already in place, just against a new prompt.

  **Sequencing note:** because Stage A's four new types would otherwise mean four hand-built card layouts, consider building Stage B's generic renderer first and implementing the four Stage A types *through* it — turns "4 hardcoded types + 1 wizard" into "1 generic system + 5 schema definitions (4 built-in + user-defined)." Worth deciding at design time, not now.

### 6. Productivity Add-ons
Lower priority, no dependencies blocking them once Compose (Feature 1) and Labels exist.
- Filters/Rules (auto-label/move/archive on arrival — natural extension of the existing Labels system), Templates/canned responses (reuses Compose), Snooze, Schedule send, Undo send.

### Explicitly out of scope for this roadmap
Calendar integration & meeting invites, delegation/shared mailboxes, offline mode, account security controls (2FA/app passwords/session list), confidentiality controls, voting/polls, translation, add-ons/API, keyboard shortcuts, print/full mailbox export. These stay on the original source list but aren't planned or estimated here — revisit explicitly if priorities change later.

## AI integration summary

| Feature | Stage | What AI does | Why cheap/natural here |
|---|---|---|---|
| Reply drafting wired into Compose | 1C | Pre-fill Reply body from existing `/draft` endpoint | Endpoint already exists, just needs wiring |
| AI-drafted new emails from a prompt | 1C | Extend `/draft` to take free-text intent, not just "reply to X" | Same endpoint, same model call shape |
| AI natural-language search | 2B | Translate chat query → structured filter object | Reuses Stage 2A's filter engine, avoids a second search path |
| Priority/importance flagging | 4 | Extra field on the existing sync-time classifier call | No new AI call — classifier already reads every email once |
| Smart Categories Wizard: schema suggestion + test preview | 5B | Propose a field schema from a one-sentence description; preview extraction against a sample email | Reuses the existing extraction call, new prompt only |
| Filters/Rules suggestions (stretch, not scheduled above) | 6+ | Suggest a rule from repeated manual triage patterns | Only worth doing once Feature 4/6 triage actions exist to learn from |

## Splitting work across the team

Backend (Tomer) and frontend (Daniel) split along the existing REST contract; Dolev's infra work is mostly upfront/parallelizable. General rule: anywhere the API request/response shape and OAuth-scope needs are agreed on *before* work starts, backend and frontend can build fully in parallel against a mocked contract.

**Fully separable (backend and frontend can proceed independently once the contract is written down):**
- Feature 2 (Search) Stage A — Tomer builds `/search` + query logic, Daniel builds the search bar/results UI against an agreed request/response shape. Low overlap risk.
- Feature 4 (Mailbox Organization) — each action (archive/delete/star/bulk) is a small independent Lambda handler; frontend just wires buttons to endpoints. High separability, low risk.
- Feature 6 (Productivity Add-ons) — Filters/Rules, Templates, Snooze, Schedule send are mostly independent backend/frontend pairs.
- Feature 2B and 5B's AI sub-pieces (natural-language search, wizard schema suggestion) are backend-only additions once their Stage-A foundation exists — near-zero frontend surface beyond an input box.

**Overlapping but low-risk (needs a shared contract, not tight coordination):**
- Feature 1 Stage A/B (Compose, Send, Reply/Forward) — frontend builds the Compose component while backend builds `/send`; the risk isn't technical overlap, it's the **OAuth scope change**, which is cross-cutting: Dolev needs to update the Google Cloud Console and Azure App Registration scopes, and every already-connected test/dev account will need to reconnect once `gmail.send`/`Mail.Send` scopes are added. This should happen first and be coordinated across all three people before Stage A backend/frontend work starts, not discovered mid-build.
- Feature 5 Stage A (more hardcoded category types, if built without the wizard's generic renderer) — same pattern as existing Smart Categories: backend adds a schema+prompt, frontend adds a card layout for it. Repeatable, low risk, but is genuinely two people's work per type.

**Needs careful integration (design the contract together before either side builds):**
- Feature 3 (Rich Compose Editor) — mostly frontend (editor library, HTML sanitization on render) but backend must sanitize HTML on the way in before persisting/sending (stored-HTML / inline-image handling is an XSS/injection risk if only one side sanitizes). Agree on who owns sanitization (recommend: both — backend sanitizes before persist/send as the source of truth, frontend sanitizes again before render as defense in depth) before either side starts.
- Feature 5 Stage B (Smart Categories Wizard) — the trickiest integration point in the whole roadmap. Backend must return a **generic schema description** (field names, types, display hints) instead of a fixed shape, and frontend must build a **generic renderer** driven by that schema instead of one hardcoded card per type. If the schema format isn't agreed first, this becomes a rebuild instead of an extension. Design this contract jointly before either side writes code — and note the sequencing option above (build Stage B first, implement Stage A's four types through it).
- Feature 6's Schedule Send — needs an infra decision (Dolev) up front: per-message EventBridge Scheduler entries vs. a polling Lambda against a "send at" timestamp. This determines what the backend API even looks like (e.g., does `/send` accept a `sendAt` field, and what does cancel/undo look like), so infra approach should be picked before backend work starts, not after.

## Next step

When ready to start building, the next document(s) cover the **how**, feature by feature: new OAuth scopes and re-consent flow, DynamoDB schema additions, new Lambda handlers and their contracts, and frontend component breakdown (the entire UI currently lives in one 1600-line `App.tsx` with no `components/` directory — worth addressing as part of the Feature 1 design, since Compose alone will be sizeable enough to justify splitting it out).


