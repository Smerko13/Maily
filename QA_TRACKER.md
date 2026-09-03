# Maily — QA & Pre-Launch Tracker

> **What this file is:** a running list of known bugs and possible improvements as we prep Maily
> for demo/production. Split into **Frontend** and **Backend** sections. Each item starts with a
> status tag:
> - `[NEEDS FIX]` — confirmed issue, not started
> - `[IN PROGRESS]` — someone is actively working on it (say who in a comment/commit, not here)
>
> The Frontend/Backend sections above the line only ever hold **unfixed** items (`[NEEDS FIX]` /
> `[IN PROGRESS]`). Once something is actually fixed and verified, move its entry down into the
> **✅ Fixed** section at the bottom instead of leaving it up top — write it up as *what the problem
> was* and *what was changed to fix it*, so the top of the file always reflects only what's still
> outstanding.
>
> **Ground rules:** this file is a planning/sync tool for the two of us, not a task queue to work
> through unilaterally — nothing here gets implemented until we've discussed it and explicitly
> agreed to tackle it. When you pick something up, flip its status to `[IN PROGRESS]` first so we
> don't duplicate work, since we're touching overlapping areas. Recommendations (further down) are
> starting points, not final designs.

---

## Frontend

1. **[NEEDS FIX] No real routing — app is one big page** (`App.tsx`)
   There is no router at all (no `react-router-dom` in `package.json`, no `BrowserRouter`). Every
   "page" (Dashboard, Inbox, Sent, Compose, Settings, Smart Categories, Statistics) is just one
   `activeTab` state value in a single ~2,900-line `App.tsx`, all rendered conditionally in the same
   component. Practical effects: the URL never changes, refreshing the page always dumps you back
   to Dashboard, there's no browser back/forward support, and nothing is bookmarkable/deep-linkable.
   Note: a full "real page components + shared context" version of this was designed in detail and
   partially built, then deliberately reverted (scope/risk, not a technical dead end) — worth
   revisiting that design before starting over from scratch if this gets picked up again.

---

## Backend

1. **[NEEDS FIX] No DB indexing — every list/sync request pulls a user's entire email history**
   `Maily-Emails` DynamoDB table (`dynamodb.tf`) has no index on `receivedAt` or account — every
   list/sync request (`get_user_emails`, `backend_lambda.py`) pulls **all** of a user's stored
   emails and filters/sorts them in-memory in Python, rather than querying DynamoDB directly for
   what's needed. Gets slower and more wasteful the longer someone uses the app. Deliberately not
   started yet — see the note under Recommendations below; we're addressing this after the sync
   changes above have had time to settle, then diving into it in more detail together.

---

## Recommendations

**Frontend**
- **Routing (FE-1):** Introduce `react-router-dom` with real routes (`/dashboard`, `/inbox`,
  `/compose`, `/settings`, `/categories`, `/stats`) replacing the `activeTab` state machine. Fixes
  refresh, back/forward, and deep-linking in one pass. See the note under Frontend item 1 above —
  a fuller design (real page components, shared context, URL-addressable email/category-item
  detail views) already exists from a prior attempt; worth reusing that design rather than
  re-deriving it.

**Backend**
- **DB indexing (BE-4):** Add a DynamoDB GSI on `(userId, receivedAt)` (and optionally
  `(userId, account)`) so filtering/sorting happens in the query instead of pulling every email and
  filtering in Python. Deliberately deferred — see the note above; revisit together once we've
  confirmed the sync changes below are solid.
- **Note — scheduled-sync fan-out (was BE-5):** spreading `handle_scheduled_sync`'s per-user loop
  across parallel invocations was considered and **explicitly declined** — not something we're
  doing for this project. Left here so nobody re-proposes it without knowing it was already decided.

*(This file will grow as we find more. When an item above is actually fixed and verified, cut it
from its Frontend/Backend list and paste it into the ✅ Fixed section below instead of leaving it
up top or deleting it.)*

---

## ✅ Fixed

**Backend**

1. **New accounts didn't auto-connect a mailbox / no primary-account concept**
   **Problem:** Signup (Cognito) and connecting Gmail/Outlook were fully disconnected flows; the
   user had to find Settings and click Connect manually, and there was no way to know which
   account was the "main" one.
   **Fix:** `google_auth.py`/`outlook_auth.py` now stamp the first account a user ever connects
   with `isPrimary: true`, preserved across reconnects and exposed via `GET /accounts`. Paired with
   a frontend onboarding prompt — see Frontend Fixed #2.

2. **No handling for expired/revoked OAuth refresh tokens (silent re-auth failure)**
   **Problem:** `refresh_google_access_token`/`refresh_microsoft_access_token` had no handling for
   a `400 invalid_grant` response (e.g. a Google "Testing" app's 7-day refresh-token expiry, or a
   revoked grant); the failure was swallowed by a generic `except Exception` and only logged to
   CloudWatch, so Settings kept showing "✅ Connected" forever while sync silently died.
   **Fix:** Both refresh functions now catch `invalid_grant` specifically, persist a `needsReauth`
   flag immediately via `_mark_account_needs_reauth` (written directly, not dependent on the sync
   loop's conditional save), and raise a distinguishable `ReauthRequiredError`. Reconnecting an
   account through the normal OAuth flow clears the flag. Exposed via `GET /accounts`; surfaced in
   Frontend Fixed #3.

3. **Gmail's SENT label was fetched then explicitly discarded — no sent/received distinction**
   **Problem:** `_GMAIL_STRUCTURAL_LABELS` explicitly skipped Gmail's `SENT` label, and the stored
   email schema had no `direction`/`folder` field at all, so sent and received mail were
   indistinguishable once stored.
   **Fix:** Every synced email now gets a `direction: 'sent' | 'received'` field — derived from
   Gmail's `SENT` label (with a from-address-matches-account fallback) and, for Outlook (no
   equivalent structural label), from a from-address comparison against the account's own email.
   `get_user_emails`/`GET /hello` now accepts an optional `?direction=sent|received` filter.
   Emails synced before this change have no `direction` key and are treated as `'received'` by
   default (no backfill was done). Consumed by Frontend Fixed #1; a dedicated Sent tab UI is still
   pending (Frontend item 4, still `[NEEDS FIX]`).

4. **Sync was slow — serial per-message fetches, and a full re-list on every cycle**
   **Problem:** `sync_single_gmail_account` fetched every new message's full content with its own
   sequential, blocking HTTP call in a loop (classic N+1), and neither provider used an incremental
   "what changed" API — every sync cycle re-listed the last `fetch_limit` messages from scratch and
   diffed against a saved watermark id. That watermark approach also had a latent data-loss bug: a
   backlog bigger than `fetch_limit` between syncs meant the watermark was never found, so the
   overflow was silently skipped forever.
   **Fix:**
   - Gmail message-detail fetches (and Outlook's per-message attachment-metadata fetches) now run
     concurrently via a thread pool (`_fetch_gmail_messages_full`, `_fetch_outlook_attachments_parallel`),
     bounded per account — never mixed across accounts, same boundary the AI classification batching
     (`finalize_batch`) already used.
   - Gmail sync now uses the `history.list`/`historyId` API to ask directly "what's new since last
     time" instead of re-listing and diffing (`_gmail_history_message_ids`); falls back to a full
     bootstrap re-list on an account's first-ever sync, or if Gmail has aged out the stored history
     baseline (`GmailHistoryExpiredError`, ~7-30 day retention).
   - Outlook sync now uses a Graph delta link the same way once one is established
     (`_outlook_delta_messages`), with the same bootstrap fallback on first sync or an expired link
     (`OutlookDeltaExpiredError`, HTTP 410). **Lower confidence than the Gmail path** — Graph's
     `$filter`+delta combination for mail wasn't verified against a live mailbox, so establishing
     the delta link is wrapped in a try/except that quietly falls back to the (still correct, just
     slower) bootstrap re-list on any failure. **Worth testing against a real Outlook account
     before the demo** to confirm it's actually taking the fast path.
   - The backlog-skip bug described above no longer applies to either provider now that both use a
     real "what changed" cursor instead of a capped re-list.
   - One-time transitional note: any already-connected account has no stored `last_history_id`
     (Gmail) or `outlook_delta_link` (Outlook) yet, so its very next sync takes the bootstrap path
     once more — this safely re-processes (overwrites, not duplicates) up to `fetch_limit` recently-
     synced emails per account, a one-time no-op cost before incremental sync takes over.

**Frontend**

1. **Inbox (and Dashboard) showed mail the user sent to others**
   **Problem:** The Inbox list (and the Dashboard's digest/recent list and stats) rendered every
   synced email with no sent/received filter, so the user's own outgoing mail showed up mixed into
   the inbox.
   **Fix:** Both now filter out `direction === 'sent'` (`App.tsx`, Inbox's `byLabel` computation and
   the Dashboard tab's `dashboardEmails`), relying on the backend's new `direction` field (Backend
   Fixed #3). Emails missing the field default to `'received'` so nothing pre-existing disappears.

2. **No prompt to connect a mailbox after registration**
   **Problem:** A brand-new user had no signal to connect Gmail/Outlook — the connect buttons only
   existed inside Settings, which nothing pointed them to.
   **Fix:** The Dashboard now shows a "👋 Connect your email to get started" banner with
   Gmail/Outlook connect buttons whenever `GET /accounts` comes back empty, reusing the existing
   `loginWithGoogle()`/`connectOutlook()` handlers. Note: the OAuth popup still requires a real
   click (browsers block un-gestured popups), so this surfaces the prompt immediately rather than
   making the connection fully silent/automatic. Pairs with Backend Fixed #1's primary-account flag.

3. **No visibility into broken/expired account connections**
   **Problem:** Settings always rendered a connected account as "✅ Connected" even when its
   refresh token had died and sync had silently stopped working for it (see Backend Fixed #2).
   **Fix:** Settings now shows a "⭐ Primary" badge on the primary account, and for any account with
   `needsReauth: true` a "⚠️ Needs reconnecting" warning plus a one-click Reconnect button (reruns
   the existing connect flow for that provider). A toast also fires once per session on load if any
   connected account needs reauth.

4. **Sidebar started collapsed instead of open**
   **Problem:** `sidebarOpen` defaulted to `false` and was never persisted, so every login/reload
   left the sidebar collapsed, requiring a manual click on the floating hamburger button.
   **Fix:** Defaults to `true`; an explicit collapse is now remembered via `localStorage`
   (`mailySidebarOpen`, same pattern as `theme`) so it stays how the user last left it.

5. **Clicking a mail on the Dashboard did nothing until you switched to Inbox**
   **Problem:** Dashboard's email rows called `openEmailDetail(email)` directly without switching
   `activeTab`, so the detail view (gated on `activeTab === 'inbox'`) never appeared; the user had
   to separately click "Inbox," landing straight in the now-already-open detail view instead of the
   list.
   **Fix:** Both Dashboard click handlers now do `setActiveTab('inbox'); openEmailDetail(email);`,
   mirroring the pattern `openEmailFromCategoryItem` already used.

6. **"Approve & Create" button in the Smart Category wizard collided with the floating Sync button**
   **Problem:** The wizard's action row and the global "Sync with Server" floating button both sit
   bottom-right; the sync button's visibility guard only excluded the Compose tab, not the wizard.
   **Fix:** Guard extended to `activeTab !== 'compose' && !categoryWizard` — the sync button now
   hides whenever the wizard is open, regardless of which tab it was opened from.

7. **No "Sent" tab — sent mail had nowhere to go**
   **Problem:** There was no Sent view anywhere in the frontend, even though the backend has tagged
   `direction: 'sent'|'received'` on every email since Backend Fixed #3.
   **Fix:** Added a Sent tab/nav item — same list-card layout as Inbox, filtered to
   `direction === 'sent'`, sorted newest-first (existing `sortEmailsByDate`), searchable by subject/
   recipient/content (shows "To: ..." instead of a sender line, since the sender is always you).
   Clicking a sent email reuses the existing Inbox detail/thread view (`setActiveTab('inbox');
   openEmailDetail(email);`, same mechanism Dashboard and Smart Categories already use) rather than
   duplicating that whole thread UI a second time — its "← Back to Inbox" button is accurate for
   where it returns to. Did not build a from-scratch detail view scoped to Sent specifically.
