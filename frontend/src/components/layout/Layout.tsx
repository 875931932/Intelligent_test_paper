import type { ReactNode } from 'react';

interface Props {
  children: ReactNode;
  sidebar: ReactNode;
}

export function Layout({ children, sidebar }: Props) {
  return (
    <div className="app-shell" style={{ display: 'flex', minHeight: '100vh', background: 'var(--page-bg)' }}>
      {sidebar}
      {/* 主区让位宽度走 .app-main（global.css）：侧栏收起时经 :has 重排，
          内容区变大；内联样式会被类规则的同特异度后者覆盖问题在这里不存在——
          间距全部由类负责，行内只保留结构无关的展示属性 */}
      <main className="app-main">
        <div className="page-enter" style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-lg)' }}>
          {children}
        </div>
      </main>
    </div>
  );
}