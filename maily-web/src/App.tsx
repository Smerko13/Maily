import { useState } from "react";
import { Authenticator } from "@aws-amplify/ui-react";
import { fetchAuthSession } from 'aws-amplify/auth';
import { useGoogleLogin } from '@react-oauth/google';
import './App.css';

function App() {
  const [message, setMessage] = useState<string>(''); // state to hold status messages for the UI, this will be used to display feedback to the user about actions like fetching data from the backend or connecting their Google account
  const [emails, setEmails] = useState<any[]>([]); // state to hold the list of emails fetched from the backend, this will be an array of email objects that we will display in the inbox tab of the UI
  const [loading, setLoading] = useState<boolean>(false); // state to indicate whether we are currently loading data from the backend, this will be used to disable the sync button and show a loading state while we are fetching data
  const [activeTab, setActiveTab] = useState<'inbox' | 'settings'>('inbox'); // state to track which tab is currently active in the UI, this will allow us to conditionally render the content for the inbox and settings tabs based on the user's selection
  const [isGoogleConnected, setIsGoogleConnected] = useState<boolean>(false); // state to track whether the user has successfully connected their Google account, this will be used to update the UI and show the connection status in the settings tab
  const [googleAuthCode, setGoogleAuthCode] = useState<string>(''); // state to hold the Google OAuth authorization code received after a successful login, this code can be used to exchange for access tokens that allow us to access the user's Gmail data, we will display this code in the UI (truncated for security) to confirm the connection was successful

  // Set up the Google login flow using the useGoogleLogin hook, this hook provides a function that we can call to initiate the Google OAuth flow, we specify the flow type as 'auth-code' to receive an authorization code, and we request the scope for read-only access to Gmail, we also define onSuccess and onError callbacks to handle the response from Google and update our UI accordingly
  const loginWithGoogle = useGoogleLogin({
    flow: 'auth-code',
    scope: 'https://www.googleapis.com/auth/gmail.readonly',
    onSuccess: async (codeResponse) => {
      console.log("Success! Auth Code from Google:", codeResponse.code);
      setIsGoogleConnected(true);
      setGoogleAuthCode(codeResponse.code); 
      setMessage("✅ Google account connected successfully!");
    },
    onError: (errorResponse) => {
      console.error("Google Login Failed:", errorResponse);
      setMessage("❌ Error connecting Google account. Please try again.");
    },
  });

  // Function to fetch data from the backend API, this function will be called when the user clicks the "Sync with Server" button in the inbox tab, it retrieves the current authentication session to get the user's ID token, then makes a GET request to our backend API endpoint with the token included in the Authorization header, if the request is successful it updates the message and emails state with the response data, if there is an error it logs it and updates the message state to inform the user
  const fetchFromBackend = async () => {
    setLoading(true);
    try {
      const session = await fetchAuthSession();
      const token = session.tokens?.idToken?.toString();
      const apiUrl = 'https://9h964a0yle.execute-api.eu-central-1.amazonaws.com/hello';
      // Make a GET request to the backend API with the token in the Authorization header
      const response = await fetch(apiUrl, {
        method: 'GET',
        headers: {
          Authorization: `Bearer ${token}`,
          'Content-Type': 'application/json'
        }
      });
      // Check if the response is successful, if not throw an error to be caught in the catch block
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

  // The return statement contains the JSX that defines the UI of the application, we use the Authenticator component from AWS Amplify to handle user authentication, inside it we have a layout with a sidebar for navigation and a main content area, we conditionally render the content of the inbox and settings tabs based on the activeTab state, we also display messages and email data based on the state variables we defined earlier
  return (
    <Authenticator loginMechanisms={['email']}>
      {({ signOut, user }) => (
        <div style={{ display: 'flex', height: '100vh', width: '100vw', backgroundColor: '#f8f9fa', fontFamily: 'Segoe UI, Tahoma, Geneva, Verdana, sans-serif' }}>
          
          <div style={{ width: '250px', backgroundColor: '#2c3e50', color: '#ecf0f1', display: 'flex', flexDirection: 'column', boxShadow: '2px 0 5px rgba(0,0,0,0.1)', zIndex: 10 }}>
            <div style={{ padding: '20px', borderBottom: '1px solid #34495e' }}>
              <h2 style={{ margin: 0, color: '#3498db', display: 'flex', alignItems: 'center', gap: '10px' }}>📧 Maily</h2>
              <p style={{ fontSize: '0.8em', color: '#95a5a6', marginTop: '5px' }}>Smart Email Assistant</p>
            </div>
            
            <div style={{ flex: 1, padding: '20px 0' }}>
              <div onClick={() => setActiveTab('inbox')} style={{ padding: '15px 20px', cursor: 'pointer', backgroundColor: activeTab === 'inbox' ? '#34495e' : 'transparent', borderLeft: activeTab === 'inbox' ? '4px solid #3498db' : 'none', opacity: activeTab === 'inbox' ? 1 : 0.7 }}>
                📥 Inbox
              </div>
              <div style={{ padding: '15px 20px', cursor: 'pointer', opacity: 0.7 }}>✨ Smart Drafting</div>
              <div style={{ padding: '15px 20px', cursor: 'pointer', opacity: 0.7 }}>📊 Statistics</div>
              <div onClick={() => setActiveTab('settings')} style={{ padding: '15px 20px', cursor: 'pointer', backgroundColor: activeTab === 'settings' ? '#34495e' : 'transparent', borderLeft: activeTab === 'settings' ? '4px solid #3498db' : 'none', opacity: activeTab === 'settings' ? 1 : 0.7 }}>
                ⚙️ Settings
              </div>
            </div>

            <div style={{ padding: '20px', borderTop: '1px solid #34495e', backgroundColor: '#243342' }}>
              <p style={{ margin: '0 0 10px 0', fontSize: '0.85em', wordBreak: 'break-all' }}>Logged in as:<br/><strong>{user?.signInDetails?.loginId || user?.username}</strong></p>
              <button onClick={signOut} style={{ width: '100%', padding: '8px', backgroundColor: 'transparent', color: '#e74c3c', border: '1px solid #e74c3c', borderRadius: '4px', cursor: 'pointer', transition: '0.3s' }}>
                Log Out
              </button>
            </div>
          </div>

          <div style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
            {/* Inbox Tab Content */}
            {activeTab === 'inbox' && (
              <>
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
                      {message && !message.includes('קוד') && <span style={{ fontSize: '0.85em', color: '#27ae60', backgroundColor: '#e8f8f5', padding: '4px 10px', borderRadius: '12px' }}>{message}</span>}
                    </div>
                    
                    {emails.length > 0 ? (
                      <div style={{ display: 'flex', flexDirection: 'column', gap: '15px' }}>
                        {emails.map((email, index) => (
                          <div key={index} style={{ padding: '20px', border: '1px solid #e2e8f0', borderRadius: '8px', backgroundColor: '#fdfdfd' }}>
                            <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '10px' }}>
                              <strong style={{ fontSize: '1.1em', color: '#2c3e50' }}>{email.subject}</strong>
                              <span style={{ fontSize: '0.8em', color: '#8e44ad', backgroundColor: '#f4ecf8', padding: '4px 8px', borderRadius: '4px', fontWeight: 'bold' }}>
                                STATUS: {email.status ? email.status.toUpperCase() : 'N/A'}
                              </span>
                            </div>
                            <p style={{ margin: 0, color: '#7f8c8d', lineHeight: '1.5' }}>{email.content}</p>
                          </div>
                        ))}
                      </div>
                    ) : (
                      <div style={{ textAlign: 'center', color: '#95a5a6', padding: '40px 0' }}>
                        <div style={{ fontSize: '3em', marginBottom: '10px' }}>📭</div>
                        <p>No emails found in the database.<br/>Click Sync to fetch them!</p>
                      </div>
                    )}
                  </div>
                </div>
              </>
            )}
            {/* Settings Tab Content */}
            {activeTab === 'settings' && (
              <>
                <header style={{ backgroundColor: 'white', padding: '20px 40px', display: 'flex', justifyContent: 'space-between', alignItems: 'center', boxShadow: '0 2px 4px rgba(0,0,0,0.05)' }}>
                  <h1 style={{ margin: 0, fontSize: '1.5em', color: '#2c3e50' }}>Settings</h1>
                </header>

                <div style={{ padding: '40px', flex: 1, overflowY: 'auto' }}>
                  <div style={{ backgroundColor: 'white', borderRadius: '10px', padding: '25px', boxShadow: '0 4px 6px rgba(0,0,0,0.05)', maxWidth: '600px' }}>
                    <h3 style={{ margin: '0 0 20px 0', color: '#34495e' }}>🔗 Connected Accounts</h3>
                    <p style={{ color: '#7f8c8d', marginBottom: '20px', lineHeight: '1.5' }}>
                      Connect your email accounts to Maily to allow our smart AI to analyze, summarize, and assist you with your inbox.
                    </p>
                    
                    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '20px', border: '1px solid #e2e8f0', borderRadius: '8px', backgroundColor: isGoogleConnected ? '#f0fff4' : '#f8f9fa', transition: '0.3s' }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '15px' }}>
                        <span style={{ fontSize: '2em' }}>✉️</span>
                        <div>
                          <strong style={{ display: 'block', color: '#2c3e50' }}>Google Workspace / Gmail</strong>
                          {/* status messages that show the connection status */}
                          {isGoogleConnected ? (
                            <span style={{ fontSize: '0.85em', color: '#27ae60', fontWeight: 'bold' }}>✅ Connected successfully</span>
                          ) : (
                            <span style={{ fontSize: '0.85em', color: '#95a5a6' }}>Not connected</span>
                          )}
                        </div>
                      </div>
                      
                      {/* change the button text and color based on the connection status */}
                      {isGoogleConnected ? (
                         <button disabled style={{ padding: '10px 20px', backgroundColor: '#e2e8f0', color: '#7f8c8d', border: 'none', borderRadius: '6px', fontWeight: 'bold', cursor: 'default' }}>
                           Connected
                         </button>
                      ) : (
                        <button onClick={() => loginWithGoogle()} style={{ padding: '10px 20px', backgroundColor: '#db4437', color: 'white', border: 'none', borderRadius: '6px', cursor: 'pointer', fontWeight: 'bold', boxShadow: '0 2px 4px rgba(219,68,55,0.3)' }}>
                          Connect
                        </button>
                      )}
                    </div>

                    {/* status messages that show the connection status and auth code */}
                    {isGoogleConnected && (
                      <div style={{ marginTop: '20px', padding: '15px', backgroundColor: '#e8f8f5', color: '#27ae60', borderRadius: '6px', fontSize: '0.9em', borderLeft: '4px solid #27ae60' }}>
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