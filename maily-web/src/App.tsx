import { useState, useEffect } from "react";
import { Authenticator } from "@aws-amplify/ui-react";
import { fetchAuthSession } from 'aws-amplify/auth';
import { useGoogleLogin } from '@react-oauth/google';
import './App.css';

interface Attachment {
  id: string;
  filename: string;
  mimeType: string;
  size: number;
}

type Provider = 'gmail' | 'outlook';

interface Email {
  emailId?: string;
  subject: string;
  content: string;
  summary?: string;
  status?: string;
  provider?: Provider;
  providerEmail?: string;
  attachments?: Attachment[];
  threadId?: string;
  inReplyTo?: string;
  receivedAt?: string;
  bodyText?: string | null;
  bodyHtml?: string | null;
}

interface ConnectedAccount {
  email: string;
  provider: Provider;
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

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

const THEMES: { id: ThemeId; label: string }[] = [
  { id: 'indigo',   label: 'Indigo'   },
  { id: 'ocean',    label: 'Ocean'    },
  { id: 'rose',     label: 'Rose'     },
  { id: 'emerald',  label: 'Emerald'  },
  { id: 'midnight', label: 'Midnight' },
];

function App() {
  const [emails, setEmails] = useState<Email[]>([]); // the array of email objects displayed in the inbox
  const [loading, setLoading] = useState<boolean>(false); // true/false to disable the Sync button while fetching
  const [activeTab, setActiveTab] = useState<'inbox' | 'settings' | 'stats' | 'drafting'>('inbox'); // which tab is visible
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
  const [exportLoading, setExportLoading] = useState<boolean>(false);
  const [exportUrl, setExportUrl] = useState<string>('');
  const [fetchLimit, setFetchLimit] = useState<number>(
    () => parseInt(localStorage.getItem('mailyFetchLimit') ?? '10', 10)
  );
  const [fetchLimitSaving, setFetchLimitSaving] = useState<boolean>(false);
  const [connectedAccounts, setConnectedAccounts] = useState<ConnectedAccount[]>([]);
  const [accountFilter, setAccountFilter] = useState<string>('all');
  const [theme, setTheme] = useState<ThemeId>(
    () => (localStorage.getItem('mailyTheme') as ThemeId) ?? 'indigo'
  );
  const [expandedEmailId, setExpandedEmailId] = useState<string | null>(null);
  const [emailBodies, setEmailBodies] = useState<Record<string, EmailBody>>({});
  const [bodyLoadingId, setBodyLoadingId] = useState<string | null>(null);
  const [attachmentLoadingId, setAttachmentLoadingId] = useState<string | null>(null);
  const [threadEmails, setThreadEmails] = useState<Record<string, Email[]>>({});
  const [threadLoadingId, setThreadLoadingId] = useState<string | null>(null);
  const [openedEmail, setOpenedEmail] = useState<Email | null>(null); // set = showing the detail view instead of the inbox list

  useEffect(() => {
    localStorage.setItem('mailyTheme', theme);
  }, [theme]);

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
    } catch {
      // silently fail
    }
  };

  // Load connected email accounts (Gmail + Outlook) on page load
  useEffect(() => {
    loadConnectedAccounts();
  }, []);

  // Handle the redirect back from Microsoft after the user approves (or cancels) Outlook access.
  // Full-page redirect flow: Azure has no popup/postMessage trick like Google's, so we land back
  // on our own origin at /oauth/outlook/callback with ?code=&state= and exchange the code server-side.
  useEffect(() => {
    if (window.location.pathname !== '/oauth/outlook/callback') return;

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
      } catch (error) {
        console.error('Error connecting Outlook account:', error);
        showToast('Error connecting to Outlook. Please try again.', 'error');
      }
    };

    handleOutlookCallback();
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
        if (Array.isArray(data.emails)) setEmails(data.emails);
      } catch {
        // silently fail — the user can always click Sync manually
      }
    };
    loadEmails();
  }, [accountFilter]);

  // Google login handler — works for both first account and adding more
 const loginWithGoogle = useGoogleLogin({
    flow: 'auth-code',
    scope: 'https://www.googleapis.com/auth/gmail.readonly',
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
      scope: 'openid profile email offline_access https://graph.microsoft.com/User.Read https://graph.microsoft.com/Mail.Read',
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
  const fetchDraft = async (email: Email) => {
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
          content: email.content
        })
      });
      if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
      const data = await response.json();
      setDraft(data.draft);
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

  // Sync emails function
  const fetchFromBackend = async () => {
    setLoading(true);
    try {
      const session = await fetchAuthSession();
      const token = session.tokens?.idToken?.toString();
      if (!token) throw new Error('No auth token available');

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
      if (data.emails) setEmails(data.emails);
    } catch (error) {
      console.error('Error fetching data from backend:', error);
      showToast('Error pulling data from backend', 'error');
    } finally {
      setLoading(false);
    }
  };

  //The UI / JSX
  return (
    <Authenticator loginMechanisms={['email']} components={{
        Header() {
          return (
            <div style={{ textAlign: 'center', paddingTop: '32px', paddingBottom: '8px' }}>
              <img src="/maily-logo.png" alt="Maily" style={{ width: '72px', height: '72px', borderRadius: '18px', objectFit: 'cover', boxShadow: '0 4px 16px rgba(0,0,0,0.12)' }} />
              <p style={{ margin: '10px 0 0', fontWeight: 700, fontSize: '1.2em', color: '#0f172a', fontFamily: 'Inter, sans-serif' }}>Maily</p>
              <p style={{ margin: '4px 0 0', fontSize: '0.78em', color: '#64748b', fontFamily: 'Inter, sans-serif', textTransform: 'uppercase', letterSpacing: '0.8px' }}>Smart Email Assistant</p>
            </div>
          );
        }
      }}>
      {({ signOut, user }) => (
        <div className="app-layout" data-theme={theme}>

          {/* Sidebar */}
          <div className="sidebar">
            <div className="sidebar-header">
              <h2 className="sidebar-logo"><img src="/maily-logo.png" alt="Maily" className="sidebar-logo-img" /><span className="logo-text">Maily</span></h2>
              <p className="sidebar-subtitle">Smart Email Assistant</p>
            </div>

            <div className="sidebar-nav">
              <div onClick={() => setActiveTab('inbox')} className={`nav-item ${activeTab === 'inbox' ? 'active' : ''}`}>
                📥 Inbox
              </div>
              <div onClick={() => { setActiveTab('drafting'); setSelectedEmailIndex(null); setDraft(''); }} className={`nav-item ${activeTab === 'drafting' ? 'active' : ''}`}>✨ Smart Drafting</div>
              <div onClick={() => { setActiveTab('stats'); if (!stats) fetchStats(); }} className={`nav-item ${activeTab === 'stats' ? 'active' : ''}`}>📊 Statistics</div>
              <div onClick={() => setActiveTab('settings')} className={`nav-item ${activeTab === 'settings' ? 'active' : ''}`}>
                ⚙️ Settings
              </div>
            </div>

            <div className="sidebar-footer">
              <p className="sidebar-user">Logged in as:<br/><strong>{user?.signInDetails?.loginId || user?.username}</strong></p>
              <button onClick={signOut} className="btn-logout">Log Out</button>
            </div>
          </div>

          {/* Main content */}
          <div className="main-content">

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
                                              onClick={e => { e.stopPropagation(); msg.emailId && downloadAttachment(msg.emailId, att); }}
                                            >
                                              📎 {att.filename} ({formatFileSize(att.size)})
                                              {attachmentLoadingId === att.id ? ' ⏳' : ''}
                                            </button>
                                          ))}
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
                      {loading ? 'Loading data...' : '🔄 Sync with Server'}
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

                  <div className="tab-body">
                    <div className="email-card">
                      <div className="email-card-header">
                        <h3>📬 Latest Email Analysis</h3>
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
                              <div className="skeleton skeleton-row medium" />
                            </div>
                          ))}
                        </div>
                      ) : emails.length > 0 ? (
                        <div className="email-list">
                          {emails.map((email, index) => (
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
                                      onClick={e => { e.stopPropagation(); email.emailId && downloadAttachment(email.emailId, att); }}
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
                      ) : (
                        <div className="empty-inbox">
                          <div className="empty-inbox-icon">📭</div>
                          <p>No emails found in the database.<br/>Click Sync to fetch them!</p>
                        </div>
                      )}
                    </div>
                  </div>
                </>
              )
            )}

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

                            <button
                              onClick={() => fetchDraft(emails[selectedEmailIndex!])}
                              disabled={draftLoading}
                              className="btn-sync"
                              style={{ marginBottom: '1rem' }}
                            >
                              {draftLoading ? '⏳ Generating...' : '✨ Generate Draft Reply'}
                            </button>

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
                        <div className="stat-card">
                          <div className="stat-number">{stats.total}</div>
                          <div className="stat-label">Total Emails</div>
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
                            <strong className="account-name">{account.email}</strong>
                            <span className="account-status-connected">✅ Connected ({providerLabel(account.provider)})</span>
                          </div>
                        </div>
                        <button onClick={() => disconnectAccount(account.email, account.provider)} className="btn-disconnect">
                          Disconnect
                        </button>
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
        </div>
      )}
    </Authenticator>
  );
}

export default App;