import type { ReactNode } from 'react';

interface Props {
  children: ReactNode;
  sidebar: ReactNode;
}

export function Layout({ children, sidebar }: Props) {
  return (
    <div style={{ display: 'flex', minHeight: '100vh' }}>
      {sidebar}
      <main style={{
        flex: 1,
        padding: '16px',
        paddingLeft: 'calc(var(--sidebar-width) + 28px)',
        paddingTop: '16px',
        minHeight: '100vh',
        maxWidth: '1400px',
        margin: '0 auto',
        width: '100%',
      }}>
        <div className="page-enter" style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-lg)' }}>
          {children}
        </div>
      </main>
    </div>
  );
}