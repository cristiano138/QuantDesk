import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import App from './App'
import './index.css'

// 后端（quantdesk/app.py）静态托管 dist 并对非 /api 路径回落 index.html，
// 因此用 History 路由：/channels、/interop 可直连访问。
ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </React.StrictMode>,
)
