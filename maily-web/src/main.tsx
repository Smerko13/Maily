import React, { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { Amplify } from 'aws-amplify'
import '@aws-amplify/ui-react/styles.css'
import { GoogleOAuthProvider } from '@react-oauth/google'

Amplify.configure({
  Auth: {
    Cognito: {
      userPoolId: 'eu-central-1_gkiKBryQu',
      userPoolClientId: '42paod9qpsaod46hi5h3gbe56u'
    }
  }
});

createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <GoogleOAuthProvider clientId="720584589522-3pl53srj5dbl28hv39hpfto4qmm78t6f.apps.googleusercontent.com">
      <App />
    </GoogleOAuthProvider>
  </React.StrictMode>,
)
