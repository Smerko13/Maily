import { useState } from "react";
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

function App() {
  const [message, setMessage] = useState<string>(''); // a string shown to the user (e.g. "✅ Connected!")
  const [emails, setEmails] = useState<Email[]>([]); // the array of email objects displayed in the inbox
  const [loading, setLoading] = useState<boolean>(false); // true/false to disable the Sync button while fetching
  const [activeTab, setActiveTab] = useState<'inbox' | 'settings'>('inbox'); // which tab is visible; TypeScript restricts it to only 'inbox' or 'settings'
  const [isGoogleConnected, setIsGoogleConnected] = useState(() => { 
  return localStorage.getItem('isGoogleConnected') === 'true'; // initialized from localStorage so it survives a page refresh
}); 

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

      setMessage(data.message);
      if (data.emails) setEmails(data.emails);
    } catch (error) {
      console.error('Error fetching data from backend:', error);
      setMessage('Error pulling data from backend');
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
              <div className="nav-item-disabled">✨ Smart Drafting</div>
              <div className="nav-item-disabled">📊 Statistics</div>
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
                      {message && !message.includes('code') && <span className="status-badge">{message}</span>}
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