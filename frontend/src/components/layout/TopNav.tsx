import { NavLink } from 'react-router-dom';
import {
  BookOpen,
  LayoutDashboard,
  LibraryBig,
  LogOut,
  ShieldCheck,
  Sparkles,
  type LucideIcon,
} from 'lucide-react';
import { useAuthStore } from '@/stores/auth';

interface NavItem {
  to: string;
  label: string;
  icon: LucideIcon;
  end?: boolean;
}

const BASE_NAV: NavItem[] = [
  { to: '/home', label: 'AI 助手', icon: Sparkles, end: true },
  { to: '/courses', label: '课程空间', icon: LayoutDashboard, end: true },
  { to: '/knowledge', label: '资料与知识中心', icon: LibraryBig },
];

/**
 * 顶层导航链接组：/home、/courses、/knowledge 共用同一套入口、顺序与激活态，
 * 管理员追加「后台管理」。首页把它内嵌在自己的页头里复用。
 */
export function TopNavLinks() {
  const role = useAuthStore((s) => s.user?.role);
  const isAdmin = role === 'admin' || role === 'org_admin';
  const items: NavItem[] = isAdmin
    ? [...BASE_NAV, { to: '/admin', label: '后台管理', icon: ShieldCheck }]
    : BASE_NAV;

  return (
    <nav className="top-nav-links" aria-label="主导航">
      {items.map(({ to, label, icon: Icon, end }) => (
        <NavLink
          key={to}
          to={to}
          end={end}
          title={label}
          className={({ isActive }) => `top-nav-link${isActive ? ' is-active' : ''}`}
        >
          <Icon size={15} />
          <span className="top-nav-link-label">{label}</span>
        </NavLink>
      ))}
    </nav>
  );
}

/**
 * 顶层页面统一导航条：品牌 + 主入口 + 账号。
 * 只用于无侧栏的三个顶层页面，课程工作台仍用 Sidebar。
 */
export function TopNav() {
  const user = useAuthStore((s) => s.user);
  const logout = useAuthStore((s) => s.logout);
  const displayName = user?.name || user?.username || '教师';

  return (
    <header className="top-nav">
      <NavLink to="/home" className="top-nav-brand" title="返回 AI 助手首页">
        <span className="top-nav-brand-mark">
          <BookOpen size={17} />
        </span>
        <span className="top-nav-brand-copy">
          <strong>PaperPact</strong>
          <small>教学知识与 AI 命题</small>
        </span>
      </NavLink>

      <TopNavLinks />

      <div className="top-nav-right">
        <span className="top-nav-user" title={user?.username || displayName}>
          {displayName.slice(0, 1)}
        </span>
        <span className="top-nav-user-name">{displayName}</span>
        <button type="button" className="top-nav-action" onClick={logout} title="退出登录">
          <LogOut size={15} />
          <span className="top-nav-action-label">退出登录</span>
        </button>
      </div>
    </header>
  );
}
