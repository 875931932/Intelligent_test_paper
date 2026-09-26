import type { ReactNode } from 'react';

interface Props {
  children: ReactNode;
  sidebar: ReactNode;
}

export function Layout({ children, sidebar }: Props) {
  return (
    <div style={{ display: 'flex', minHeight: '100vh', background: 'var(--page-bg)' }}>
      {sidebar}
      <main style={{
        flex: 1,
        // 侧栏让位与内容宽度全部走 token，改侧栏宽度时布局自动跟随
        padding: '24px 32px 24px var(--sidebar-width)',
        minHeight: '100vh',
        maxWidth: 'var(--content-max-width)',
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