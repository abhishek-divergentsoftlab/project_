import React from 'react';
import { useAuth } from '../context/AuthContext';
import { User, Mail, LogOut, MapPin, Phone } from 'lucide-react';

export const Profile = () => {
  const { user, logout } = useAuth();

  if (!user) return null;

  return (
    <div style={{ padding: '2rem', maxWidth: '600px', margin: '0 auto', width: '100%' }}>
      <header style={{ marginBottom: '2rem' }}>
        <h2>Your Profile</h2>
        <p style={{ color: 'var(--text-secondary)' }}>Manage your account settings</p>
      </header>

      <div className="glass-panel" style={{ padding: '2rem' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '1.5rem', marginBottom: '2rem', paddingBottom: '2rem', borderBottom: '1px solid var(--border-color)' }}>
          <div style={{ width: '80px', height: '80px', borderRadius: '50%', background: 'var(--surface-color-hover)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
            <User size={40} color="var(--accent-primary)" />
          </div>
          <div>
            <h3 style={{ fontSize: '1.5rem', marginBottom: '0.25rem' }}>{user.name}</h3>
            <p style={{ color: 'var(--text-secondary)', textTransform: 'capitalize' }}>{user.role}</p>
          </div>
        </div>

        <div style={{ display: 'flex', flexDirection: 'column', gap: '1.5rem' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '1rem' }}>
            <div style={{ padding: '0.75rem', background: 'var(--surface-color)', borderRadius: 'var(--radius-sm)' }}>
              <Mail size={20} color="var(--text-secondary)" />
            </div>
            <div>
              <p style={{ fontSize: '0.85rem', color: 'var(--text-secondary)', marginBottom: '0.25rem' }}>Email Address</p>
              <p style={{ fontWeight: 500 }}>{user.email}</p>
            </div>
          </div>

          <div style={{ display: 'flex', alignItems: 'center', gap: '1rem' }}>
            <div style={{ padding: '0.75rem', background: 'var(--surface-color)', borderRadius: 'var(--radius-sm)' }}>
              <MapPin size={20} color="var(--text-secondary)" />
            </div>
            <div>
              <p style={{ fontSize: '0.85rem', color: 'var(--text-secondary)', marginBottom: '0.25rem' }}>Region / Country</p>
              <p style={{ fontWeight: 500 }}>{user.region}</p>
            </div>
          </div>

          <div style={{ display: 'flex', alignItems: 'center', gap: '1rem' }}>
            <div style={{ padding: '0.75rem', background: 'var(--surface-color)', borderRadius: 'var(--radius-sm)' }}>
              <Phone size={20} color="var(--text-secondary)" />
            </div>
            <div>
              <p style={{ fontSize: '0.85rem', color: 'var(--text-secondary)', marginBottom: '0.25rem' }}>Phone Number</p>
              <p style={{ fontWeight: 500 }}>{user.phone}</p>
            </div>
          </div>
        </div>

        <div style={{ marginTop: '3rem' }}>
          <button 
            onClick={logout}
            style={{ 
              display: 'flex', alignItems: 'center', gap: '0.5rem', padding: '0.75rem 1.5rem', 
              background: 'rgba(239, 68, 68, 0.1)', border: '1px solid rgba(239, 68, 68, 0.2)', 
              borderRadius: 'var(--radius-sm)', color: '#ef4444', cursor: 'pointer',
              fontWeight: 500, transition: 'all 0.2s'
            }}
            onMouseOver={(e) => (e.currentTarget.style.background = 'rgba(239, 68, 68, 0.2)')}
            onMouseOut={(e) => (e.currentTarget.style.background = 'rgba(239, 68, 68, 0.1)')}
          >
            <LogOut size={18} />
            Sign Out
          </button>
        </div>
      </div>
    </div>
  );
};
