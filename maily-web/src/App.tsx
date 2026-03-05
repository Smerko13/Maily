import { useState } from 'react';
import { Authenticator } from '@aws-amplify/ui-react';
import './App.css'

function App() {
  const [message, setMessage] = useState<string>('')
  const [loading, setLoading] = useState<boolean>(false)

  const fetchFromBackend = async () => {
    setLoading(true)
    try {
      const apiUrl = 'https://9h964a0yle.execute-api.eu-central-1.amazonaws.com/hello'

      const response = await fetch(apiUrl)
      const data = await response.json()
      setMessage(data)
    } catch (error) {
      console.error('Error fetching data from backend:', error)
      setMessage('Failed to fetch message from backend.')
    } finally {
      setLoading(false)
    }
  }
  
  return (
    <Authenticator loginMechanisms={['email']}>
      {({ signOut, user }) => (
        <div className="App">
          <h1>Maily-Frontend</h1>

          <div style={{ marginBottom: '20px', padding: '10px', backgroundColor: '#f0f0f0', borderRadius: '8px', color: '#333' }}>
            <p style={{ margin: 0 }}>Welcome, <strong>{user?.signInDetails?.loginId || user?.username}</strong>!</p>
            <button onClick={signOut} style={{ marginTop: '10px', padding: '8px 16px', backgroundColor: '#646cff', color: '#fff', border: 'none', borderRadius: '4px', cursor: 'pointer' }}>
              Sign Out
            </button>
          </div>

          <button onClick={fetchFromBackend} disabled={loading}>
            {loading ? 'Loading...' : 'Fetch Message from Backend'}
          </button>
          
          {message && ( 
            <div style={{ marginTop: '20px' , padding: '15px', border: '1px solid #646cff', borderRadius: '8px' }}>
              <p style={{ margin: 0, color: '#888' }}>The message that was fetched from the backend:</p>
              <strong style={{fontSize: '1.2em'}}>{message}</strong>
            </div>
          )}
        </div>
      )}
    </Authenticator>
  );
}

export default App;