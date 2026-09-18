import React, { useState } from 'react';
import { ChatBox } from './components/ChatBox';
import './App.css';
import { PackageSearch, UserPlus, Database } from 'lucide-react';

function App() {
  const [mode, setMode] = useState<'onboarding' | 'search'>('search');

  return (
    <div className="app-container">
      {/* Sidebar */}
      <div className="sidebar glass-panel" style={{ border: 'none', borderRight: '1px solid var(--border-color)', borderRadius: 0 }}>
        <div className="logo">
          NextGen B2B
        </div>
        <div style={{ marginTop: '2rem', display: 'flex', flexDirection: 'column', gap: '1rem' }}>
          <button 
            onClick={() => setMode('search')}
            style={{ 
              display: 'flex', alignItems: 'center', gap: '0.75rem', padding: '0.75rem', 
              background: mode === 'search' ? 'var(--surface-color-hover)' : 'transparent',
              border: 'none', borderRadius: 'var(--radius-sm)', color: 'white', cursor: 'pointer', textAlign: 'left'
            }}
          >
            <PackageSearch size={20} />
            Marketplace Search
          </button>
          
          <button 
            onClick={() => setMode('onboarding')}
            style={{ 
              display: 'flex', alignItems: 'center', gap: '0.75rem', padding: '0.75rem', 
              background: mode === 'onboarding' ? 'var(--surface-color-hover)' : 'transparent',
              border: 'none', borderRadius: 'var(--radius-sm)', color: 'white', cursor: 'pointer', textAlign: 'left'
            }}
          >
            <UserPlus size={20} />
            Onboarding Chat
          </button>

          <button 
            style={{ 
              display: 'flex', alignItems: 'center', gap: '0.75rem', padding: '0.75rem', 
              background: 'transparent', border: 'none', borderRadius: 'var(--radius-sm)', 
              color: 'var(--text-secondary)', cursor: 'pointer', textAlign: 'left', marginTop: 'auto'
            }}
          >
            <Database size={20} />
            My RFQs
          </button>
        </div>
      </div>

      {/* Main Content */}
      <div className="main-content">
        <header style={{ padding: '1.5rem 2rem', borderBottom: '1px solid var(--border-color)', background: 'var(--bg-color)', zIndex: 10 }}>
          <h2>{mode === 'search' ? 'Marketplace Agent' : 'Supplier/Buyer Onboarding'}</h2>
          <p style={{ color: 'var(--text-secondary)', margin: '0.25rem 0 0 0', fontSize: '0.9rem' }}>
            {mode === 'search' 
              ? 'Find products and suppliers instantly using vector search.'
              : 'Complete your profile through natural conversation.'}
          </p>
        </header>

        {/* Chat Interface */}
        <ChatBox type={mode} />
      </div>
    </div>
  );
}

export default App;
