import { Link, useLocation } from 'react-router-dom';
import { ChevronRight, Home } from 'lucide-react';

interface Breadcrumb {
  label: string;
  path?: string;
}

const routeLabels: Record<string, string> = {
  'dashboard': '概览',
  'materials': '资料库',
  'framework': '命题框架',
  'knowledge': '知识目录',
  'exam-projects': '试卷项目',
  'projects': '试卷项目',
};

export function Breadcrumbs() {
  const location = useLocation();
  const segments = location.pathname.split('/').filter(Boolean);

  // Remove 'courses' prefix and course id from breadcrumb display
  const breadcrumbs: Breadcrumb[] = [{ label: '首页', path: '/dashboard' }];

  if (segments[0] === 'courses' && segments[1]) {
    // Don't show course id in breadcrumb, jump to section
    if (segments[2]) {
      const label = routeLabels[segments[2]] ?? segments[2];
      breadcrumbs.push({ label, path: location.pathname });
    }
  }

  return (
    <div style={{
      height: '48px',
      display: 'flex',
      alignItems: 'center',
      gap: '8px',
      padding: '0 32px',
      borderBottom: '1px solid rgba(0,0,0,0.04)',
      background: 'rgba(255,255,255,0.6)',
      backdropFilter: 'blur(12px)',
      fontSize: '0.8125rem',
      color: 'var(--text-secondary)',
    }}>
      <Home size={14} />
      {breadcrumbs.map((crumb, i) => (
        <span key={i} style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          {i > 0 && <ChevronRight size={12} />}
          {crumb.path && i < breadcrumbs.length - 1 ? (
            <Link to={crumb.path} style={{ color: 'var(--accent-text)', textDecoration: 'none', fontWeight: 500 }}>
              {crumb.label}
            </Link>
          ) : (
            <span style={{ color: 'var(--text)', fontWeight: 500 }}>{crumb.label}</span>
          )}
        </span>
      ))}
    </div>
  );
}
