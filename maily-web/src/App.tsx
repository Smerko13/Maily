import { useState } from "react";
import { Authenticator } from "@aws-amplify/ui-react";
import { fetchAuthSession } from "aws-amplify/auth";
import './App.css';

function App() {
  const [message, setMessage] = useState<string>('');
  const [emails, setEmails] = useState<any[]>([]); // A simple array to hold email data from the backend
  const [loading, setLoading] = useState<boolean>(false);

  const fetchFromBackend = async () => {
    setLoading(true);
    try {
      // 1. Getting the current authenticated session to retrieve the ID token (JWT)
      const session = await fetchAuthSession();
      const token = session.tokens?.idToken?.toString();

      const apiUrl = 'https://9h964a0yle.execute-api.eu-central-1.amazonaws.com/hello';
      
      // 2. Attaching the token to the request headers
      const response = await fetch(apiUrl, {
        method: 'GET',
        headers: {
          Authorization: `Bearer ${token}`, // Here we send the identity token
          'Content-Type': 'application/json'
        }
      });
      
      // Checking if the server rejected us (e.g., expired token)
      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`);
      }

      const data = await response.json();
      
      setMessage(data.message);
      if (data.emails) {
        setEmails(data.emails);
      }
    } catch (error) {
      console.error('Error fetching data from backend:', error);
      setMessage('Error fetching data from backend - check console for details.');
    } finally {
      setLoading(false);
    }
  };

  return (
    <Authenticator loginMechanisms={['email']}>
      {({ signOut, user }) => (
        <div style={{ display: 'flex', height: '100vh', width: '100vw', backgroundColor: '#f8f9fa', fontFamily: 'Segoe UI, Tahoma, Geneva, Verdana, sans-serif' }}>
          
          {/* Side Menu */}
          <div style={{ width: '250px', backgroundColor: '#2c3e50', color: '#ecf0f1', display: 'flex', flexDirection: 'column', boxShadow: '2px 0 5px rgba(0,0,0,0.1)' }}>
            <div style={{ padding: '20px', borderBottom: '1px solid #34495e' }}>
              <h2 style={{ margin: 0, color: '#3498db', display: 'flex', alignItems: 'center', gap: '10px' }}>📧 Maily</h2>
              <p style={{ fontSize: '0.8em', color: '#95a5a6', marginTop: '5px' }}>Smart Email Assistant</p>
            </div>
            
            <div style={{ flex: 1, padding: '20px 0' }}>
              <div style={{ padding: '15px 20px', cursor: 'pointer', backgroundColor: '#34495e', borderLeft: '4px solid #3498db' }}>📥 Inbox</div>
              <div style={{ padding: '15px 20px', cursor: 'pointer', opacity: 0.7 }}>✨ Smart Drafting</div>
              <div style={{ padding: '15px 20px', cursor: 'pointer', opacity: 0.7 }}>📊 Statistics</div>
              <div style={{ padding: '15px 20px', cursor: 'pointer', opacity: 0.7 }}>⚙️ Settings</div>
            </div>

            <div style={{ padding: '20px', borderTop: '1px solid #34495e', backgroundColor: '#243342' }}>
              <p style={{ margin: '0 0 10px 0', fontSize: '0.85em', wordBreak: 'break-all' }}>
                 Logged in as:<br/><strong>{user?.signInDetails?.loginId || user?.username}</strong>
              </p>
              <button onClick={signOut} style={{ width: '100%', padding: '8px', backgroundColor: 'transparent', color: '#e74c3c', border: '1px solid #e74c3c', borderRadius: '4px', cursor: 'pointer', transition: '0.3s' }}>
                Log Out
              </button>
            </div>
          </div>

          {/* Central Area */}
          <div style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
            <header style={{ backgroundColor: 'white', padding: '20px 40px', display: 'flex', justifyContent: 'space-between', alignItems: 'center', boxShadow: '0 2px 4px rgba(0,0,0,0.05)' }}>
              <h1 style={{ margin: 0, fontSize: '1.5em', color: '#2c3e50' }}>Overview</h1>
              <button onClick={fetchFromBackend} disabled={loading} style={{ padding: '10px 20px', backgroundColor: '#3498db', color: 'white', border: 'none', borderRadius: '6px', cursor: loading ? 'not-allowed' : 'pointer', fontWeight: 'bold' }}>
                {loading ? 'Loading data...' : '🔄 Sync with Server'}
              </button>
            </header>

            <div style={{ padding: '40px', flex: 1, overflowY: 'auto' }}>
              <div style={{ backgroundColor: 'white', borderRadius: '10px', padding: '25px', boxShadow: '0 4px 6px rgba(0,0,0,0.05)' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', borderBottom: '2px solid #f1f2f6', paddingBottom: '10px', marginBottom: '20px' }}>
                  <h3 style={{ margin: 0, color: '#34495e' }}>📬 Latest Email Analysis</h3>
                  {message && <span style={{ fontSize: '0.85em', color: '#27ae60', backgroundColor: '#e8f8f5', padding: '4px 10px', borderRadius: '12px' }}>{message}</span>}
                </div>
                
                {/* Rendering the list of emails */}
                {emails.length > 0 ? (
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '15px' }}>
                    {emails.map((email, index) => (
                      <div key={index} style={{ padding: '20px', border: '1px solid #e2e8f0', borderRadius: '8px', backgroundColor: '#fdfdfd', transition: '0.2s' }}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '10px' }}>
                          <strong style={{ fontSize: '1.1em', color: '#2c3e50' }}>{email.subject}</strong>
                          <span style={{ fontSize: '0.8em', color: '#8e44ad', backgroundColor: '#f4ecf8', padding: '4px 8px', borderRadius: '4px', fontWeight: 'bold' }}>
                            STATUS: {email.status.toUpperCase()}
                          </span>
                        </div>
                        <p style={{ margin: 0, color: '#7f8c8d', lineHeight: '1.5' }}>{email.content}</p>
                      </div>
                    ))}
                  </div>
                ) : (
                  <div style={{ textAlign: 'center', color: '#95a5a6', padding: '40px 0' }}>
                    <div style={{ fontSize: '3em', marginBottom: '10px' }}>📭</div>
                    <p>No emails found in the database.<br/>(If you ran the previous tests, click Sync to fetch them!)</p>
                  </div>
                )}
              </div>
            </div>

          </div>
        </div>
      )}
    </Authenticator>
  );
}

export default App;