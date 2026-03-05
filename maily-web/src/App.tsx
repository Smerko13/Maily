import { useState } from 'react';
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
    <div className="App">
      <h1>Maily-Frontend</h1>
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
  )
}

export default App