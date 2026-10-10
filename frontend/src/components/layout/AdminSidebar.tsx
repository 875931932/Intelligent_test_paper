import { useState } from 'react';
import { NavLink, useNavigate } from 'react-router-dom';
import {
  ShieldCheck,
  LayoutDashboard,
  Building2,
  Users,
  Cpu,
  Activity,
  LogOut,
  ArrowLeft,
  PanelLeftClose,
  PanelLeft,
  type LucideIcon,
} from 'lucide-react';
import { useAuthStore } from '@/stores/auth';

interface Props {
  onLogout: () => void;
}

/* 后台侧栏：复用 .sidebar-island 体系（实底白面板），与业务端同布局机制。
   菜单按角色收敛：模型管理仅平台管理员可见；单位管理对 org_admin 只读。 */
const TEXT_MAIN = 'var(--text)';
const TEXT_SECONDARY = 'var(--text-secondary)';
const ACCENT = 'var(--accent)';
const ACCENT_BG = 'var(--accent-subtle)';

export function AdminSidebar({ onLogout }: Props) {
  const navigate = useNavigate();
  const [collapsed, setCollapsed] = useState(false);
  const role = useAuthStore((s) => s.user?.role);
  const isPlatformAdmin = role === 'admin';

  const navItems: Array<{ to: string; icon: LucideIcon; label: string; end?: boolean }> = [
    { to: '/admin', icon: LayoutDashboard, label: '概览', end: true },
    { to: '/admin/organizations', icon: Building2, label: '单位管理' },
    { to: '/admin/users', icon: Users, label: '账号管理' },
    ...(isPlatformAdmin ? [{ to: '/admin/llm', icon: Cpu, label: '模型管理' }] : []),
    { to: '/admin/monitoring', icon: Activity, label: '调用监控' },
  ];

  return (
    <aside
      className={`sidebar-island${collapsed ? ' is-collapsed' : ''}`}
      style={{ width: collapsed ? 'var(--sidebar-collapsed)' : 'var(--sidebar-width)' }}
    >
      {/* Header */}
      <div
        style={{
          height: 60,
          display: 'flex',
          alignItems: 'center',
          justifyContent: collapsed ? 'center' : 'space-between',
          padding: collapsed ? '0 10px' : '0 14px 0 16px',
          flexShrink: 0,
        }}
      >
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            color: TEXT_MAIN,
            fontWeight: 600,
            fontSize: 16,
            whiteSpace: 'nowrap',
          }}
        >
          <span
            style={{
              display: 'inline-flex',
              alignItems: 'center',
              justifyContent: 'center',
              width: 30,
              height: 30,
              borderRadius: 9,
              background: 'var(--accent)',
              color: '#ffffff',
              flexShrink: 0,
            }}
          >
            <ShieldCheck size={17} />
          </span>
          {!collapsed && <span>后台管理</span>}
        </div>

        <button
          onClick={() => setCollapsed((v) => !v)}
          title={collapsed ? '展开' : '收起'}
          style={{
            width: 28,
            height: 28,
            border: 'none',
            borderRadius: 'var(--radius-sm)',
            background: 'transparent',
            cursor: 'pointer',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            color: TEXT_SECONDARY,
            transition: 'background 0.2s var(--ease-snappy)',
          }}
          onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--fill)')}
          onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
        >
          {collapsed ? <PanelLeft size={18} /> : <PanelLeftClose size={18} />}
        </button>
      </div>

      {/* Back to business side */}
      <div style={{ padding: '10px 10px 0', flexShrink: 0 }}>
        <button
          onClick={() => navigate('/courses')}
          title="返回业务端"
          style={{
            width: '100%',
            display: 'flex',
            alignItems: 'center',
            gap: collapsed ? 0 : 10,
            justifyContent: collapsed ? 'center' : 'flex-start',
            padding: collapsed ? 9 : '9px 11px',
            border: 'none',
            borderRadius: 12,
            background: ACCENT_BG,
            cursor: 'pointer',
            color: ACCENT,
            transition: 'background 0.2s var(--ease-snappy)',
          }}
          onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--accent-soft)')}
          onMouseLeave={(e) => (e.currentTarget.style.background = ACCENT_BG)}
        >
          <ArrowLeft size={18} />
          {!collapsed && <span style={{ fontSize: 13, fontWeight: 600 }}>返回业务端</span>}
        </button>
      </div>

      {/* Navigation */}
      <nav style={{ flex: 1, padding: '10px 8px', overflowY: 'auto' }}>
        {navItems.map(({ to, icon: Icon, label, end }) => (
          <NavLink
            key={to}
            to={to}
            end={end}
            style={({ isActive }) => ({
              display: 'flex',
              alignItems: 'center',
              gap: collapsed ? 0 : 11,
              justifyContent: collapsed ? 'center' : 'flex-start',
              padding: collapsed ? '10px 0' : '10px 11px',
              borderRadius: 12,
              marginBottom: 4,
              color: isActive ? ACCENT : TEXT_SECONDARY,
              background: isActive ? ACCENT_BG : 'transparent',
              textDecoration: 'none',
              fontSize: 14,
              fontWeight: isActive ? 600 : 500,
              transition: 'all 0.2s var(--ease-snappy)',
            })}
          >
            <Icon size={18} />
            {!collapsed && <span>{label}</span>}
          </NavLink>
        ))}
      </nav>

      {/* Logout */}
      <div style={{ padding: '10px 8px', borderTop: '1px solid var(--line)', flexShrink: 0 }}>
        <button
          onClick={onLogout}
          style={{
            width: '100%',
            display: 'flex',
            alignItems: 'center',
            gap: collapsed ? 0 : 11,
            justifyContent: collapsed ? 'center' : 'flex-start',
            padding: collapsed ? '10px 0' : '10px 11px',
            borderRadius: 12,
            border: 'none',
            background: 'transparent',
            color: TEXT_SECONDARY,
            cursor: 'pointer',
            fontSize: 14,
            fontWeight: 500,
            transition: 'background 0.2s var(--ease-snappy)',
          }}
          onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--fill)')}
          onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
        >
          <LogOut size={18} />
          {!collapsed && <span>退出登录</span>}
        </button>
      </div>
    </aside>
  );
}
