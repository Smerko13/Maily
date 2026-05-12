import { useState, useEffect } from "react";
import { Authenticator } from "@aws-amplify/ui-react";
import { fetchAuthSession } from 'aws-amplify/auth';
import { useGoogleLogin } from '@react-oauth/google';
import './App.css';

interface Email {
  subject: string;
  content: string;
  summary?: string;
  status?: string;
}

interface Stats {
  total: number;
  unread: number;
  read: number;
  top_senders: { sender: string; count: number }[];
}

function App() {
  const [message, setMessage] = useState<string>(''); // Google auth status (used in Settings tab)
  const [syncMessage, setSyncMessage] = useState<string>(''); // inbox sync status
  const [emails, setEmails] = useState<Email[]>([]); // the array of email objects displayed in the inbox
  const [loading, setLoading] = useState<boolean>(false); // true/false to disable the Sync button while fetching
  const [activeTab, setActiveTab] = useState<'inbox' | 'settings' | 'stats' | 'drafting'>('inbox'); // which tab is visible
  const [stats, setStats] = useState<Stats | null>(null);
  const [statsLoading, setStatsLoading] = useState<boolean>(false);
  const [statsError, setStatsError] = useState<string>('');
  const [selectedEmailIndex, setSelectedEmailIndex] = useState<number | null>(null); // index of the email selected for drafting
  const [draft, setDraft] = useState<string>(''); // the AI-generated reply draft
  const [draftLoading, setDraftLoading] = useState<boolean>(false);
  const [exportLoading, setExportLoading] = useState<boolean>(false);
  const [exportUrl, setExportUrl] = useState<string>('');
  const [isGoogleConnected, setIsGoogleConnected] = useState(() => { 
  return localStorage.getItem('isGoogleConnected') === 'true'; // initialized from localStorage so it survives a page refresh
}); 

  // Auto-load emails from DynamoDB on page load (for returning users who refresh the page)
  useEffect(() => {
    const loadEmails = async () => {
      try {
        const session = await fetchAuthSession();
        const token = session.tokens?.idToken?.toString();
        if (!token) return; // not logged in yet, silently skip
        const response = await fetch(`${import.meta.env.VITE_API_BASE_URL}/hello`, {
          headers: { Authorization: `Bearer ${token}` }
        });
        if (!response.ok) return;
        const data = await response.json();
        if (data.emails && data.emails.length > 0) setEmails(data.emails);
      } catch {
        // silently fail — the user can always click Sync manually
      }
    };
    loadEmails();
  }, []);

  // Google login handler
 const loginWithGoogle = useGoogleLogin({
    flow: 'auth-code',
    scope: 'https://www.googleapis.com/auth/gmail.readonly',
    onSuccess: async (codeResponse) => {
      console.log("Success! Auth Code from Google:", codeResponse.code);
      setMessage("⏳ Auth code received! Connecting to Google...");

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

        localStorage.setItem('isGoogleConnected', 'true');
        setIsGoogleConnected(true);
        
        const data = await response.json();
        console.log("Backend response:", data);
        
        setMessage("✅ Access token received from backend! Google account connected successfully.");
        
      } catch (error) {
        console.error('Error sending code to backend:', error);
        setMessage('❌ Error connecting to Google. Please try again.');
        setIsGoogleConnected(false); 
        localStorage.removeItem('isGoogleConnected');
      }
    },
    onError: (errorResponse) => {
      console.error("Google Login Failed:", errorResponse);
      setMessage("❌ Error connecting to Google. Please try again.");
    },
  });

  // Fetch statistics function
  const fetchStats = async () => {
    setStatsLoading(true);
    setStatsError('');
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
      setStatsError('❌ Failed to load statistics. Please try again.');
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

      setSyncMessage(data.message);
      if (data.emails) setEmails(data.emails);
    } catch (error) {
      console.error('Error fetching data from backend:', error);
      setSyncMessage('❌ Error pulling data from backend');
    } finally {
      setLoading(false);
    }
  };

  //The UI / JSX
  return (
    <Authenticator loginMechanisms={['email']}>
      {({ signOut, user }) => (
        <div className="app-layout">

          {/* Sidebar */}
          <div className="sidebar">
            <div className="sidebar-header">
              <h2 className="sidebar-logo">📧 Maily</h2>
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
              <>
                <header className="tab-header">
                  <h1>Overview</h1>
                  <button onClick={fetchFromBackend} disabled={loading} className="btn-sync">
                    {loading ? 'Loading data...' : '🔄 Sync with Server'}
                  </button>
                </header>

                <div className="tab-body">
                  <div className="email-card">
                    <div className="email-card-header">
                      <h3>📬 Latest Email Analysis</h3>
                      {syncMessage && <span className="status-badge">{syncMessage}</span>}
                    </div>

                    {emails.length > 0 ? (
                      <div className="email-list">
                        {emails.map((email, index) => (
                          <div key={index} className="email-item">
                            <div className="email-item-header">
                              <strong className="email-subject">{email.subject}</strong>
                              <span className="email-status">
                                STATUS: {email.status ? email.status.toUpperCase() : 'N/A'}
                              </span>
                            </div>
                            {email.summary && (
                              <p className="email-summary"><strong>Summary:</strong> {email.summary}</p>
                            )}
                            <p className="email-content">{email.content}</p>
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

                            {draft && (
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

                  {statsLoading && <p style={{ padding: '1rem' }}>Loading statistics...</p>}

                  {statsError && (
                    <div className="stats-error">{statsError}</div>
                  )}

                  {!statsLoading && !statsError && stats && (
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

                  {!statsLoading && !statsError && !stats && (
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
                  <div className="settings-card">
                    <h3>🔗 Connected Accounts</h3>
                    <p>
                      Connect your email accounts to Maily to allow our smart AI to analyze, summarize, and assist you with your inbox.
                    </p>

                    <div className={`account-row ${isGoogleConnected ? 'connected' : ''}`}>
                      <div className="account-info">
                        <span className="account-icon">✉️</span>
                        <div>
                          <strong className="account-name">Google Workspace / Gmail</strong>
                          {isGoogleConnected ? (
                            <span className="account-status-connected">✅ Connected successfully</span>
                          ) : (
                            <span className="account-status-disconnected">Not connected</span>
                          )}
                        </div>
                      </div>

                      {isGoogleConnected ? (
                        <button disabled className="btn-connected">Connected</button>
                      ) : (
                        <button onClick={() => loginWithGoogle()} className="btn-connect">Connect</button>
                      )}
                    </div>

                    {isGoogleConnected && (
                      <div className="connection-success">
                        <strong>{message}</strong>
                      </div>
                    )}
                  </div>
                </div>
              </>
            )}

          </div>
        </div>
      )}
    </Authenticator>
  );
}

export default App;