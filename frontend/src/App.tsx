import React, { useState, useEffect } from 'react';
import { Routes, Route, Navigate, Link, useLocation } from 'react-router-dom';
import { ChatBox } from './components/ChatBox';
import { Login } from './pages/Login';
import { Signup } from './pages/Signup';
import { Profile } from './pages/Profile';
import { useAuth } from './context/AuthContext';
import './App.css';
import { PackageSearch, UserPlus, UserCircle, Sun, Moon } from 'lucide-react';

const ProtectedRoute = ({ children }: { children: React.ReactNode }) => {
  const { user, isLoading } = useAuth();
  if (isLoading) return <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', height: '100vh' }}>Loading...</div>;
  if (!user) return <Navigate to="/login" />;
  return <>{children}</>;
};

const DashboardLayout = ({ children, mode, setMode }: any) => {
  const location = useLocation();
  const [theme, setTheme] = useState(localStorage.getItem('theme') || 'dark');

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme);
    localStorage.setItem('theme', theme);
  }, [theme]);

  const toggleTheme = () => {
    setTheme(theme === 'dark' ? 'light' : 'dark');
  };

  return (
    <div className="app-container">
      {/* Sidebar */}
      <div className="sidebar glass-panel" style={{ border: 'none', borderRight: '1px solid var(--border-color)', borderRadius: 0 }}>
        <div className="logo">
          NextGen B2B
        </div>
        <div style={{ marginTop: '2rem', display: 'flex', flexDirection: 'column', gap: '1rem', height: '100%' }}>
          <Link 
            to="/"
            onClick={() => setMode('search')}
            style={{ 
              display: 'flex', alignItems: 'center', gap: '0.75rem', padding: '0.75rem', 
              background: location.pathname === '/' && mode === 'search' ? 'var(--surface-color-hover)' : 'transparent',
              border: 'none', borderRadius: 'var(--radius-sm)', color: 'var(--text-primary)', cursor: 'pointer', textAlign: 'left', textDecoration: 'none'
            }}
          >
            <PackageSearch size={20} />
            Marketplace Search
          </Link>
          
          <Link 
            to="/"
            onClick={() => setMode('onboarding')}
            style={{ 
              display: 'flex', alignItems: 'center', gap: '0.75rem', padding: '0.75rem', 
              background: location.pathname === '/' && mode === 'onboarding' ? 'var(--surface-color-hover)' : 'transparent',
              border: 'none', borderRadius: 'var(--radius-sm)', color: 'var(--text-primary)', cursor: 'pointer', textAlign: 'left', textDecoration: 'none'
            }}
          >
            <UserPlus size={20} />
            Onboarding Chat
          </Link>

          <div style={{ marginTop: 'auto', display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
            <button 
              onClick={toggleTheme}
              style={{ 
                display: 'flex', alignItems: 'center', gap: '0.75rem', padding: '0.75rem', 
                background: 'transparent', border: 'none', borderRadius: 'var(--radius-sm)', 
                color: 'var(--text-secondary)', cursor: 'pointer', textAlign: 'left'
              }}
            >
              {theme === 'dark' ? <Sun size={20} /> : <Moon size={20} />}
              {theme === 'dark' ? 'Light Mode' : 'Dark Mode'}
            </button>

            <Link 
              to="/profile"
              style={{ 
                display: 'flex', alignItems: 'center', gap: '0.75rem', padding: '0.75rem', 
                background: location.pathname === '/profile' ? 'var(--surface-color-hover)' : 'transparent', 
                border: 'none', borderRadius: 'var(--radius-sm)', 
                color: 'var(--text-secondary)', cursor: 'pointer', textAlign: 'left', textDecoration: 'none'
              }}
            >
              <UserCircle size={20} />
              Profile
            </Link>
          </div>
        </div>
      </div>

      {/* Main Content */}
      <div className="main-content">
        <header style={{ padding: '1.5rem 2rem', borderBottom: '1px solid var(--border-color)', background: 'var(--bg-color)', zIndex: 10 }}>
          {location.pathname === '/' ? (
            <>
              <h2>{mode === 'search' ? 'Marketplace Agent' : 'Supplier/Buyer Onboarding'}</h2>
              <p style={{ color: 'var(--text-secondary)', margin: '0.25rem 0 0 0', fontSize: '0.9rem' }}>
                {mode === 'search' 
                  ? 'Find products and suppliers instantly using vector search.'
                  : 'Complete your profile through natural conversation.'}
              </p>
            </>
          ) : (
             <h2>Account</h2>
          )}
        </header>

        {children}
      </div>
    </div>
  );
};

function App() {
  const [mode, setMode] = useState<'onboarding' | 'search'>('search');

  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/signup" element={<Signup />} />
      
      <Route path="/" element={
        <ProtectedRoute>
          <DashboardLayout mode={mode} setMode={setMode}>
            <ChatBox type={mode} />
          </DashboardLayout>
        </ProtectedRoute>
      } />
      
      <Route path="/profile" element={
        <ProtectedRoute>
          <DashboardLayout mode={mode} setMode={setMode}>
            <Profile />
          </DashboardLayout>
        </ProtectedRoute>
      } />
    </Routes>
  );
}

export default App;
