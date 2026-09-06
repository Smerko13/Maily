import { useState, useEffect, useRef } from "react";
import { Authenticator } from "@aws-amplify/ui-react";
import { fetchAuthSession } from 'aws-amplify/auth';
import { useGoogleLogin } from '@react-oauth/google';
import CategoryWizard from './components/CategoryWizard';
import EmailSelectionPicker from './components/EmailSelectionPicker';
import TravelTripsView from './components/TravelTripsView';
import { CategoryFieldValue, formatFieldValueText } from './components/fields/CategoryFieldValue';
import {
  LayoutDashboard, Inbox as InboxIcon, Send, Sparkles, Truck, BarChart3, Settings,
  LogOut, Sun, Moon, PanelLeftClose, PanelLeftOpen, RefreshCw, SquarePen,
} from 'lucide-react';
import './App.css';
import Compose, { type ComposeSeed } from './Compose';

interface Attachment {
  id: string;
  filename: string;
  mimeType: string;
  size: number;
}

export type Provider = 'gmail' | 'outlook';

export interface Email {
  emailId?: string;
  subject: string;
  from?: string;
  fromAddress?: string;
  to?: string[];
  cc?: string[];
  content: string;
  summary?: string;
  status?: string;
  direction?: 'sent' | 'received';
  provider?: Provider;
  providerEmail?: string;
  attachments?: Attachment[];
  threadId?: string;
  inReplyTo?: string;
  messageId?: string;
  receivedAt?: string;
  bodyText?: string | null;
  bodyHtml?: string | null;
  labels?: string[];
  providerLabels?: string[];
  categoryItemId?: string;
}

export interface ConnectedAccount {
  email: string;
  provider: Provider;
  isPrimary?: boolean;
  needsReauth?: boolean;
}

export interface LabelDef {
  id: string;
  name: string;
  description: string;
  color: string;
}

// A label whose id starts with "custom#" belongs to this user and can be edited/deleted; anything
// else is an app-wide preset (see PRESET_LABELS in backend_lambda.py) and is shown read-only.
function isCustomLabel(label: LabelDef): boolean {
  return label.id.startsWith('custom#');
}

// DynamoDB's Query returns items ordered by the emailId sort key (lexicographic, not chronological),
// so the inbox has to be sorted client-side. Newest first; emails missing receivedAt sink to the bottom.
export function sortEmailsByDate(emails: Email[]): Email[] {
  return [...emails].sort((a, b) => {
    const aTime = a.receivedAt ? new Date(a.receivedAt).getTime() : -Infinity;
    const bTime = b.receivedAt ? new Date(b.receivedAt).getTime() : -Infinity;
    return bTime - aTime;
  });
}

// A card's actual placement in the UI: 'active'/'done' are either computed from the schema's
// completionRule or forced by the mark-done/restore buttons; 'trashed' is always manual. Manual states
// win over the computed one and never get reset by new email data merging in — see effectiveState.
export type CategoryItemState = 'active' | 'done' | 'trashed';

// A user-created "trip" wrapper for the built-in Travel category — just a name + date range. Which
// items belong to it is never stored; it's computed (a travel item's startDate falling inside
// [startDate, endDate]) so editing a trip's dates re-groups items with no migration. See tripForItem
// in TravelTripsView.
export interface TravelTrip {
  tripId: string;
  name: string;
  startDate: string;
  endDate: string;
}

// The built-in category id this special trip-grouping UI applies to — everything else about "travel"
// (fields, matchKeys, rules, extraction) is identical to any other category.
export const TRAVEL_CATEGORY_TYPE_ID = 'travel';

export interface CategoryItem {
  itemId: string;
  categoryType: string;
  fields: Record<string, string | null>;
  contributingEmailIds: string[];
  createdAt: string;
  updatedAt: string;
  lastUpdatedFromEmailAt?: string;
  isComplete: boolean;
  isAtRisk: boolean;
  manualState: 'done' | 'trashed' | null;
  effectiveState: CategoryItemState;
}

export type CategoryFieldType = 'string' | 'number' | 'date' | 'enum' | 'boolean';

// Optional display hint on top of `type`, driving which widget a field renders with — e.g. a number
// field formatted "currency" shows as $42.99 instead of a bare 42.99. Must be valid for its own type.
export type CategoryFieldFormat = 'currency' | 'percent' | 'url' | 'relative-date';

export interface CategoryFieldDef {
  key: string;
  label: string;
  type: CategoryFieldType;
  hint?: string;
  values?: string[];
  sticky?: boolean;
  format?: CategoryFieldFormat;
}

export interface CategoryRule {
  // "date_passed" is unconditional (a date has simply gone by, e.g. an event's date) — field/values
  // don't apply to it. The other two both need a status field: "field_equals" alone, or
  // "date_passed_without" for "overdue on a date without reaching that status yet".
  type: 'field_equals' | 'date_passed_without' | 'date_passed';
  field?: string;
  dateField?: string;
  values?: string[];
}

// The schema-driven metadata for one category type (built-in or custom), as returned by
// GET /category-types — this replaces what used to be a hardcoded frontend lookup table, so a new
// category type (built-in or user-created via the wizard) needs zero frontend code changes to render.
export interface CategoryTypeMeta {
  id: string;
  label: string;
  icon: string;
  classifierDescription: string;
  fields: CategoryFieldDef[];
  matchKeys: string[];
  // "OR" (default): matching any one matchKeys field is enough to be the same item. "AND": every
  // matchKeys field must match together — for categories where no single field is unique on its own.
  keyMode: 'OR' | 'AND';
  titleTemplate: string;
  primaryDateField: string;
  cardFields: string[];
  completionRule: CategoryRule | null;
  atRiskRule: CategoryRule | null;
  automations: unknown[];
  schemaVersion: number;
  isBuiltIn: boolean;
}

export const FALLBACK_CATEGORY_TYPE_META: CategoryTypeMeta = {
  id: '', label: '', icon: '🏷️', classifierDescription: '', fields: [], matchKeys: [], keyMode: 'OR',
  titleTemplate: '', primaryDateField: '', cardFields: [], completionRule: null, atRiskRule: null,
  automations: [], schemaVersion: 1, isBuiltIn: true,
};

export function categoryTypeMeta(categoryType: string, catalog: Record<string, CategoryTypeMeta>): CategoryTypeMeta {
  return catalog[categoryType] ?? { ...FALLBACK_CATEGORY_TYPE_META, id: categoryType, label: categoryType, titleTemplate: categoryType };
}

// Naive {fieldKey} placeholder substitution, deliberately not a full templating engine — also cleans
// up leftover separators (e.g. a dangling " — ") left behind when a referenced field is empty. Values
// are formatted per their field def (so a date field shows "May 26, 2027, 8:30 PM", not a raw ISO
// string with a literal "T" in it) — falls back to the raw value for a placeholder with no matching field.
export function renderTitleTemplate(template: string, fields: CategoryItem['fields'], fieldDefs: CategoryFieldDef[]): string {
  if (!template) return '';
  const rendered = template.replace(/\{(\w+)\}/g, (_, key) => {
    const value = fields[key];
    if (!value) return '';
    const fieldDef = fieldDefs.find(f => f.key === key);
    return fieldDef ? formatFieldValueText(fieldDef, value) : value;
  });
  return rendered.replace(/^[\s—-]+|[\s—-]+$/g, '').replace(/\s{2,}/g, ' ').trim();
}

export function categoryItemTitle(meta: CategoryTypeMeta, fields: CategoryItem['fields']): string {
  return renderTitleTemplate(meta.titleTemplate, fields, meta.fields) || meta.label || 'Untitled';
}

export function CategoryItemStatusBadge({ item }: { item: CategoryItem }) {
  if (item.effectiveState === 'done') return <span className="status-badge status-done">✅ Done</span>;
  if (item.effectiveState === 'trashed') return <span className="status-badge status-trashed">🗑️ Trashed</span>;
  if (item.isAtRisk) return <span className="status-badge status-at-risk">⚠️ At risk</span>;
  return <span className="status-badge status-in-progress">{item.fields.status || 'In progress'}</span>;
}

// Fully schema-driven: which lines appear on a card is driven by meta.cardFields (set either by the
// built-in CATEGORY_TYPES definition, or by the user in the Category Wizard) rather than hardcoded
// per-type JSX — this is what lets a wizard-created category render as well as the built-in ones.
// Each value is rendered through CategoryFieldValue, which dispatches on the field's type/format
// (currency, a link, a status badge, a relative date, ...) instead of always showing plain text.
export function CategoryItemCard({ item, meta, onClick }: { item: CategoryItem; meta: CategoryTypeMeta; onClick: () => void }) {
  const primaryDate = meta.primaryDateField ? item.fields[meta.primaryDateField] : undefined;
  const primaryDateField = meta.fields.find(f => f.key === meta.primaryDateField);
  return (
    <div className="category-item-card" onClick={onClick}>
      <div className="category-item-card-header">
        <span className="category-item-card-title">{meta.icon} {categoryItemTitle(meta, item.fields)}</span>
        <CategoryItemStatusBadge item={item} />
      </div>
      {meta.cardFields.filter(key => key !== meta.primaryDateField).map(key => {
        const value = item.fields[key];
        if (!value) return null;
        const fieldDef = meta.fields.find(f => f.key === key);
        return (
          <p key={key} className="category-item-card-line">
            {fieldDef?.label ?? key}: {fieldDef ? <CategoryFieldValue fieldDef={fieldDef} value={value} /> : value}
          </p>
        );
      })}
      {primaryDate && (
        <p className="category-item-card-line">
          📅 {primaryDateField?.label || 'Date'}: {primaryDateField ? <CategoryFieldValue fieldDef={primaryDateField} value={primaryDate} /> : primaryDate}
        </p>
      )}
    </div>
  );
}

function providerLabel(provider?: Provider): string {
  return provider === 'outlook' ? '📨 Outlook' : '📧 Gmail';
}

interface EmailBody {
  text: string | null;
  html: string | null;
}

interface Stats {
  total: number;
  unread: number;
  read: number;
  top_senders: { sender: string; count: number }[];
}

interface Toast { id: number; text: string; type: 'success' | 'error' | 'info'; }

type ThemeId = 'indigo' | 'ocean' | 'rose' | 'emerald' | 'midnight';

type DraftTone = 'formal' | 'friendly' | 'brief';

const DRAFT_TONES: { id: DraftTone; label: string }[] = [
  { id: 'formal',   label: 'Formal' },
  { id: 'friendly', label: 'Friendly' },
  { id: 'brief',    label: 'Brief' },
];

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

// id 'indigo' is a historical key (data-theme="indigo" in App.css, localStorage values) — its
// actual accent color is teal now, hence the label mismatch here.
const THEMES: { id: ThemeId; label: string }[] = [
  { id: 'indigo',   label: 'Teal'     },
  { id: 'ocean',    label: 'Ocean'    },
  { id: 'rose',     label: 'Rose'     },
  { id: 'emerald',  label: 'Emerald'  },
  { id: 'midnight', label: 'Midnight' },
];

function App() {
  const [emails, setEmails] = useState<Email[]>([]); // the array of email objects displayed in the inbox
  const [loading, setLoading] = useState<boolean>(false); // true/false to disable the Sync button while fetching
  const [activeTab, setActiveTab] = useState<'dashboard' | 'inbox' | 'sent' | 'compose' | 'settings' | 'stats' | 'drafting' | 'categories'>('dashboard'); // which tab is visible
  const [stats, setStats] = useState<Stats | null>(null);
  const [statsLoading, setStatsLoading] = useState<boolean>(false);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const showToast = (text: string, type: 'success' | 'error' | 'info' = 'info') => {
    const id = Date.now();
    setToasts(prev => [...prev, { id, text, type }]);
    setTimeout(() => setToasts(prev => prev.filter(t => t.id !== id)), 4000);
  };
  const [selectedEmailIndex, setSelectedEmailIndex] = useState<number | null>(null); // index of the email selected for drafting
  const [draft, setDraft] = useState<string>(''); // the AI-generated reply draft
  const [draftLoading, setDraftLoading] = useState<boolean>(false);
  const [draftTone, setDraftTone] = useState<DraftTone>('formal');
  const [exportLoading, setExportLoading] = useState<boolean>(false);
  const [exportUrl, setExportUrl] = useState<string>('');
  const [fetchLimit, setFetchLimit] = useState<number>(
    () => parseInt(localStorage.getItem('mailyFetchLimit') ?? '10', 10)
  );
  const [fetchLimitSaving, setFetchLimitSaving] = useState<boolean>(false);
  const [connectedAccounts, setConnectedAccounts] = useState<ConnectedAccount[]>([]);
  // Distinguishes "haven't checked yet" from "checked, and there are genuinely zero accounts" —
  // without this the connect-prompt banner would flash on every load before data arrives.
  const [accountsLoaded, setAccountsLoaded] = useState<boolean>(false);
  const reauthToastShownRef = useRef(false);
  const [signature, setSignature] = useState<string>('');
  const [signatureSaving, setSignatureSaving] = useState<boolean>(false);
  const [composeSeed, setComposeSeed] = useState<ComposeSeed | undefined>();
  const [composeKey, setComposeKey] = useState<number>(0);
  const [accountFilter, setAccountFilter] = useState<string>('all');
  const [theme, setTheme] = useState<ThemeId>(() => {
    const stored = localStorage.getItem('mailyTheme') as ThemeId | null;
    if (stored) return stored;
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'midnight' : 'indigo';
  });
  // Opens by default on every login/page load; if the user explicitly collapses it, that choice is
  // remembered (same localStorage pattern as `theme`) instead of resetting open on the next reload.
  const [sidebarOpen, setSidebarOpen] = useState<boolean>(() => {
    const stored = localStorage.getItem('mailySidebarOpen');
    return stored === null ? true : stored === 'true';
  });

  // Quick light/dark toggle, layered on top of the 5-theme picker: 'midnight' is already a full dark
  // palette, so toggling just swaps to/from it and remembers whichever light theme you were on before.
  const toggleDarkMode = () => {
    if (theme === 'midnight') {
      const lastLight = (localStorage.getItem('mailyLastLightTheme') as ThemeId) || 'indigo';
      setTheme(lastLight);
    } else {
      localStorage.setItem('mailyLastLightTheme', theme);
      setTheme('midnight');
    }
  };
  const [expandedEmailId, setExpandedEmailId] = useState<string | null>(null);
  const [emailBodies, setEmailBodies] = useState<Record<string, EmailBody>>({});
  const [bodyLoadingId, setBodyLoadingId] = useState<string | null>(null);
  const [attachmentLoadingId, setAttachmentLoadingId] = useState<string | null>(null);
  const [threadEmails, setThreadEmails] = useState<Record<string, Email[]>>({});
  const [threadLoadingId, setThreadLoadingId] = useState<string | null>(null);
  const [openedEmail, setOpenedEmail] = useState<Email | null>(null); // set = showing the detail view instead of the inbox list

  const [inboxSearch, setInboxSearch] = useState<string>('');
  const [sentSearch, setSentSearch] = useState<string>('');
  // Semantic search results from the /search endpoint (LLM-ranked emailIds) for the query they were
  // fetched for. Compared against the current trimmed query before use, so a fast subsequent edit
  // never shows results for a stale query while the debounced request for the new one is in flight.
  const [semanticSearchQuery, setSemanticSearchQuery] = useState<string>('');
  const [semanticSearchIds, setSemanticSearchIds] = useState<string[]>([]);
  const [semanticSearchLoading, setSemanticSearchLoading] = useState<boolean>(false);
  const [labels, setLabels] = useState<LabelDef[]>([]);
  const [labelFilter, setLabelFilter] = useState<string>('all');
  const [labelSaving, setLabelSaving] = useState<boolean>(false);
  const [editingLabelId, setEditingLabelId] = useState<string | null>(null);
  const [editLabelDraft, setEditLabelDraft] = useState<{ name: string; description: string; color: string }>({ name: '', description: '', color: '#0d9488' });
  const [newLabelName, setNewLabelName] = useState<string>('');
  const [newLabelDescription, setNewLabelDescription] = useState<string>('');
  const [newLabelColor, setNewLabelColor] = useState<string>('#0d9488');

  const [categoryItems, setCategoryItems] = useState<CategoryItem[]>([]);
  const [categoryItemsLoading, setCategoryItemsLoading] = useState<boolean>(false);
  const [openedCategoryItem, setOpenedCategoryItem] = useState<CategoryItem | null>(null);
  // Non-null = drilled into one category type's full active/done grid; null = the row-per-category
  // overview. Independent of openedCategoryItem so "back" from an item detail returns to whichever of
  // these the user came from (drill-in or the overview), not always the overview.
  const [openedCategoryType, setOpenedCategoryType] = useState<string | null>(null);
  const [categoryItemEmails, setCategoryItemEmails] = useState<Email[]>([]);
  const [categoryItemLoading, setCategoryItemLoading] = useState<boolean>(false);
  const [returnToCategoryItemId, setReturnToCategoryItemId] = useState<string | null>(null);

  // Built-in + this user's custom category types, keyed by id — replaces what used to be a hardcoded
  // frontend metadata table, so the Category Wizard's custom types render with zero frontend changes.
  const [categoryTypeCatalog, setCategoryTypeCatalog] = useState<Record<string, CategoryTypeMeta>>({});
  // Non-null = the wizard panel is showing instead of the Smart Categories grid. 'replace' pre-loads
  // wizardExisting as the starting draft (editing an already-created custom category).
  const [categoryWizard, setCategoryWizard] = useState<{ mode: 'create' | 'replace'; existing?: CategoryTypeMeta } | null>(null);
  // Wizard reference-email selection — lives here (not inside CategoryWizard) because it's shared with
  // the full-inbox EmailSelectionPicker overlay below, a sibling of the wizard rather than a child.
  const [categoryReferenceIds, setCategoryReferenceIds] = useState<string[]>([]);
  const [emailPickerOpen, setEmailPickerOpen] = useState(false);
  const [emailPickerSnapshot, setEmailPickerSnapshot] = useState<string[]>([]);
  const MAX_CATEGORY_REFERENCE_EMAILS = 3;
  const toggleCategoryReferenceId = (emailId: string) => setCategoryReferenceIds(prev =>
    prev.includes(emailId)
      ? prev.filter(id => id !== emailId)
      : (prev.length < MAX_CATEGORY_REFERENCE_EMAILS ? [...prev, emailId] : prev)
  );
  // Which email's manual-classify panel is expanded (inbox detail thread view). Only one open at a time.
  const [classifyMenuEmailId, setClassifyMenuEmailId] = useState<string | null>(null);
  const [classifySaving, setClassifySaving] = useState<boolean>(false);
  const [categoryHintDrafts, setCategoryHintDrafts] = useState<Record<string, string>>({}); // Settings' "add a hint" inputs, keyed by categoryTypeId

  useEffect(() => {
    localStorage.setItem('mailyTheme', theme);
  }, [theme]);

  useEffect(() => {
    localStorage.setItem('mailySidebarOpen', String(sidebarOpen));
  }, [sidebarOpen]);

  // Fetches the current connected-accounts list fresh from the backend. Used on page load, and again
  // after connecting Outlook — re-fetching rather than optimistically merging local state avoids a race
  // where the mount-time load (fast) and the Outlook OAuth exchange (slow, multi-hop) resolve out of
  // order and the later one clobbers the earlier one's result.
  const loadConnectedAccounts = async () => {
    try {
      const session = await fetchAuthSession();
      const token = session.tokens?.idToken?.toString();
      if (!token) return;
      const response = await fetch(`${import.meta.env.VITE_API_BASE_URL}/accounts`, {
        headers: { Authorization: `Bearer ${token}` }
      });
      if (!response.ok) return;
      const data = await response.json();
      setConnectedAccounts(data.accounts || []);
      if (data.settings) {
        setSignature(data.settings.signature || '');
        if (data.settings.email_fetch_limit) setFetchLimit(data.settings.email_fetch_limit);
      }
    } catch {
      // silently fail
    } finally {
      setAccountsLoaded(true);
    }
  };

  // Surface expired/revoked provider access as soon as we know about it, rather than leaving it
  // to be discovered only if the user happens to open Settings. Shown once per session per load
  // that reveals the problem, not on every re-render.
  useEffect(() => {
    if (!accountsLoaded) return;
    const needingReauth = connectedAccounts.filter(a => a.needsReauth);
    if (needingReauth.length > 0 && !reauthToastShownRef.current) {
      reauthToastShownRef.current = true;
      showToast(
        needingReauth.length === 1
          ? `${needingReauth[0].email} needs to be reconnected — see Settings.`
          : `${needingReauth.length} accounts need to be reconnected — see Settings.`,
        'error'
      );
    }
  }, [accountsLoaded, connectedAccounts]);

  // Fetches this user's full label catalog (app presets + their own custom labels).
  const loadLabels = async () => {
    try {
      const session = await fetchAuthSession();
      const token = session.tokens?.idToken?.toString();
      if (!token) return;
      const response = await fetch(`${import.meta.env.VITE_API_BASE_URL}/labels`, {
        headers: { Authorization: `Bearer ${token}` }
      });
      if (!response.ok) return;
      const data = await response.json();
      setLabels(data.labels || []);
    } catch {
      // silently fail
    }
  };

  // Fetches this user's full category-type catalog (built-ins + their own custom types) — the
  // schema-driven metadata every generic card/detail render and the wizard depend on.
  const loadCategoryTypeCatalog = async () => {
    try {
      const session = await fetchAuthSession();
      const token = session.tokens?.idToken?.toString();
      if (!token) return;
      const response = await fetch(`${import.meta.env.VITE_API_BASE_URL}/category-types`, {
        headers: { Authorization: `Bearer ${token}` }
      });
      if (!response.ok) return;
      const data = await response.json();
      const catalog: Record<string, CategoryTypeMeta> = {};
      (data.categoryTypes || []).forEach((ct: CategoryTypeMeta) => { catalog[ct.id] = ct; });
      setCategoryTypeCatalog(catalog);
    } catch {
      // silently fail
    }
  };

  // Load connected email accounts (Gmail + Outlook), the label catalog, and the category-type catalog
  // on page load. Also trigger a sync so the inbox reflects the latest mail as soon as the user opens/
  // refreshes the app, rather than just showing whatever was last synced — the loadEmails effect below
  // shows the cached DB state immediately, and this overwrites it once the sync's actual provider
  // round-trip completes.
  // hasMountedRef guards against React StrictMode's dev-only double-invoke of mount effects, which
  // would otherwise fire two real /sync calls (double provider hits, double AI classify) on every load.
  const hasMountedRef = useRef(false);
  useEffect(() => {
    if (hasMountedRef.current) return;
    hasMountedRef.current = true;
    loadConnectedAccounts();
    loadLabels();
    loadCategoryTypeCatalog();
    fetchFromBackend();
    // This effect is intentionally mount-only; the ref prevents StrictMode from starting two syncs.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Handle the redirect back from Microsoft after the user approves (or cancels) Outlook access.
  // Full-page redirect flow: Azure has no popup/postMessage trick like Google's, so we land back
  // on our own origin at /oauth/outlook/callback with ?code=&state= and exchange the code server-side.
  useEffect(() => {
    // Amplify Hosting 301-redirects every extensionless path to add a trailing slash before
    // serving index.html, so the browser lands on '/oauth/outlook/callback/' (with slash) — strip
    // it before comparing so this still matches.
    if (window.location.pathname.replace(/\/+$/, '') !== '/oauth/outlook/callback') return;

    const handleOutlookCallback = async () => {
      const params = new URLSearchParams(window.location.search);
      const code = params.get('code');
      const returnedState = params.get('state');
      const errorParam = params.get('error');
      window.history.replaceState({}, '', '/'); // clean the URL regardless of outcome

      if (errorParam) {
        showToast('Outlook connection was cancelled or failed.', 'error');
        return;
      }

      const expectedState = sessionStorage.getItem('mailyOutlookState');
      sessionStorage.removeItem('mailyOutlookState');
      if (!code || !returnedState || returnedState !== expectedState) {
        showToast('Outlook connection failed: invalid response.', 'error');
        return;
      }

      showToast('Connecting to Outlook...', 'info');
      try {
        const session = await fetchAuthSession();
        const token = session.tokens?.idToken?.toString();
        if (!token) throw new Error('No auth token available');

        const redirectUri = `${window.location.origin}/oauth/outlook/callback`;
        const response = await fetch(`${import.meta.env.VITE_API_BASE_URL}/auth/outlook`, {
          method: 'POST',
          headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
          body: JSON.stringify({ code, redirect_uri: redirectUri })
        });
        if (!response.ok) {
          const errorData = await response.text();
          throw new Error(`HTTP error! status: ${response.status}, details: ${errorData}`);
        }
        const data = await response.json();
        await loadConnectedAccounts(); // refetch fresh rather than trust local state, per the race note above
        showToast(`Connected ${data.email ?? 'Outlook account'} successfully!`, 'success');
        fetchFromBackend(); // pull the new account's mail in immediately rather than waiting for a manual sync
      } catch (error) {
        console.error('Error connecting Outlook account:', error);
        showToast('Error connecting to Outlook. Please try again.', 'error');
      }
    };

    handleOutlookCallback();
    // OAuth codes are single-use, so this callback must only be processed once after the redirect.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Auto-load emails from DynamoDB on page load and whenever the account filter changes
  useEffect(() => {
    const loadEmails = async () => {
      try {
        const session = await fetchAuthSession();
        const token = session.tokens?.idToken?.toString();
        if (!token) return;
        const url = accountFilter === 'all'
          ? `${import.meta.env.VITE_API_BASE_URL}/hello`
          : `${import.meta.env.VITE_API_BASE_URL}/hello?account=${encodeURIComponent(accountFilter)}`;
        const response = await fetch(url, {
          headers: { Authorization: `Bearer ${token}` }
        });
        if (!response.ok) return;
        const data = await response.json();
        // Update even on an empty result — a filtered account with zero matching emails is a valid,
        // real result and should clear the list, not silently leave the previous (unfiltered) one showing.
        if (Array.isArray(data.emails)) setEmails(sortEmailsByDate(data.emails));
      } catch {
        // silently fail — the user can always click Sync manually
      }
    };
    loadEmails();
  }, [accountFilter]);

  // Debounced semantic search: asks the backend (an LLM call over the user's email summaries) which
  // emails match the query in meaning, not just exact substrings. Instant substring filtering below
  // covers the gap while this is in flight or if it fails, so search never goes empty/frozen.
  useEffect(() => {
    const query = inboxSearch.trim();
    if (!query) {
      setSemanticSearchLoading(false);
      return;
    }
    setSemanticSearchLoading(true);
    const handle = setTimeout(async () => {
      try {
        const session = await fetchAuthSession();
        const token = session.tokens?.idToken?.toString();
        if (!token) return;
        const params = new URLSearchParams({ q: query });
        if (accountFilter !== 'all') params.set('account', accountFilter);
        const url = `${import.meta.env.VITE_API_BASE_URL}/search?${params.toString()}`;
        const response = await fetch(url, { headers: { Authorization: `Bearer ${token}` } });
        if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
        const data = await response.json();
        setSemanticSearchQuery(query);
        setSemanticSearchIds(Array.isArray(data.emailIds) ? data.emailIds : []);
      } catch {
        // silently fall back to the plain substring filter — semanticSearchQuery is left stale so it
        // won't be used for this query
      } finally {
        setSemanticSearchLoading(false);
      }
    }, 450);
    return () => clearTimeout(handle);
  }, [inboxSearch, accountFilter]);

  // Google login handler — works for both first account and adding more
 const loginWithGoogle = useGoogleLogin({
    flow: 'auth-code',
   scope: 'https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/gmail.send',
    onSuccess: async (codeResponse) => {
      console.log("Success! Auth Code from Google:", codeResponse.code);
      showToast('Connecting to Google...', 'info');

      try {
        const session = await fetchAuthSession();
        const token = session.tokens?.idToken?.toString();
        if (!token) throw new Error('No auth token available');

        const apiUrl = `${import.meta.env.VITE_API_BASE_URL}/auth/google`;
        
        const response = await fetch(apiUrl, {
          method: 'POST',
          headers: {
            Authorization: `Bearer ${token}`,
            'Content-Type': 'application/json'
          },
          body: JSON.stringify({ code: codeResponse.code }) 
        });
        
        if (!response.ok) {
          const errorData = await response.text();
          throw new Error(`HTTP error! status: ${response.status}, details: ${errorData}`);
        }

        const data = await response.json();
        // Add the newly connected account to the list (avoid duplicates)
        if (data.email) {
          setConnectedAccounts(prev =>
            prev.some(a => a.email === data.email && a.provider === 'gmail')
              ? prev
              : [...prev, { email: data.email, provider: 'gmail' as const }]
          );
        }
        showToast(`Connected ${data.email ?? 'Google account'} successfully!`, 'success');
        fetchFromBackend(); // pull the new account's mail in immediately rather than waiting for a manual sync

      } catch (error) {
        console.error('Error sending code to backend:', error);
        showToast('Error connecting to Google. Please try again.', 'error');
      }
    },
    onError: (errorResponse) => {
      console.error("Google Login Failed:", errorResponse);
      showToast('Error connecting to Google. Please try again.', 'error');
    },
  });

  // Kick off the Outlook connect flow: a full-page redirect to Microsoft's consent screen.
  // A random state value guards against CSRF on the way back (checked in the callback effect above).
  const connectOutlook = () => {
    const state = crypto.randomUUID();
    sessionStorage.setItem('mailyOutlookState', state);
    const redirectUri = `${window.location.origin}/oauth/outlook/callback`;
    const params = new URLSearchParams({
      client_id: import.meta.env.VITE_MICROSOFT_CLIENT_ID,
      response_type: 'code',
      redirect_uri: redirectUri,
      response_mode: 'query',
      scope: 'openid profile email offline_access https://graph.microsoft.com/User.Read https://graph.microsoft.com/Mail.Read https://graph.microsoft.com/Mail.Send',
      state,
      prompt: 'consent'
    });
    window.location.href = `https://login.microsoftonline.com/common/oauth2/v2.0/authorize?${params.toString()}`;
  };

  // Disconnect an email account (Gmail or Outlook)
  const disconnectAccount = async (email: string, provider: Provider) => {
    try {
      const session = await fetchAuthSession();
      const token = session.tokens?.idToken?.toString();
      if (!token) throw new Error('No auth token available');
      const response = await fetch(`${import.meta.env.VITE_API_BASE_URL}/auth/account`, {
        method: 'DELETE',
        headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, provider })
      });
      if (!response.ok) throw new Error('Failed to disconnect');
      setConnectedAccounts(prev => prev.filter(a => !(a.email === email && a.provider === provider)));
      setEmails(prev => prev.filter(e => !(e.providerEmail === email && e.provider === provider)));
      if (accountFilter === email) setAccountFilter('all');
      showToast(`Disconnected ${email}`, 'success');
    } catch {
      showToast('Failed to disconnect account. Please try again.', 'error');
    }
  };

  // Fetch statistics function
  const fetchStats = async () => {
    setStatsLoading(true);
    try {
      const session = await fetchAuthSession();
      const token = session.tokens?.idToken?.toString();
      if (!token) throw new Error('No auth token available');

      const apiUrl = `${import.meta.env.VITE_API_BASE_URL}/stats`;
      const response = await fetch(apiUrl, {
        method: 'GET',
        headers: {
          Authorization: `Bearer ${token}`,
          'Content-Type': 'application/json'
        }
      });
      if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
      const data = await response.json();
      setStats(data);
    } catch (error) {
      console.error('Error fetching stats:', error);
      showToast('Failed to load statistics. Please try again.', 'error');
    } finally {
      setStatsLoading(false);
    }
  };

  // Generate a draft reply for the selected email
  const fetchDraft = async (email: Email, tone: DraftTone) => {
    setDraftLoading(true);
    setDraft('');
    try {
      const session = await fetchAuthSession();
      const token = session.tokens?.idToken?.toString();
      if (!token) throw new Error('No auth token available');

      const apiUrl = `${import.meta.env.VITE_API_BASE_URL}/draft`;
      const response = await fetch(apiUrl, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${token}`,
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({
          subject: email.subject,
          summary: email.summary,
          content: email.content,
          tone
        })
      });
      if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
      const data = await response.json();
      setDraft(data.draft);
      openComposeFromEmail('reply', email, data.draft);
    } catch (error) {
      console.error('Error generating draft:', error);
      setDraft('❌ Failed to generate draft. Please try again.');
    } finally {
      setDraftLoading(false);
    }
  };

  // Generate and download an export of all email summaries from S3
  const fetchExport = async () => {
    setExportLoading(true);
    setExportUrl('');
    try {
      const session = await fetchAuthSession();
      const token = session.tokens?.idToken?.toString();
      if (!token) throw new Error('No auth token available');

      const response = await fetch(`${import.meta.env.VITE_API_BASE_URL}/export`, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${token}`,
          'Content-Type': 'application/json'
        }
      });
      if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
      const data = await response.json();
      setExportUrl(data.download_url);
    } catch (error) {
      console.error('Error exporting emails:', error);
    } finally {
      setExportLoading(false);
    }
  };

  // Loads (if needed) and marks one message as the currently-expanded one within the detail view.
  // If it was synced this session, the backend already delivered the body alongside the rest of the
  // email — use that directly. Otherwise (e.g. after a plain page reload, or an email that already
  // existed and was skipped by a later sync) fall back to fetching it live. The body is never
  // persisted server-side, so this fallback is the only way to see it outside the syncing session.
  // Marks one email as read locally (list, detail view, and any cached thread copies) and persists it
  // to the backend. Fire-and-forget on the network side — the UI already reflects the change optimistically.
  const markAsRead = (emailId: string) => {
    const markRead = (e: Email): Email => (e.emailId === emailId ? { ...e, status: 'read' } : e);
    setEmails(prev => prev.map(markRead));
    setOpenedEmail(prev => (prev ? markRead(prev) : prev));
    setThreadEmails(prev => {
      const next: Record<string, Email[]> = {};
      for (const threadId in prev) next[threadId] = prev[threadId].map(markRead);
      return next;
    });

    (async () => {
      try {
        const session = await fetchAuthSession();
        const token = session.tokens?.idToken?.toString();
        if (!token) return;
        await fetch(`${import.meta.env.VITE_API_BASE_URL}/mark-read`, {
          method: 'POST',
          headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
          body: JSON.stringify({ emailId })
        });
      } catch {
        // best-effort — the UI is already updated; worst case this stays stale server-side until retried
      }
    })();
  };

  const openEmailBody = async (email: Email) => {
    if (!email.emailId) return;
    setExpandedEmailId(email.emailId);
    if (email.status === 'unread') markAsRead(email.emailId);
    if (emailBodies[email.emailId]) return; // already cached

    if (email.bodyText || email.bodyHtml) {
      setEmailBodies(prev => ({ ...prev, [email.emailId as string]: { text: email.bodyText ?? null, html: email.bodyHtml ?? null } }));
      return;
    }

    setBodyLoadingId(email.emailId);
    try {
      const session = await fetchAuthSession();
      const token = session.tokens?.idToken?.toString();
      if (!token) throw new Error('No auth token available');

      const url = `${import.meta.env.VITE_API_BASE_URL}/email-body?emailId=${encodeURIComponent(email.emailId)}`;
      const response = await fetch(url, { headers: { Authorization: `Bearer ${token}` } });
      if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
      const data = await response.json();
      setEmailBodies(prev => ({ ...prev, [email.emailId as string]: { text: data.text, html: data.html } }));
    } catch (error) {
      console.error('Error fetching email body:', error);
      showToast('Failed to load the full email. Please try again.', 'error');
      setExpandedEmailId(null);
    } finally {
      setBodyLoadingId(null);
    }
  };

  // Click-to-open/close one message's body within the detail view's thread list
  const toggleEmailBody = (email: Email) => {
    if (!email.emailId) return;
    if (expandedEmailId === email.emailId) {
      setExpandedEmailId(null);
      return;
    }
    openEmailBody(email);
  };

  // Opens the dedicated detail view for one email. If it's part of a thread, loads every message in
  // that thread (via the threadId GSI) and auto-opens the most recent one — same convention Gmail uses,
  // where only the latest message in a conversation starts expanded and earlier ones are collapsed.
  const openEmailDetail = async (email: Email) => {
    if (!email.emailId) return;
    setOpenedEmail(email);

    let messages: Email[] = [email];

    if (email.threadId) {
      const cached = threadEmails[email.threadId];
      if (cached) {
        messages = cached;
      } else {
        setThreadLoadingId(email.threadId);
        try {
          const session = await fetchAuthSession();
          const token = session.tokens?.idToken?.toString();
          if (token) {
            const url = `${import.meta.env.VITE_API_BASE_URL}/thread?threadId=${encodeURIComponent(email.threadId)}`;
            const response = await fetch(url, { headers: { Authorization: `Bearer ${token}` } });
            if (response.ok) {
              const data = await response.json();
              messages = data.emails && data.emails.length > 0 ? data.emails : [email];
              setThreadEmails(prev => ({ ...prev, [email.threadId as string]: messages }));
            }
          }
        } catch (error) {
          console.error('Error fetching thread:', error);
        } finally {
          setThreadLoadingId(null);
        }
      }
    }

    openEmailBody(messages[messages.length - 1]);
  };

  const goBackToInbox = () => {
    setOpenedEmail(null);
    setExpandedEmailId(null);
    // If we got here by drilling into an email from a Smart Category card, "back" should return
    // there rather than to the plain inbox list.
    if (returnToCategoryItemId) {
      const itemId = returnToCategoryItemId;
      setReturnToCategoryItemId(null);
      setActiveTab('categories');
      openCategoryItemDetail(itemId);
    }
  };

  // Opens an email from within a Smart Category item's detail view, reusing the existing
  // thread/body detail view — remembers where to return to when the user clicks "back".
  const openEmailFromCategoryItem = (email: Email) => {
    setReturnToCategoryItemId(openedCategoryItem?.itemId ?? null);
    setActiveTab('inbox');
    openEmailDetail(email);
  };

  // Fetch a presigned S3 download URL for an attachment and open it
  const downloadAttachment = async (emailId: string, attachment: Attachment) => {
    setAttachmentLoadingId(attachment.id);
    try {
      const session = await fetchAuthSession();
      const token = session.tokens?.idToken?.toString();
      if (!token) throw new Error('No auth token available');

      const url = `${import.meta.env.VITE_API_BASE_URL}/attachment?emailId=${encodeURIComponent(emailId)}&attachmentId=${encodeURIComponent(attachment.id)}`;
      const response = await fetch(url, { headers: { Authorization: `Bearer ${token}` } });
      if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
      const data = await response.json();
      window.open(data.download_url, '_blank');
    } catch (error) {
      console.error('Error downloading attachment:', error);
      showToast(`Failed to download ${attachment.filename}. Please try again.`, 'error');
    } finally {
      setAttachmentLoadingId(null);
    }
  };

  // Manually classify one email: toggle labels on/off, and/or assign or clear its smart category.
  // Unlike AI auto-classification, this runs on-demand from a single user action — it's how existing
  // mail gets attached to a brand-new custom category (no automated backfill), and also doubles as a
  // "fix a wrong/missing AI classification" tool. Patches local state in place so chips/cards update
  // without a full refetch.
  const classifyEmail = async (email: Email, updates: { addLabels?: string[]; removeLabels?: string[]; categoryType?: string | null }) => {
    if (!email.emailId) return;
    setClassifySaving(true);
    try {
      const session = await fetchAuthSession();
      const token = session.tokens?.idToken?.toString();
      if (!token) throw new Error('No auth token available');

      const response = await fetch(`${import.meta.env.VITE_API_BASE_URL}/email-classify`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({ emailId: email.emailId, ...updates })
      });
      if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
      const data = await response.json();

      const patch = (list: Email[]) => list.map(e => e.emailId === email.emailId
        ? { ...e, labels: data.labels, categoryItemId: data.categoryItem?.itemId ?? (updates.categoryType === null ? undefined : e.categoryItemId) }
        : e);
      setEmails(prev => patch(prev));
      if (email.threadId) {
        setThreadEmails(prev => prev[email.threadId as string] ? { ...prev, [email.threadId as string]: patch(prev[email.threadId as string]) } : prev);
      }
      setOpenedEmail(prev => prev && prev.emailId === email.emailId
        ? { ...prev, labels: data.labels, categoryItemId: data.categoryItem?.itemId ?? (updates.categoryType === null ? undefined : prev.categoryItemId) }
        : prev);
      if (updates.categoryType !== undefined) {
        // A category was assigned/cleared — the Smart Categories tab's cached list is now stale;
        // force a refetch next time it's viewed rather than guessing at how to patch it in place.
        setCategoryItems([]);
      }
      showToast('Email classified', 'success');
    } catch (error) {
      console.error('Error classifying email:', error);
      showToast('Failed to classify email. Please try again.', 'error');
    } finally {
      setClassifySaving(false);
    }
  };

  // Save the email fetch limit preference to the backend
  const saveFetchLimit = async (limit: number) => {
    setFetchLimitSaving(true);
    try {
      const session = await fetchAuthSession();
      const token = session.tokens?.idToken?.toString();
      if (!token) throw new Error('No auth token available');

      const response = await fetch(`${import.meta.env.VITE_API_BASE_URL}/settings`, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${token}`,
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({ email_fetch_limit: limit })
      });
      if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
      localStorage.setItem('mailyFetchLimit', String(limit));
      showToast(`Fetch limit saved: ${limit} emails per sync`, 'success');
    } catch (error) {
      console.error('Error saving settings:', error);
      showToast('Failed to save settings. Please try again.', 'error');
    } finally {
      setFetchLimitSaving(false);
    }
  };

  const saveSignature = async () => {
    setSignatureSaving(true);
    try {
      const session = await fetchAuthSession();
      const token = session.tokens?.idToken?.toString();
      if (!token) throw new Error('No auth token available');
      const response = await fetch(`${import.meta.env.VITE_API_BASE_URL}/settings`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({ signature }),
      });
      if (!response.ok) throw new Error('Failed to save signature');
      showToast('Signature saved.', 'success');
    } catch (error) {
      console.error('Error saving signature:', error);
      showToast('Failed to save signature.', 'error');
    } finally {
      setSignatureSaving(false);
    }
  };

  // Create a new custom label
  const createLabel = async () => {
    if (!newLabelName.trim() || !newLabelDescription.trim()) {
      showToast('Name and description are required.', 'error');
      return;
    }
    setLabelSaving(true);
    try {
      const session = await fetchAuthSession();
      const token = session.tokens?.idToken?.toString();
      if (!token) throw new Error('No auth token available');

      const response = await fetch(`${import.meta.env.VITE_API_BASE_URL}/labels`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: newLabelName.trim(), description: newLabelDescription.trim(), color: newLabelColor })
      });
      if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
      setNewLabelName('');
      setNewLabelDescription('');
      setNewLabelColor('#0d9488');
      await loadLabels();
      showToast('Label created', 'success');
    } catch (error) {
      console.error('Error creating label:', error);
      showToast('Failed to create label. Please try again.', 'error');
    } finally {
      setLabelSaving(false);
    }
  };

  // Save edits to an existing custom label
  const updateLabel = async (labelId: string, updates: { name?: string; description?: string; color?: string }) => {
    setLabelSaving(true);
    try {
      const session = await fetchAuthSession();
      const token = session.tokens?.idToken?.toString();
      if (!token) throw new Error('No auth token available');

      const response = await fetch(`${import.meta.env.VITE_API_BASE_URL}/labels`, {
        method: 'PUT',
        headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({ labelId, ...updates })
      });
      if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
      await loadLabels();
      setEditingLabelId(null);
      showToast('Label updated', 'success');
    } catch (error) {
      console.error('Error updating label:', error);
      showToast('Failed to update label. Please try again.', 'error');
    } finally {
      setLabelSaving(false);
    }
  };

  // Delete a custom label
  const deleteLabel = async (labelId: string) => {
    setLabelSaving(true);
    try {
      const session = await fetchAuthSession();
      const token = session.tokens?.idToken?.toString();
      if (!token) throw new Error('No auth token available');

      const response = await fetch(`${import.meta.env.VITE_API_BASE_URL}/labels`, {
        method: 'DELETE',
        headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({ labelId })
      });
      if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
      if (labelFilter === labelId) setLabelFilter('all');
      await loadLabels();
      showToast('Label deleted', 'success');
    } catch (error) {
      console.error('Error deleting label:', error);
      showToast('Failed to delete label. Please try again.', 'error');
    } finally {
      setLabelSaving(false);
    }
  };

  // Deletes a custom category type and its tracked items (cascades server-side).
  const deleteCategoryType = async (categoryTypeId: string) => {
    if (!window.confirm('Delete this category? Its tracked items will be deleted too.')) return;
    try {
      const session = await fetchAuthSession();
      const token = session.tokens?.idToken?.toString();
      if (!token) throw new Error('No auth token available');

      const response = await fetch(`${import.meta.env.VITE_API_BASE_URL}/category-types`, {
        method: 'DELETE',
        headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({ categoryTypeId })
      });
      if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
      await loadCategoryTypeCatalog();
      setCategoryItems([]); // stale — force a refetch next time the tab is viewed
      showToast('Category deleted', 'success');
    } catch (error) {
      console.error('Error deleting category type:', error);
      showToast('Failed to delete category. Please try again.', 'error');
    }
  };

  // Lightweight edit: appends free text to a category's classifier description, helping the AI catch
  // more matching emails without going through the full wizard (which is reserved for structural changes).
  const appendCategoryClassifierHint = async (categoryTypeId: string) => {
    const hint = (categoryHintDrafts[categoryTypeId] || '').trim();
    if (!hint) return;
    try {
      const session = await fetchAuthSession();
      const token = session.tokens?.idToken?.toString();
      if (!token) throw new Error('No auth token available');

      const response = await fetch(`${import.meta.env.VITE_API_BASE_URL}/category-types`, {
        method: 'PATCH',
        headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({ categoryTypeId, appendClassifierHint: hint })
      });
      if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
      setCategoryHintDrafts(prev => ({ ...prev, [categoryTypeId]: '' }));
      await loadCategoryTypeCatalog();
      showToast('Hint added', 'success');
    } catch (error) {
      console.error('Error updating category type:', error);
      showToast('Failed to add hint. Please try again.', 'error');
    }
  };

  // Fetches this user's tracked smart-category items. The backend only ever returns active or done
  // items per call (never trashed — those are soft-deleted and permanently hidden), so both states are
  // fetched in parallel to populate the Active grid and the collapsible "Completed" section at once.
  const loadCategoryItems = async () => {
    setCategoryItemsLoading(true);
    try {
      const session = await fetchAuthSession();
      const token = session.tokens?.idToken?.toString();
      if (!token) throw new Error('No auth token available');

      const base = import.meta.env.VITE_API_BASE_URL;
      const [activeRes, doneRes] = await Promise.all([
        fetch(`${base}/smart-categories?state=active`, { headers: { Authorization: `Bearer ${token}` } }),
        fetch(`${base}/smart-categories?state=done`, { headers: { Authorization: `Bearer ${token}` } }),
      ]);
      if (!activeRes.ok) throw new Error(`HTTP error! status: ${activeRes.status}`);
      if (!doneRes.ok) throw new Error(`HTTP error! status: ${doneRes.status}`);
      const activeData = await activeRes.json();
      const doneData = await doneRes.json();
      setCategoryItems([...(activeData.items || []), ...(doneData.items || [])]);
    } catch (error) {
      console.error('Error loading smart categories:', error);
      showToast('Failed to load smart categories. Please try again.', 'error');
    } finally {
      setCategoryItemsLoading(false);
    }
  };

  // Mark done / restore to active / trash — the three manual card controls. manualState: null clears
  // a "done" override back to active (governed by the schema's rule again); "trashed" has no restore
  // button in this UI today (the backend endpoint itself is permissive — soft delete only — so adding
  // one later needs no backend change), by design (see SMART_CATEGORIES_DESIGN.md).
  const updateCategoryItemState = async (itemId: string, manualState: 'done' | 'trashed' | null) => {
    try {
      const session = await fetchAuthSession();
      const token = session.tokens?.idToken?.toString();
      if (!token) throw new Error('No auth token available');

      const url = `${import.meta.env.VITE_API_BASE_URL}/smart-category?itemId=${encodeURIComponent(itemId)}`;
      const response = await fetch(url, {
        method: 'PATCH',
        headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({ manualState }),
      });
      if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);

      if (manualState === 'trashed') {
        goBackToCategoryItems();
        showToast('Card deleted', 'success');
      } else {
        const data = await response.json();
        setOpenedCategoryItem(data.item);
        showToast(manualState === 'done' ? 'Marked as done' : 'Moved back to active', 'success');
      }
      loadCategoryItems();
    } catch (error) {
      console.error('Error updating smart category item:', error);
      showToast('Failed to update this item. Please try again.', 'error');
    }
  };

  // Opens the dedicated detail view for one tracked smart-category item (its full fields + every
  // contributing email, oldest-first).
  const openCategoryItemDetail = async (itemId: string) => {
    setCategoryItemLoading(true);
    try {
      const session = await fetchAuthSession();
      const token = session.tokens?.idToken?.toString();
      if (!token) throw new Error('No auth token available');

      const url = `${import.meta.env.VITE_API_BASE_URL}/smart-category?itemId=${encodeURIComponent(itemId)}`;
      const response = await fetch(url, { headers: { Authorization: `Bearer ${token}` } });
      if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
      const data = await response.json();
      setOpenedCategoryItem(data.item);
      setCategoryItemEmails(data.emails || []);
    } catch (error) {
      console.error('Error loading smart category item:', error);
      showToast('Failed to load this item. Please try again.', 'error');
    } finally {
      setCategoryItemLoading(false);
    }
  };

  const goBackToCategoryItems = () => {
    setOpenedCategoryItem(null);
    setCategoryItemEmails([]);
  };

  // Sync emails function
  const fetchFromBackend = async () => {
    setLoading(true);
    try {
      const session = await fetchAuthSession();
      const token = session.tokens?.idToken?.toString();
      if (!token) return;

      const apiUrl = `${import.meta.env.VITE_API_BASE_URL}/sync`;
      const response = await fetch(apiUrl, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${token}`,
          'Content-Type': 'application/json'
        }
      });
      if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
      const data = await response.json();

      showToast(data.message, 'success');
      if (data.emails) setEmails(sortEmailsByDate(data.emails));
    } catch (error) {
      console.error('Error fetching data from backend:', error);
      showToast('Error pulling data from backend', 'error');
    } finally {
      setLoading(false);
    }
  };

  const addressFromEmail = (email: Email): string => {
    if (email.fromAddress) return email.fromAddress;
    const match = email.from?.match(/<([^>]+)>/) || email.from?.match(/[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}/);
    return match?.[1] || match?.[0] || '';
  };

  const openCompose = () => {
    setComposeSeed({ mode: 'new' });
    setComposeKey(value => value + 1);
    setActiveTab('compose');
  };

  const openComposeFromEmail = (mode: 'reply' | 'replyAll' | 'forward', email: Email, generatedBody = '') => {
    const sender = addressFromEmail(email);
    const ownAddresses = new Set(connectedAccounts.map(account => account.email.toLowerCase()));
    const originalRecipients = [...(email.to || []), ...(email.cc || [])]
      .filter(address => !ownAddresses.has(address.toLowerCase()));
    const to = mode === 'forward' ? [] : [sender].filter(Boolean);
    const cc = mode === 'replyAll'
      ? [...new Set(originalRecipients.filter(address => address.toLowerCase() !== sender.toLowerCase()))]
      : [];
    const prefix = mode === 'forward' ? 'Fwd:' : 'Re:';
    const prefixedSubject = email.subject.toLowerCase().startsWith(prefix.toLowerCase())
      ? email.subject
      : `${prefix} ${email.subject}`;
    const loadedBody = email.emailId ? emailBodies[email.emailId]?.text : null;
    const originalText = loadedBody || email.bodyText || email.content || '';
    const quote = originalText
      ? `\n\nOn ${email.receivedAt ? new Date(email.receivedAt).toLocaleString() : 'an earlier message'}, ${email.from || sender} wrote:\n${originalText.split('\n').map(line => `> ${line}`).join('\n')}`
      : '';

    setComposeSeed({
      mode,
      senderEmail: email.providerEmail,
      provider: email.provider,
      to,
      cc,
      subject: prefixedSubject,
      body: `${generatedBody}${quote}`,
      threadId: email.threadId,
      inReplyTo: email.messageId || email.inReplyTo,
      originalMessageId: email.emailId?.split('#', 3)[2],
      draftContext: { subject: email.subject, summary: email.summary, content: email.content },
    });
    setComposeKey(value => value + 1);
    setActiveTab('compose');
  };

  const composeContacts = [...new Set(emails.flatMap(email => [
    addressFromEmail(email),
    ...(email.to || []),
    ...(email.cc || []),
  ]).filter(address => address && !connectedAccounts.some(account => account.email.toLowerCase() === address.toLowerCase())))]
    .sort((left, right) => left.localeCompare(right));

  //The UI / JSX
  const renderApp = ({ signOut, user }: { signOut?: (data?: any) => void; user?: any }) => (
          <div className="app-layout" data-theme={theme}>

          {/* Hamburger toggle — floats in place once the sidebar is collapsed */}
          {!sidebarOpen && (
            <button
              onClick={() => setSidebarOpen(true)}
              className="sidebar-hamburger-float"
              aria-label="Open menu"
              title="Open menu"
            >
              <PanelLeftOpen size={18} strokeWidth={2} />
            </button>
          )}

          {/* Sidebar backdrop — only visible as an overlay-dimmer on narrow (mobile) layouts */}
          {sidebarOpen && (
            <div className="sidebar-backdrop" onClick={() => setSidebarOpen(false)} />
          )}

          {/* Sidebar */}
          <div className={`sidebar ${sidebarOpen ? '' : 'sidebar-collapsed'}`}>
            <div className="sidebar-header">
              <h2 className="sidebar-logo">
                <img src="/maily-logo.png" alt="Maily" className="sidebar-logo-img" /><span className="logo-text">Maily</span>
                <button
                  onClick={() => setSidebarOpen(false)}
                  className="sidebar-hamburger"
                  aria-label="Close menu"
                  title="Close menu"
                >
                  <PanelLeftClose size={17} strokeWidth={2} />
                </button>
              </h2>
              <p className="sidebar-subtitle">Smart Email Assistant</p>
            </div>

            {/* On narrow layouts the sidebar is an overlay, so picking a destination should close it too */}
            <div className="sidebar-nav" onClick={() => { if (window.innerWidth < 860) setSidebarOpen(false); }}>
              <div onClick={() => setActiveTab('dashboard')} className={`nav-item ${activeTab === 'dashboard' ? 'active' : ''}`}>
                <span className="nav-item-icon"><LayoutDashboard size={17} strokeWidth={2} /></span>Dashboard
              </div>
              <div onClick={() => setActiveTab('inbox')} className={`nav-item ${activeTab === 'inbox' ? 'active' : ''}`}>
                <span className="nav-item-icon"><InboxIcon size={17} strokeWidth={2} /></span>Inbox
              </div>
              <div onClick={() => setActiveTab('sent')} className={`nav-item ${activeTab === 'sent' ? 'active' : ''}`}>
                <span className="nav-item-icon"><Send size={17} strokeWidth={2} /></span>Sent
              </div>
              <div onClick={openCompose} className={`nav-item compose-nav-item ${activeTab === 'compose' ? 'active' : ''}`}>
                <span className="nav-item-icon"><SquarePen size={17} strokeWidth={2} /></span>Compose
              </div>
              <div onClick={() => { setActiveTab('drafting'); setSelectedEmailIndex(null); setDraft(''); }} className={`nav-item ${activeTab === 'drafting' ? 'active' : ''}`}>
                <span className="nav-item-icon"><Sparkles size={17} strokeWidth={2} /></span>Smart Drafting
              </div>
              <div onClick={() => { setActiveTab('categories'); setOpenedCategoryItem(null); setOpenedCategoryType(null); if (categoryItems.length === 0) loadCategoryItems(); }} className={`nav-item ${activeTab === 'categories' ? 'active' : ''}`}>
                <span className="nav-item-icon"><Truck size={17} strokeWidth={2} /></span>Smart Categories
              </div>
              <div onClick={() => { setActiveTab('stats'); if (!stats) fetchStats(); }} className={`nav-item ${activeTab === 'stats' ? 'active' : ''}`}>
                <span className="nav-item-icon"><BarChart3 size={17} strokeWidth={2} /></span>Statistics
              </div>
              <div onClick={() => setActiveTab('settings')} className={`nav-item ${activeTab === 'settings' ? 'active' : ''}`}>
                <span className="nav-item-icon"><Settings size={17} strokeWidth={2} /></span>Settings
              </div>
            </div>

            <div className="sidebar-footer">
              <div className="sidebar-profile">
                <div className="sidebar-avatar">{(user?.signInDetails?.loginId || user?.username || '?').charAt(0).toUpperCase()}</div>
                <div className="sidebar-profile-info">
                  <span className="sidebar-profile-label">Signed in as</span>
                  <strong className="sidebar-profile-email" title={user?.signInDetails?.loginId || user?.username}>{user?.signInDetails?.loginId || user?.username}</strong>
                </div>
              </div>
              <div className="sidebar-footer-actions">
                <button onClick={signOut} className="btn-logout">
                  <LogOut size={14} strokeWidth={2} />Log Out
                </button>
                <button
                  onClick={toggleDarkMode}
                  className="btn-theme-toggle"
                  aria-label={theme === 'midnight' ? 'Switch to light mode' : 'Switch to dark mode'}
                  title={theme === 'midnight' ? 'Switch to light mode' : 'Switch to dark mode'}
                >
                  {theme === 'midnight' ? <Sun size={16} strokeWidth={2} /> : <Moon size={16} strokeWidth={2} />}
                </button>
              </div>
            </div>
          </div>

          {/* Main content */}
          <div className={`main-content ${sidebarOpen ? '' : 'main-content-expanded'}`}>

            {/* Dashboard Tab */}
            {activeTab === 'dashboard' && (() => {
              // Same rule as the Inbox: mail the user sent doesn't belong in the received-mail
              // digest/stats. Emails missing 'direction' predate this field and default to 'received'.
              const dashboardEmails = emails.filter(e => (e.direction || 'received') !== 'sent');
              const totalCount = dashboardEmails.length;
              const unreadCount = dashboardEmails.filter(e => e.status === 'unread').length;
              const readCount = totalCount - unreadCount;
              return (
                <>
                  <header className="tab-header">
                    <h1>Dashboard</h1>
                    <button onClick={fetchFromBackend} disabled={loading} className="btn-sync">
                      <RefreshCw size={14} strokeWidth={2.25} className={loading ? 'btn-spin-icon' : ''} />
                      {loading ? 'Syncing…' : 'Sync'}
                    </button>
                  </header>

                  <div className="tab-body">
                    {/* First-run prompt: connect a mailbox immediately instead of making the user
                        find Settings on their own. */}
                    {accountsLoaded && connectedAccounts.length === 0 && (
                      <div className="email-card" style={{ marginBottom: '1.5rem', textAlign: 'center', padding: '2rem 1.5rem' }}>
                        <h3 style={{ marginTop: 0 }}>👋 Connect your email to get started</h3>
                        <p style={{ color: 'var(--text-secondary)', marginBottom: '1.25rem' }}>
                          Maily needs access to a mailbox before it can sync, summarize, or draft anything for you.
                        </p>
                        <div style={{ display: 'flex', gap: '0.75rem', justifyContent: 'center', flexWrap: 'wrap' }}>
                          <button onClick={() => loginWithGoogle()} className="btn-connect">✉️ Connect Gmail</button>
                          <button onClick={() => connectOutlook()} className="btn-connect">📨 Connect Outlook</button>
                        </div>
                      </div>
                    )}

                    {/* Bento stat row */}
                    {totalCount > 0 && (
                      <div className="stats-cards stats-cards-compact">
                        <div className="stat-card stat-card-wide">
                          <div className="stat-number">{totalCount}</div>
                          <div className="stat-label">Total Emails</div>
                          <div className="stat-proportion-bar">
                            <div className="stat-proportion-unread" style={{ width: `${(unreadCount / totalCount) * 100}%` }} />
                            <div className="stat-proportion-read" style={{ width: `${(readCount / totalCount) * 100}%` }} />
                          </div>
                          <div className="stat-proportion-legend">
                            <span><i className="stat-dot stat-dot-unread" />{unreadCount} unread</span>
                            <span><i className="stat-dot stat-dot-read" />{readCount} read</span>
                          </div>
                        </div>
                        <div className="stat-card">
                          <div className="stat-number">{unreadCount}</div>
                          <div className="stat-label">Unread</div>
                        </div>
                        <div className="stat-card">
                          <div className="stat-number">{readCount}</div>
                          <div className="stat-label">Read</div>
                        </div>
                      </div>
                    )}

                    {/* AI digest hero */}
                    <div className="email-card">
                      <div className="email-card-header">
                        <h3>🧠 Today's Summary</h3>
                      </div>

                      {loading ? (
                        <div className="email-list">
                          {[1, 2, 3].map(i => (
                            <div key={i} className="skeleton-email-item">
                              <div style={{ display: 'flex', justifyContent: 'space-between', gap: '12px' }}>
                                <div className="skeleton skeleton-row medium" />
                                <div className="skeleton skeleton-badge" />
                              </div>
                              <div className="skeleton skeleton-row full" />
                            </div>
                          ))}
                        </div>
                      ) : totalCount === 0 ? (
                        <div className="empty-inbox">
                          <div className="empty-inbox-icon">✨</div>
                          <p>No emails synced yet.<br/>Sync your inbox to see your AI-powered digest.</p>
                        </div>
                      ) : (
                        <>
                          <p style={{ padding: '0 0 14px', color: 'var(--text-secondary)', fontSize: '0.88em' }}>
                            <strong>{totalCount}</strong> email{totalCount === 1 ? '' : 's'} in your inbox
                            {unreadCount > 0 && (
                              <> · <span style={{ color: '#d97706', fontWeight: 700 }}>{unreadCount} need{unreadCount === 1 ? 's' : ''} attention</span></>
                            )}
                          </p>
                          <div className="email-list">
                            {dashboardEmails.slice(0, 3).map((email, i) => (
                              <div
                                key={email.emailId ?? i}
                                className="email-item email-item-clickable dashboard-digest-item"
                                onClick={() => { setActiveTab('inbox'); openEmailDetail(email); }}
                              >
                                <div className="dashboard-rank-badge">{i + 1}</div>
                                <div style={{ flex: 1, minWidth: 0 }}>
                                  <div className="email-item-header">
                                    <strong className="email-subject">{email.subject}</strong>
                                    <span className="email-status">STATUS: {email.status ? email.status.toUpperCase() : 'N/A'}</span>
                                  </div>
                                  {email.summary && <p className="email-summary"><strong>Summary:</strong> {email.summary}</p>}
                                </div>
                              </div>
                            ))}
                          </div>
                        </>
                      )}
                    </div>

                    {/* Recent emails */}
                    <div className="email-card" style={{ marginTop: '1.5rem' }}>
                      <div className="email-card-header">
                        <h3>📬 Recent Emails</h3>
                        <button
                          onClick={() => setActiveTab('inbox')}
                          style={{ background: 'none', border: 'none', color: 'var(--accent)', fontWeight: 600, fontSize: '0.85em', cursor: 'pointer', fontFamily: 'inherit' }}
                        >
                          View all →
                        </button>
                      </div>
                      {totalCount === 0 ? (
                        <p style={{ padding: '1rem', color: 'var(--text-muted)' }}>No emails to show.</p>
                      ) : (
                        <div className="email-list">
                          {dashboardEmails.slice(0, 5).map((email, i) => (
                            <div
                              key={email.emailId ?? i}
                              className="email-item email-item-clickable"
                              onClick={() => { setActiveTab('inbox'); openEmailDetail(email); }}
                            >
                              <div className="email-item-header">
                                <strong className="email-subject">{email.subject}</strong>
                                <span className="email-status">STATUS: {email.status ? email.status.toUpperCase() : 'N/A'}</span>
                              </div>
                              {email.summary && <p className="email-summary"><strong>Summary:</strong> {email.summary}</p>}
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  </div>
                </>
              );
            })()}

            {activeTab === 'compose' && (
              <>
                <header className="tab-header">
                  <h1>
                    {composeSeed?.mode === 'replyAll' ? 'Reply all'
                      : composeSeed?.mode === 'forward' ? 'Forward'
                      : composeSeed?.mode === 'reply' ? 'Reply'
                      : 'Compose'}
                  </h1>
                  <button onClick={() => setActiveTab('inbox')} className="btn-sync">Back to Inbox</button>
                </header>
                <div className="tab-body">
                  <Compose
                    key={composeKey}
                    accounts={connectedAccounts}
                    contacts={composeContacts}
                    signature={signature}
                    seed={composeSeed}
                    onCancel={() => setActiveTab('inbox')}
                    onSent={() => {
                      showToast('Email sent.', 'success');
                      setComposeSeed(undefined);
                      setActiveTab('inbox');
                    }}
                  />
                </div>
              </>
            )}

            {/* Inbox Tab */}
            {activeTab === 'inbox' && (
              openedEmail ? (
                <>
                  <header className="tab-header">
                    <button className="btn-back" onClick={goBackToInbox}>← Back to Inbox</button>
                  </header>

                  <div className="tab-body">
                    <div className="email-card">
                      <div className="email-card-header">
                        <h3>{openedEmail.subject}</h3>
                      </div>

                      {openedEmail.threadId && threadLoadingId === openedEmail.threadId ? (
                        <div className="email-detail-thread">
                          <div className="skeleton skeleton-row full" />
                        </div>
                      ) : (() => {
                        const messages = openedEmail.threadId ? (threadEmails[openedEmail.threadId] || [openedEmail]) : [openedEmail];
                        return (
                          <div className="email-detail-thread">
                            {messages.map((msg, index) => {
                              const isExpanded = expandedEmailId === msg.emailId;
                              return (
                                <div key={msg.emailId ?? index} className={`thread-message ${isExpanded ? 'expanded' : ''}`}>
                                  <div className="thread-message-header" onClick={() => toggleEmailBody(msg)}>
                                    <div className="thread-message-from-line">
                                      <strong className="thread-message-from">{msg.from}</strong>
                                      <span className="thread-message-date">
                                        {msg.receivedAt ? new Date(msg.receivedAt).toLocaleString() : ''}
                                      </span>
                                    </div>
                                    {((msg.labels && msg.labels.length > 0) || (msg.providerLabels && msg.providerLabels.length > 0)) && (
                                      <div className="email-label-row">
                                        {(msg.labels || []).map(labelId => {
                                          const def = labels.find(l => l.id === labelId);
                                          return def ? <span key={labelId} className="label-chip" style={{ background: def.color }}>{def.name}</span> : null;
                                        })}
                                        {(msg.providerLabels || []).map(name => (
                                          <span key={name} className="provider-label-badge">{name}</span>
                                        ))}
                                      </div>
                                    )}
                                    {!isExpanded && msg.summary && (
                                      <p className="thread-message-preview">{msg.summary}</p>
                                    )}
                                  </div>

                                  {isExpanded && (
                                    <div className="thread-message-content">
                                      {msg.attachments && msg.attachments.length > 0 && (
                                        <div className="email-attachments">
                                          {msg.attachments.map(att => (
                                            <button
                                              key={att.id}
                                              className="attachment-chip"
                                              disabled={attachmentLoadingId === att.id}
                                              onClick={e => {
                                                e.stopPropagation();
                                                if (msg.emailId) downloadAttachment(msg.emailId, att);
                                              }}
                                            >
                                              📎 {att.filename} ({formatFileSize(att.size)})
                                              {attachmentLoadingId === att.id ? ' ⏳' : ''}
                                            </button>
                                          ))}
                                        </div>
                                      )}

                                      <div className="classify-toolbar" onClick={e => e.stopPropagation()}>
                                        <button
                                          className="btn-classify"
                                          onClick={() => setClassifyMenuEmailId(prev => prev === msg.emailId ? null : (msg.emailId ?? null))}
                                        >
                                          🏷️ Classify
                                        </button>
                                      </div>
                                      {classifyMenuEmailId === msg.emailId && (
                                        <div className="classify-menu" onClick={e => e.stopPropagation()}>
                                          <div className="classify-menu-section">
                                            <span className="classify-menu-title">Labels</span>
                                            <div className="classify-menu-checkboxes">
                                              {labels.map(l => {
                                                const checked = (msg.labels || []).includes(l.id);
                                                return (
                                                  <label key={l.id} className="classify-menu-checkbox">
                                                    <input
                                                      type="checkbox"
                                                      checked={checked}
                                                      disabled={classifySaving}
                                                      onChange={() => classifyEmail(msg, checked ? { removeLabels: [l.id] } : { addLabels: [l.id] })}
                                                    />
                                                    <span className="label-chip" style={{ background: l.color }}>{l.name}</span>
                                                  </label>
                                                );
                                              })}
                                            </div>
                                          </div>
                                          <div className="classify-menu-section">
                                            <span className="classify-menu-title">Smart category</span>
                                            <select
                                              className="classify-menu-select"
                                              disabled={classifySaving}
                                              defaultValue=""
                                              onChange={e => { const v = e.target.value; classifyEmail(msg, { categoryType: v === '' ? null : v }); e.target.value = ''; }}
                                            >
                                              <option value="" disabled={!msg.categoryItemId}>{msg.categoryItemId ? '— Clear category —' : '— Assign to category —'}</option>
                                              {Object.values(categoryTypeCatalog).map(ct => (
                                                <option key={ct.id} value={ct.id}>{ct.icon} {ct.label}</option>
                                              ))}
                                            </select>
                                          </div>
                                        </div>
                                      )}

                                      {bodyLoadingId === msg.emailId ? (
                                        <div className="skeleton skeleton-row full" />
                                      ) : (() => {
                                        const bodyData = msg.emailId ? emailBodies[msg.emailId] : undefined;
                                        if (bodyData?.html) {
                                          return (
                                            <iframe
                                              title={`email-body-${msg.emailId}`}
                                              sandbox=""
                                              referrerPolicy="no-referrer"
                                              srcDoc={bodyData.html}
                                              className="email-body-frame"
                                            />
                                          );
                                        }
                                        if (bodyData?.text) {
                                          return <p className="email-body-text">{bodyData.text}</p>;
                                        }
                                        return bodyData ? <p className="email-body-text">(This email has no readable body content.)</p> : null;
                                      })()}
                                      <div className="message-actions">
                                        <button onClick={() => openComposeFromEmail('reply', msg)}>Reply</button>
                                        <button onClick={() => openComposeFromEmail('replyAll', msg)}>Reply all</button>
                                        <button onClick={() => openComposeFromEmail('forward', msg)}>Forward</button>
                                      </div>
                                    </div>
                                  )}
                                </div>
                              );
                            })}
                          </div>
                        );
                      })()}
                    </div>
                  </div>
                </>
              ) : (
                <>
                  <header className="tab-header">
                    <h1>Overview</h1>
                    <button onClick={fetchFromBackend} disabled={loading} className="btn-sync">
                      <RefreshCw size={14} strokeWidth={2.25} className={loading ? 'btn-spin-icon' : ''} />
                      {loading ? 'Syncing…' : 'Sync'}
                    </button>
                  </header>

                  {/* Account filter tabs — only visible when 2+ accounts are connected */}
                  {connectedAccounts.length > 1 && (
                    <div className="account-filter-bar">
                      <button
                        className={`filter-tab ${accountFilter === 'all' ? 'active' : ''}`}
                        onClick={() => setAccountFilter('all')}
                      >
                        All Accounts
                      </button>
                      {connectedAccounts.map(a => (
                        <button
                          key={a.email}
                          className={`filter-tab ${accountFilter === a.email ? 'active' : ''}`}
                          onClick={() => setAccountFilter(a.email)}
                        >
                          {a.email}
                        </button>
                      ))}
                    </div>
                  )}

                  {/* Label filter tabs — only visible once at least one label exists */}
                  {labels.length > 0 && (
                    <div className="account-filter-bar">
                      <button
                        className={`filter-tab ${labelFilter === 'all' ? 'active' : ''}`}
                        onClick={() => setLabelFilter('all')}
                      >
                        All Labels
                      </button>
                      {labels.map(l => (
                        <button
                          key={l.id}
                          className={`filter-tab ${labelFilter === l.id ? 'active' : ''}`}
                          onClick={() => setLabelFilter(l.id)}
                        >
                          {l.name}
                        </button>
                      ))}
                    </div>
                  )}

                  {/* Search — subject, sender, or content */}
                  {emails.length > 0 && (
                    <div className="inbox-search-bar">
                      <div className={`inbox-search-input-wrap ${inboxSearch ? 'has-value' : ''}`}>
                        <Sparkles
                          size={16}
                          strokeWidth={2.25}
                          className={`inbox-search-icon ${semanticSearchLoading ? 'inbox-search-icon-thinking' : ''}`}
                        />
                        <input
                          type="text"
                          value={inboxSearch}
                          onChange={e => setInboxSearch(e.target.value)}
                          placeholder="Ask Maily to find an email — try “that flight confirmation”"
                          className="inbox-search-input"
                        />
                        {inboxSearch && (
                          <button
                            className="inbox-search-clear"
                            onClick={() => setInboxSearch('')}
                            aria-label="Clear search"
                          >
                            ×
                          </button>
                        )}
                      </div>
                    </div>
                  )}

                  <div className="tab-body">
                    <div className="email-card">
                      <div className="email-card-header">
                        <h3>📬 Latest Email Analysis</h3>
                      </div>

                      {(() => {
                        // Mail the user sent shouldn't show up in the Inbox — it belongs in Sent
                        // (not yet built as its own tab; emails missing 'direction' predate this
                        // field and default to 'received' so nothing existing gets hidden).
                        const receivedEmails = emails.filter(e => (e.direction || 'received') !== 'sent');
                        const byLabel = labelFilter === 'all'
                          ? receivedEmails
                          : receivedEmails.filter(e => (e.labels || []).includes(labelFilter));
                        const trimmedQuery = inboxSearch.trim();
                        const query = trimmedQuery.toLowerCase();
                        // Once the semantic search response for this exact query is in, use its
                        // LLM-ranked order; otherwise (still debouncing, in flight, or it errored)
                        // fall back to an instant plain substring match so results are never empty.
                        const hasSemanticResults = trimmedQuery !== '' && semanticSearchQuery === trimmedQuery;
                        const visibleEmails = !query
                          ? byLabel
                          : hasSemanticResults
                          ? semanticSearchIds
                              .map(id => byLabel.find(e => e.emailId === id))
                              .filter((e): e is Email => !!e)
                          : byLabel.filter(e =>
                              e.subject?.toLowerCase().includes(query) ||
                              e.from?.toLowerCase().includes(query) ||
                              e.providerEmail?.toLowerCase().includes(query) ||
                              e.content?.toLowerCase().includes(query) ||
                              e.summary?.toLowerCase().includes(query)
                            );
                        return loading ? (
                        <div className="email-list">
                          {[1,2,3,4].map(i => (
                            <div key={i} className="skeleton-email-item">
                              <div style={{ display: 'flex', justifyContent: 'space-between', gap: '12px' }}>
                                <div className="skeleton skeleton-row medium" />
                                <div className="skeleton skeleton-badge" />
                              </div>
                              <div className="skeleton skeleton-row long" />
                              <div className="skeleton skeleton-row full" />
                              <div className="skeleton skeleton-row medium" />
                            </div>
                          ))}
                        </div>
                      ) : visibleEmails.length > 0 ? (
                        <div className="email-list">
                          {visibleEmails.map((email, index) => (
                            <div key={index} className="email-item email-item-clickable" onClick={() => openEmailDetail(email)}>
                              <div className="email-item-header">
                                <strong className="email-subject">{email.subject}</strong>
                                <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center' }}>
                                  {email.providerEmail && connectedAccounts.length > 1 && (
                                    <span className="email-account-badge">{providerLabel(email.provider)}: {email.providerEmail}</span>
                                  )}
                                  <span className="email-status">
                                    STATUS: {email.status ? email.status.toUpperCase() : 'N/A'}
                                  </span>
                                </div>
                              </div>
                              {((email.labels && email.labels.length > 0) || (email.providerLabels && email.providerLabels.length > 0)) && (
                                <div className="email-label-row">
                                  {(email.labels || []).map(labelId => {
                                    const def = labels.find(l => l.id === labelId);
                                    return def ? <span key={labelId} className="label-chip" style={{ background: def.color }}>{def.name}</span> : null;
                                  })}
                                  {(email.providerLabels || []).map(name => (
                                    <span key={name} className="provider-label-badge">{name}</span>
                                  ))}
                                </div>
                              )}
                              {email.summary && (
                                <p className="email-summary"><strong>Summary:</strong> {email.summary}</p>
                              )}
                              <p className="email-content">{email.content}</p>

                              {email.attachments && email.attachments.length > 0 && (
                                <div className="email-attachments">
                                  {email.attachments.map(att => (
                                    <button
                                      key={att.id}
                                      className="attachment-chip"
                                      disabled={attachmentLoadingId === att.id}
                                      onClick={e => {
                                        e.stopPropagation();
                                        if (email.emailId) downloadAttachment(email.emailId, att);
                                      }}
                                    >
                                      📎 {att.filename} ({formatFileSize(att.size)})
                                      {attachmentLoadingId === att.id ? ' ⏳' : ''}
                                    </button>
                                  ))}
                                </div>
                              )}
                            </div>
                          ))}
                        </div>
                      ) : query ? (
                        <div className="empty-inbox">
                          <div className="empty-inbox-icon">🔎</div>
                          <p>No matches for "{inboxSearch.trim()}".<br/>Try a different subject or sender.</p>
                        </div>
                      ) : (
                        <div className="empty-inbox">
                          <div className="empty-inbox-icon">📭</div>
                          <p>No emails found in the database.<br/>Click Sync to fetch them!</p>
                        </div>
                      );
                      })()}
                    </div>
                  </div>
                </>
              )
            )}

            {/* Sent Tab — mirrors the Inbox list (newest first, filterable by search), filtered to
                mail the user sent rather than received. Opening one reuses the Inbox detail view
                (same as Dashboard/Smart Categories do) rather than duplicating that whole thread UI
                here — "Back to Inbox" on that view is a fair description of where it returns to. */}
            {activeTab === 'sent' && (() => {
              const sentEmails = emails.filter(e => e.direction === 'sent');
              const query = sentSearch.trim().toLowerCase();
              const visibleSentEmails = query
                ? sentEmails.filter(e =>
                    e.subject?.toLowerCase().includes(query) ||
                    (e.to || []).some(addr => addr.toLowerCase().includes(query)) ||
                    e.content?.toLowerCase().includes(query) ||
                    e.summary?.toLowerCase().includes(query)
                  )
                : sentEmails;
              return (
                <>
                  <header className="tab-header">
                    <h1>Sent</h1>
                  </header>

                  {/* Search — subject, recipient, or content */}
                  {sentEmails.length > 0 && (
                    <div className="inbox-search-bar">
                      <div className="inbox-search-input-wrap">
                        <span className="inbox-search-icon">🔎</span>
                        <input
                          type="text"
                          value={sentSearch}
                          onChange={e => setSentSearch(e.target.value)}
                          placeholder="Search by subject, recipient, or content"
                          className="inbox-search-input"
                        />
                        {sentSearch && (
                          <button
                            className="inbox-search-clear"
                            onClick={() => setSentSearch('')}
                            aria-label="Clear search"
                          >
                            ×
                          </button>
                        )}
                      </div>
                    </div>
                  )}

                  <div className="tab-body">
                    <div className="email-card">
                      <div className="email-card-header">
                        <h3>📤 Sent Mail</h3>
                      </div>

                      {loading ? (
                        <div className="email-list">
                          {[1,2,3,4].map(i => (
                            <div key={i} className="skeleton-email-item">
                              <div style={{ display: 'flex', justifyContent: 'space-between', gap: '12px' }}>
                                <div className="skeleton skeleton-row medium" />
                                <div className="skeleton skeleton-badge" />
                              </div>
                              <div className="skeleton skeleton-row long" />
                              <div className="skeleton skeleton-row full" />
                            </div>
                          ))}
                        </div>
                      ) : visibleSentEmails.length > 0 ? (
                        <div className="email-list">
                          {visibleSentEmails.map((email, index) => (
                            <div
                              key={index}
                              className="email-item email-item-clickable"
                              onClick={() => { setActiveTab('inbox'); openEmailDetail(email); }}
                            >
                              <div className="email-item-header">
                                <strong className="email-subject">{email.subject}</strong>
                                {email.providerEmail && connectedAccounts.length > 1 && (
                                  <span className="email-account-badge">{providerLabel(email.provider)}: {email.providerEmail}</span>
                                )}
                              </div>
                              <p className="email-summary"><strong>To:</strong> {(email.to && email.to.length > 0) ? email.to.join(', ') : '(no recipients)'}</p>
                              {email.summary && (
                                <p className="email-summary"><strong>Summary:</strong> {email.summary}</p>
                              )}
                              <p className="email-content">{email.content}</p>

                              {email.attachments && email.attachments.length > 0 && (
                                <div className="email-attachments">
                                  {email.attachments.map(att => (
                                    <button
                                      key={att.id}
                                      className="attachment-chip"
                                      disabled={attachmentLoadingId === att.id}
                                      onClick={e => {
                                        e.stopPropagation();
                                        if (email.emailId) downloadAttachment(email.emailId, att);
                                      }}
                                    >
                                      📎 {att.filename} ({formatFileSize(att.size)})
                                      {attachmentLoadingId === att.id ? ' ⏳' : ''}
                                    </button>
                                  ))}
                                </div>
                              )}
                            </div>
                          ))}
                        </div>
                      ) : query ? (
                        <div className="empty-inbox">
                          <div className="empty-inbox-icon">🔎</div>
                          <p>No matches for "{sentSearch.trim()}".<br/>Try a different subject or recipient.</p>
                        </div>
                      ) : (
                        <div className="empty-inbox">
                          <div className="empty-inbox-icon">📤</div>
                          <p>No sent mail found.<br/>Anything you send from Maily will show up here.</p>
                        </div>
                      )}
                    </div>
                  </div>
                </>
              );
            })()}

            {/* Smart Drafting Tab */}
            {activeTab === 'drafting' && (
              <>
                <header className="tab-header">
                  <h1>Smart Drafting</h1>
                </header>

                <div className="tab-body">
                  {emails.length === 0 ? (
                    <div className="empty-inbox">
                      <div className="empty-inbox-icon">✨</div>
                      <p>No emails loaded yet.<br/>Go to Inbox and click Sync first!</p>
                    </div>
                  ) : (
                    <div className="drafting-layout">
                      {/* Left panel: email picker */}
                      <div className="drafting-email-list">
                        <h3 className="drafting-panel-title">Select an email</h3>
                        {emails.map((email, index) => (
                          <div
                            key={index}
                            className={`drafting-email-item ${selectedEmailIndex === index ? 'selected' : ''}`}
                            onClick={() => { setSelectedEmailIndex(index); setDraft(''); }}
                          >
                            <strong className="email-subject">{email.subject}</strong>
                            <span className="drafting-email-snippet">{email.content?.slice(0, 80)}…</span>
                          </div>
                        ))}
                      </div>

                      {/* Right panel: draft area */}
                      <div className="drafting-panel">
                        {selectedEmailIndex === null ? (
                          <div className="drafting-placeholder">
                            <p>👈 Pick an email on the left to generate a reply draft.</p>
                          </div>
                        ) : (
                          <>
                            <div className="drafting-email-preview">
                              <h3>{emails[selectedEmailIndex].subject}</h3>
                              {emails[selectedEmailIndex].summary && (
                                <p className="drafting-summary"><strong>AI Summary:</strong> {emails[selectedEmailIndex].summary}</p>
                              )}
                              <p className="drafting-snippet">{emails[selectedEmailIndex].content}</p>
                            </div>

                            <div className="draft-tone-row">
                              <div className="draft-tone-picker">
                                {DRAFT_TONES.map(t => (
                                  <button
                                    key={t.id}
                                    className={`draft-tone-option ${draftTone === t.id ? 'active' : ''}`}
                                    onClick={() => setDraftTone(t.id)}
                                    disabled={draftLoading}
                                  >
                                    {t.label}
                                  </button>
                                ))}
                              </div>
                              <button
                                onClick={() => fetchDraft(emails[selectedEmailIndex!], draftTone)}
                                disabled={draftLoading}
                                className="btn-sync draft-generate-btn"
                              >
                                {draftLoading ? '⏳ Generating...' : '✨ Generate Draft Reply'}
                              </button>
                            </div>

                            {draftLoading && (
                              <div className="skeleton-draft">
                                <div className="skeleton skeleton-row short" style={{ height: '16px' }} />
                                <div className="skeleton skeleton-row full" />
                                <div className="skeleton skeleton-row full" />
                                <div className="skeleton skeleton-row long" />
                                <div className="skeleton skeleton-row medium" />
                                <div className="skeleton skeleton-row full" />
                              </div>
                            )}
                            {!draftLoading && draft && (
                              <div className="draft-output">
                                <div className="draft-output-header">
                                  <h4>📝 Suggested Reply</h4>
                                  <button
                                    className="btn-copy"
                                    onClick={() => navigator.clipboard.writeText(draft)}
                                  >
                                    Copy
                                  </button>
                                </div>
                                <p className="draft-text">{draft}</p>
                              </div>
                            )}
                          </>
                        )}
                      </div>
                    </div>
                  )}
                </div>
              </>
            )}

            {/* Smart Categories Tab */}
            {activeTab === 'categories' && (
              categoryWizard ? (
                <CategoryWizard
                  mode={categoryWizard.mode}
                  existingCategoryType={categoryWizard.existing}
                  emails={emails}
                  apiBaseUrl={import.meta.env.VITE_API_BASE_URL}
                  getAuthToken={async () => (await fetchAuthSession()).tokens?.idToken?.toString() ?? null}
                  onClose={() => { setCategoryWizard(null); setCategoryReferenceIds([]); }}
                  onSaved={() => { setCategoryWizard(null); setCategoryReferenceIds([]); loadCategoryTypeCatalog(); loadCategoryItems(); showToast('Category saved', 'success'); }}
                  selectedReferenceIds={categoryReferenceIds}
                  onOpenEmailPicker={() => { setEmailPickerSnapshot(categoryReferenceIds); setEmailPickerOpen(true); }}
                  onRemoveReferenceId={id => setCategoryReferenceIds(prev => prev.filter(existingId => existingId !== id))}
                />
              ) : openedCategoryItem ? (
                <>
                  <header className="tab-header">
                    <button className="btn-back" onClick={goBackToCategoryItems}>← Back to Smart Categories</button>
                  </header>

                  <div className="tab-body">
                    {categoryItemLoading ? (
                      <div className="email-card"><div className="skeleton skeleton-row full" /></div>
                    ) : (() => {
                      const meta = categoryTypeMeta(openedCategoryItem.categoryType, categoryTypeCatalog);
                      return (
                      <>
                        <div className="email-card">
                          <div className="email-card-header">
                            <h3>{meta.icon} {categoryItemTitle(meta, openedCategoryItem.fields)}</h3>
                            <CategoryItemStatusBadge item={openedCategoryItem} />
                          </div>
                          <div className="category-item-fields">
                            {Object.entries(openedCategoryItem.fields).filter(([, value]) => value).map(([key, value]) => {
                              const fieldDef = meta.fields.find(f => f.key === key);
                              return (
                                <div key={key} className="category-item-field">
                                  <span className="category-item-field-label">{fieldDef?.label ?? key}</span>
                                  <span className="category-item-field-value">
                                    {fieldDef ? <CategoryFieldValue fieldDef={fieldDef} value={value as string} /> : value}
                                  </span>
                                </div>
                              );
                            })}
                          </div>
                          <div className="category-item-actions">
                            {openedCategoryItem.effectiveState === 'active' && (
                              <button className="btn-connect" onClick={() => updateCategoryItemState(openedCategoryItem.itemId, 'done')}>
                                ✅ Mark as done
                              </button>
                            )}
                            {openedCategoryItem.effectiveState === 'done' && (
                              <button className="btn-disconnect" onClick={() => updateCategoryItemState(openedCategoryItem.itemId, null)}>
                                ↩️ Restore to active
                              </button>
                            )}
                            <button
                              className="btn-disconnect"
                              onClick={() => {
                                if (window.confirm('Delete this card? This cannot be undone from the UI.')) {
                                  updateCategoryItemState(openedCategoryItem.itemId, 'trashed');
                                }
                              }}
                            >
                              🗑️ Delete
                            </button>
                          </div>
                        </div>

                        <div className="email-card" style={{ marginTop: '1.5rem' }}>
                          <div className="email-card-header">
                            <h3>📧 Related Emails</h3>
                          </div>
                          <div className="email-list">
                            {categoryItemEmails.map((email, index) => (
                              <div
                                key={email.emailId ?? index}
                                className="email-item email-item-clickable"
                                onClick={() => openEmailFromCategoryItem(email)}
                              >
                                <div className="email-item-header">
                                  <strong className="email-subject">{email.subject}</strong>
                                  <span className="email-status">
                                    {email.receivedAt ? new Date(email.receivedAt).toLocaleString() : ''}
                                  </span>
                                </div>
                                {email.summary && <p className="email-summary">{email.summary}</p>}
                              </div>
                            ))}
                          </div>
                        </div>
                      </>
                      );
                    })()}
                  </div>
                </>
              ) : openedCategoryType === TRAVEL_CATEGORY_TYPE_ID ? (
                <TravelTripsView
                  meta={categoryTypeMeta(TRAVEL_CATEGORY_TYPE_ID, categoryTypeCatalog)}
                  items={categoryItems.filter(i => i.categoryType === TRAVEL_CATEGORY_TYPE_ID)}
                  apiBaseUrl={import.meta.env.VITE_API_BASE_URL}
                  getAuthToken={async () => (await fetchAuthSession()).tokens?.idToken?.toString() ?? null}
                  onOpenItem={openCategoryItemDetail}
                  onBack={() => setOpenedCategoryType(null)}
                />
              ) : openedCategoryType ? (() => {
                const meta = categoryTypeMeta(openedCategoryType, categoryTypeCatalog);
                const items = categoryItems.filter(i => i.categoryType === openedCategoryType);
                const active = [...items.filter(i => i.effectiveState === 'active')].sort((a, b) =>
                  (a.fields[meta.primaryDateField] || '').localeCompare(b.fields[meta.primaryDateField] || '')
                );
                const done = items.filter(i => i.effectiveState === 'done');
                return (
                  <>
                    <header className="tab-header">
                      <button className="btn-back" onClick={() => setOpenedCategoryType(null)}>← Back to Smart Categories</button>
                      <h1>{meta.icon} {meta.label}</h1>
                    </header>
                    <div className="tab-body">
                      <div className="category-item-grid">
                        {active.map(item => (
                          <CategoryItemCard key={item.itemId} item={item} meta={meta} onClick={() => openCategoryItemDetail(item.itemId)} />
                        ))}
                      </div>
                      {active.length === 0 && done.length === 0 && (
                        <div className="empty-inbox">
                          <div className="empty-inbox-icon">{meta.icon}</div>
                          <p>No tracked cards in this category yet.</p>
                        </div>
                      )}
                      {done.length > 0 && (
                        <details className="category-completed-section">
                          <summary>Completed ({done.length})</summary>
                          <div className="category-item-grid">
                            {done.map(item => (
                              <CategoryItemCard key={item.itemId} item={item} meta={meta} onClick={() => openCategoryItemDetail(item.itemId)} />
                            ))}
                          </div>
                        </details>
                      )}
                    </div>
                  </>
                );
              })() : (
                <>
                  <header className="tab-header">
                    <h1>Smart Categories</h1>
                    <div style={{ display: 'flex', gap: '0.75rem' }}>
                      <button onClick={() => setCategoryWizard({ mode: 'create' })} className="btn-connect">
                        + New Category
                      </button>
                      <button onClick={loadCategoryItems} disabled={categoryItemsLoading} className="btn-sync">
                        {categoryItemsLoading ? 'Loading...' : '🔄 Refresh'}
                      </button>
                    </div>
                  </header>

                  <div className="tab-body">
                    {categoryItemsLoading ? (
                      <div className="category-item-grid">
                        {[1, 2, 3].map(i => (
                          <div key={i} className="skeleton-email-item">
                            <div className="skeleton skeleton-row medium" />
                            <div className="skeleton skeleton-row full" />
                          </div>
                        ))}
                      </div>
                    ) : (() => {
                      // One row per category type that EXISTS (built-in or custom) — not just ones that
                      // already have a card. A brand new category (built-in or just-created) needs to be
                      // visible and enterable even at zero cards, otherwise there's no way to discover it
                      // or watch it fill in. Each row: a short preview (active cards first, sorted the
                      // same way the drill-in view sorts them, done cards filling remaining slots) with a
                      // way to see everything.
                      const categoryTypeIds = Object.keys(categoryTypeCatalog);
                      const PREVIEW_COUNT = 5;
                      if (categoryTypeIds.length === 0) {
                        return (
                          <div className="empty-inbox">
                            <div className="empty-inbox-icon">🚚</div>
                            <p>No categories yet.<br/>Create your own with "+ New Category".</p>
                          </div>
                        );
                      }
                      return (
                        <div className="category-type-rows">
                          {categoryTypeIds.map(categoryTypeId => {
                            const meta = categoryTypeMeta(categoryTypeId, categoryTypeCatalog);
                            const items = categoryItems.filter(i => i.categoryType === categoryTypeId);
                            const active = [...items.filter(i => i.effectiveState === 'active')].sort((a, b) =>
                              (a.fields[meta.primaryDateField] || '').localeCompare(b.fields[meta.primaryDateField] || '')
                            );
                            const done = items.filter(i => i.effectiveState === 'done');
                            const preview = [...active, ...done].slice(0, PREVIEW_COUNT);
                            return (
                              <div key={categoryTypeId} className="category-type-row">
                                <div className="category-type-row-header">
                                  <h3>{meta.icon} {meta.label}</h3>
                                  <span className="category-type-row-count">{items.length} card{items.length === 1 ? '' : 's'}</span>
                                  <button className="btn-disconnect" onClick={() => setOpenedCategoryType(categoryTypeId)}>View all →</button>
                                </div>
                                <div className="category-type-row-preview">
                                  {preview.length > 0
                                    ? preview.map(item => (
                                        <CategoryItemCard key={item.itemId} item={item} meta={meta} onClick={() => openCategoryItemDetail(item.itemId)} />
                                      ))
                                    : <p className="wizard-stage-help">No cards yet — they'll show up automatically once Maily detects a matching email.</p>}
                                </div>
                              </div>
                            );
                          })}
                        </div>
                      );
                    })()}
                  </div>
                </>
              )
            )}

            {/* Statistics Tab */}
            {activeTab === 'stats' && (
              <>
                <header className="tab-header">
                  <h1>Statistics</h1>
                  <div style={{ display: 'flex', gap: '0.75rem' }}>
                    <button onClick={fetchStats} disabled={statsLoading} className="btn-sync">
                      {statsLoading ? 'Loading...' : '🔄 Refresh'}
                    </button>
                    <button onClick={fetchExport} disabled={exportLoading} className="btn-sync">
                      {exportLoading ? '⏳ Exporting...' : '⬇️ Export Summaries'}
                    </button>
                  </div>
                </header>

                <div className="tab-body">
                  {exportUrl && (
                    <div className="email-card" style={{ marginTop: '1rem' }}>
                      <div className="email-card-header">
                        <h3>⬇️ Export Ready</h3>
                      </div>
                      <p style={{ padding: '0.75rem 1rem' }}>
                        Your summaries have been saved to S3.{' '}
                        <a href={exportUrl} download="email-summaries.json" style={{ color: 'var(--accent)' }}>
                          Click here to download
                        </a>
                        {' '}(link expires in 15 minutes)
                      </p>
                    </div>
                  )}

                  {statsLoading && (
                    <>
                      <div className="stats-cards">
                        {[1,2,3].map(i => (
                          <div key={i} className="skeleton-stat-card">
                            <div className="skeleton skeleton-number" />
                            <div className="skeleton skeleton-label" />
                          </div>
                        ))}
                      </div>
                      <div className="email-card">
                        <div className="email-card-header">
                          <div className="skeleton skeleton-row short" style={{ height: '18px' }} />
                        </div>
                        <div className="email-list">
                          {[1,2,3].map(i => (
                            <div key={i} className="skeleton-email-item">
                              <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                                <div className="skeleton skeleton-row medium" />
                                <div className="skeleton skeleton-badge" />
                              </div>
                            </div>
                          ))}
                        </div>
                      </div>
                    </>
                  )}

                  {!statsLoading && stats && (
                    <>
                      {/* Summary cards */}
                      <div className="stats-cards">
                        <div className="stat-card stat-card-wide">
                          <div className="stat-number">{stats.total}</div>
                          <div className="stat-label">Total Emails</div>
                          {stats.total > 0 && (
                            <>
                              <div className="stat-proportion-bar">
                                <div className="stat-proportion-unread" style={{ width: `${(stats.unread / stats.total) * 100}%` }} />
                                <div className="stat-proportion-read" style={{ width: `${(stats.read / stats.total) * 100}%` }} />
                              </div>
                              <div className="stat-proportion-legend">
                                <span><i className="stat-dot stat-dot-unread" />{stats.unread} unread</span>
                                <span><i className="stat-dot stat-dot-read" />{stats.read} read</span>
                              </div>
                            </>
                          )}
                        </div>
                        <div className="stat-card">
                          <div className="stat-number">{stats.unread}</div>
                          <div className="stat-label">Unread</div>
                        </div>
                        <div className="stat-card">
                          <div className="stat-number">{stats.read}</div>
                          <div className="stat-label">Read</div>
                        </div>
                      </div>

                      {/* Top senders */}
                      <div className="email-card" style={{ marginTop: '1.5rem' }}>
                        <div className="email-card-header">
                          <h3>📬 Top Senders</h3>
                        </div>
                        {stats.top_senders.length > 0 ? (
                          <div className="email-list">
                            {stats.top_senders.map((entry, index) => (
                              <div key={index} className="email-item">
                                <div className="email-item-header">
                                  <strong className="email-subject">{entry.sender}</strong>
                                  <span className="email-status">{entry.count} email{entry.count !== 1 ? 's' : ''}</span>
                                </div>
                              </div>
                            ))}
                          </div>
                        ) : (
                          <p style={{ padding: '1rem', color: '#888' }}>No sender data available.</p>
                        )}
                      </div>
                    </>
                  )}

                  {!statsLoading && !stats && (
                    <div className="empty-inbox">
                      <div className="empty-inbox-icon">📊</div>
                      <p>No statistics yet.<br/>Sync your emails first, then come back here!</p>
                    </div>
                  )}
                </div>
              </>
            )}

            {/* Settings Tab */}
            {activeTab === 'settings' && (
              <>
                <header className="tab-header">
                  <h1>Settings</h1>
                </header>

                <div className="tab-body">
                  {/* Theme picker */}
                  <div className="settings-card" style={{ marginBottom: '20px' }}>
                    <h3>🎨 Appearance</h3>
                    <p>Choose a colour theme for Maily.</p>
                    <div className="theme-picker-grid">
                      {THEMES.map((t) => (
                        <button
                          key={t.id}
                          className={`theme-swatch ${theme === t.id ? 'active' : ''}`}
                          data-swatch={t.id}
                          onClick={() => setTheme(t.id)}
                          aria-label={`Select ${t.label} theme`}
                        >
                          <div className="theme-swatch-circle" />
                          <span className="theme-swatch-label">{t.label}</span>
                        </button>
                      ))}
                    </div>
                  </div>

                  {/* Email fetch limit */}
                  <div className="settings-card" style={{ marginBottom: '20px' }}>
                    <h3>📨 Email Fetch Limit</h3>
                    <p>Choose how many emails are fetched and summarized each time you sync. Applies globally to all syncs and exports.</p>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem', marginTop: '0.75rem' }}>
                      <input
                        type="number"
                        min={1}
                        max={100}
                        value={fetchLimit}
                        onChange={e => setFetchLimit(Math.min(100, Math.max(1, parseInt(e.target.value) || 1)))}
                        style={{
                          width: '80px',
                          padding: '0.4rem 0.6rem',
                          borderRadius: '8px',
                          border: '1px solid var(--border)',
                          fontSize: '1rem',
                          background: 'var(--surface)',
                          color: 'var(--text)'
                        }}
                      />
                      <span style={{ color: 'var(--text-muted)', fontSize: '0.9rem' }}>emails per sync (1 – 100)</span>
                      <button
                        onClick={() => saveFetchLimit(fetchLimit)}
                        disabled={fetchLimitSaving}
                        className="btn-sync"
                        style={{ marginLeft: 'auto' }}
                      >
                        {fetchLimitSaving ? 'Saving...' : 'Save'}
                      </button>
                    </div>
                  </div>

                  {/* Manage labels */}
                  <div className="settings-card" style={{ marginBottom: '20px' }}>
                    <h3>Signature</h3>
                    <p>This plain-text signature is appended automatically when you send an email.</p>
                    <textarea
                      className="settings-signature"
                      value={signature}
                      maxLength={2000}
                      onChange={event => setSignature(event.target.value)}
                      placeholder={'Best,\nYour name'}
                    />
                    <div className="settings-signature-actions">
                      <span>{signature.length}/2000</span>
                      <button className="btn-sync" onClick={saveSignature} disabled={signatureSaving}>
                        {signatureSaving ? 'Saving...' : 'Save signature'}
                      </button>
                    </div>
                  </div>

                  {/* Manage labels */}
                  <div className="settings-card" style={{ marginBottom: '20px' }}>
                    <h3>🏷️ Manage Labels</h3>
                    <p>
                      Labels are assigned automatically by AI based on each label's description. Presets are built into Maily; you can also create your own.
                    </p>

                    {labels.map(label => (
                      <div key={label.id} className="label-manage-row">
                        {editingLabelId === label.id ? (
                          <>
                            <input
                              type="color"
                              value={editLabelDraft.color}
                              onChange={e => setEditLabelDraft(prev => ({ ...prev, color: e.target.value }))}
                              className="label-color-input"
                            />
                            <input
                              type="text"
                              value={editLabelDraft.name}
                              onChange={e => setEditLabelDraft(prev => ({ ...prev, name: e.target.value }))}
                              className="label-text-input"
                              placeholder="Name"
                            />
                            <input
                              type="text"
                              value={editLabelDraft.description}
                              onChange={e => setEditLabelDraft(prev => ({ ...prev, description: e.target.value }))}
                              className="label-text-input label-description-input"
                              placeholder="Description (used by the AI to classify emails)"
                            />
                            <button
                              className="btn-sync"
                              disabled={labelSaving}
                              onClick={() => updateLabel(label.id, editLabelDraft)}
                            >
                              Save
                            </button>
                            <button className="btn-disconnect" onClick={() => setEditingLabelId(null)}>Cancel</button>
                          </>
                        ) : (
                          <>
                            <span className="label-chip" style={{ background: label.color }}>{label.name}</span>
                            <span className="label-manage-description">{label.description}</span>
                            {isCustomLabel(label) ? (
                              <div style={{ display: 'flex', gap: '0.5rem' }}>
                                <button
                                  className="btn-disconnect"
                                  onClick={() => { setEditingLabelId(label.id); setEditLabelDraft({ name: label.name, description: label.description, color: label.color }); }}
                                >
                                  Edit
                                </button>
                                <button className="btn-disconnect" disabled={labelSaving} onClick={() => deleteLabel(label.id)}>Delete</button>
                              </div>
                            ) : (
                              <span className="label-preset-tag">Preset</span>
                            )}
                          </>
                        )}
                      </div>
                    ))}

                    {/* Create a new custom label */}
                    <div className="label-create-row">
                      <input
                        type="color"
                        value={newLabelColor}
                        onChange={e => setNewLabelColor(e.target.value)}
                        className="label-color-input"
                      />
                      <input
                        type="text"
                        value={newLabelName}
                        onChange={e => setNewLabelName(e.target.value)}
                        className="label-text-input"
                        placeholder="Label name"
                      />
                      <input
                        type="text"
                        value={newLabelDescription}
                        onChange={e => setNewLabelDescription(e.target.value)}
                        className="label-text-input label-description-input"
                        placeholder="Description (used by the AI to classify emails)"
                      />
                      <button onClick={createLabel} disabled={labelSaving} className="btn-connect">
                        {labelSaving ? 'Saving...' : 'Create Label'}
                      </button>
                    </div>
                  </div>

                  {/* Manage custom smart categories */}
                  <div className="settings-card" style={{ marginBottom: '20px' }}>
                    <h3>🚚 Manage Custom Categories</h3>
                    <p>
                      Categories built into Maily can't be changed here. Your own categories (made with the Smart Categories wizard) can be edited, given more AI hints, or deleted.
                    </p>

                    {Object.values(categoryTypeCatalog).filter(ct => !ct.isBuiltIn).map(ct => (
                      <div key={ct.id} className="label-manage-row" style={{ flexWrap: 'wrap' }}>
                        <span className="label-chip" style={{ background: 'var(--accent)' }}>{ct.icon} {ct.label}</span>
                        <span className="label-manage-description">{ct.fields.length} field{ct.fields.length !== 1 ? 's' : ''}</span>
                        <div style={{ display: 'flex', gap: '0.5rem' }}>
                          <button className="btn-disconnect" onClick={() => { setActiveTab('categories'); setOpenedCategoryItem(null); setCategoryWizard({ mode: 'replace', existing: ct }); }}>
                            Edit
                          </button>
                          <button className="btn-disconnect" onClick={() => deleteCategoryType(ct.id)}>Delete</button>
                        </div>
                        <div style={{ display: 'flex', gap: '0.5rem', width: '100%', marginTop: '0.5rem' }}>
                          <input
                            type="text"
                            className="label-text-input label-description-input"
                            value={categoryHintDrafts[ct.id] || ''}
                            onChange={e => setCategoryHintDrafts(prev => ({ ...prev, [ct.id]: e.target.value }))}
                            placeholder="Add a hint to help the AI catch more matching emails"
                          />
                          <button className="btn-sync" onClick={() => appendCategoryClassifierHint(ct.id)}>Add Hint</button>
                        </div>
                      </div>
                    ))}

                    {Object.values(categoryTypeCatalog).filter(ct => !ct.isBuiltIn).length === 0 && (
                      <p style={{ padding: '0.75rem 0', color: 'var(--text-muted)' }}>
                        No custom categories yet — create one from the "+ New Category" button on the Smart Categories tab.
                      </p>
                    )}
                  </div>

                  {/* Connected accounts */}
                  <div className="settings-card">
                    <h3>🔗 Connected Accounts</h3>
                    <p>
                      Connect your Gmail and Outlook accounts to Maily. You can add multiple accounts and filter between them in the Inbox.
                    </p>

                    {/* One row per connected account */}
                    {connectedAccounts.map(account => (
                      <div key={`${account.provider}-${account.email}`} className="account-row connected">
                        <div className="account-info">
                          <span className="account-icon">{account.provider === 'outlook' ? '📨' : '✉️'}</span>
                          <div>
                            <strong className="account-name">
                              {account.email}
                              {account.isPrimary && <span className="account-primary-badge" title="Default sender for Compose"> ⭐ Primary</span>}
                            </strong>
                            {account.needsReauth ? (
                              <span className="account-status-warning">⚠️ Needs reconnecting — access expired or was revoked</span>
                            ) : (
                              <span className="account-status-connected">✅ Connected ({providerLabel(account.provider)})</span>
                            )}
                          </div>
                        </div>
                        <div style={{ display: 'flex', gap: '0.5rem' }}>
                          {account.needsReauth && (
                            <button
                              onClick={() => account.provider === 'outlook' ? connectOutlook() : loginWithGoogle()}
                              className="btn-connect"
                            >
                              Reconnect
                            </button>
                          )}
                          <button onClick={() => disconnectAccount(account.email, account.provider)} className="btn-disconnect">
                            Disconnect
                          </button>
                        </div>
                      </div>
                    ))}

                    {/* Add another account row */}
                    <div className="account-row">
                      <div className="account-info">
                        <span className="account-icon">➕</span>
                        <div>
                          <strong className="account-name">Add Google Account</strong>
                          <span className="account-status-disconnected">Connect another Gmail or Google Workspace</span>
                        </div>
                      </div>
                      <button onClick={() => loginWithGoogle()} className="btn-connect">Connect</button>
                    </div>

                    {/* Add Outlook account row */}
                    <div className="account-row">
                      <div className="account-info">
                        <span className="account-icon">➕</span>
                        <div>
                          <strong className="account-name">Add Outlook Account</strong>
                          <span className="account-status-disconnected">Connect an Outlook.com or Microsoft 365 account</span>
                        </div>
                      </div>
                      <button onClick={connectOutlook} className="btn-connect">Connect</button>
                    </div>
                  </div>
                </div>
              </>
            )}

          </div>

          {/* Toast notifications */}
          <div className="toast-container">
            {toasts.map(toast => (
              <div key={toast.id} className={`toast toast-${toast.type}`}>
                <span className="toast-text">{toast.text}</span>
                <button className="toast-close" onClick={() => setToasts(prev => prev.filter(t => t.id !== toast.id))}>×</button>
              </div>
            ))}
          </div>

          {/* Category Wizard's "select example emails" overlay — a sibling of whatever tab is showing
              (not a tab swap), so the wizard underneath stays mounted with its state intact. */}
          {emailPickerOpen && (
            <EmailSelectionPicker
              title="Select example emails (up to 3)"
              emails={emails}
              labels={labels}
              accounts={connectedAccounts}
              selectedIds={categoryReferenceIds}
              maxSelected={MAX_CATEGORY_REFERENCE_EMAILS}
              onToggle={toggleCategoryReferenceId}
              onDone={() => setEmailPickerOpen(false)}
              onCancel={() => { setCategoryReferenceIds(emailPickerSnapshot); setEmailPickerOpen(false); }}
            />
          )}

          {/* Global compose action — one persistent button instead of a per-tab header button.
              Hidden on Compose (its own footer action bar sits in that same corner) and while the
              Smart Category wizard is open (its "Approve & Create" button sits there too). */}
          {activeTab !== 'compose' && !categoryWizard && (
            <button
              onClick={openCompose}
              className="btn-compose-fab"
              aria-label="Compose"
              title="Compose"
            >
              <SquarePen size={18} strokeWidth={2.25} />
              <span>Compose</span>
            </button>
          )}
        </div>
  );

  return (
    <div className="login-shell" data-theme="midnight">
      <Authenticator loginMechanisms={['email']} components={{
          Header() {
            return (
              <div className="login-header">
                <img src="/maily-logo.png" alt="Maily" className="login-header-logo" />
                <p className="login-header-title">Maily</p>
                <p className="login-header-subtitle">Smart Email Assistant</p>
              </div>
            );
          }
        }}>
        {renderApp}
      </Authenticator>
    </div>
  );
}

export default App;