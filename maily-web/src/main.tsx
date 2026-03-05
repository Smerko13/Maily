import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { Amplify } from 'aws-amplify'
import '@aws-amplify/ui-react/styles.css'

Amplify.configure({
  Auth: {
    Cognito: {
      userPoolId: 'eu-central-1_gkiKBryQu',
      userPoolClientId: '42paod9qpsaod46hi5h3gbe56u'
    }
  }
});

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
