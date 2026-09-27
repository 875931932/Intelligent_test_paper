import { useState } from 'react';
import { NavLink, useParams, useNavigate } from 'react-router-dom';
import {
  BookOpen,
  LayoutDashboard,
  FolderOpen,
  FlaskConical,
  FolderTree,
  FileQuestion,
  LogOut,
  ArrowLeft,
  PanelLeftClose,
  PanelLeft,
  type LucideIcon,
} from 'lucide-react';
import { useCourseStore } from '@/stores/course';

interface Props {
  onLogout: () => void;
}

/* 侧栏色板全部走令牌：曾经的 Tailwind 系（#2563eb/#1f2937/#6b7280）
   与主站令牌（学海蓝体系）两套并存，颜色统一后一律引用设计令牌 */
const ISLAND_BG = 'var(--sidebar-glass)';
const ISLAND_SHADOW = 'var(--shadow-2)';
const HAIRLINE = '1px solid var(--line)';
const RADIUS = 20;
const TEXT_MAIN = 'var(--text)';
const TEXT_SECONDARY = 'var(--text-secondary)';
const ACCENT = 'var(--accent)';
const ACCENT_BG = 'var(--accent-subtle)';

export function Sidebar({ onLogout }: Props) {
  const { courseId } = useParams<{ courseId: string }>();
  const navigate = useNavigate();
  const [collapsed, setCollapsed] = useState(false);
  const courses = useCourseStore((s) => s.courses);
  const currentCourse = courses.find((c) => c.id === courseId);

  const base = courseId ? `/courses/${courseId}` : '/courses';

  // 出卷流水线与试卷查看/审核/导出已合并为同一个「试卷」页面
  const navItems: Array<{ to: string; icon: LucideIcon; label: string }> = [
    { to: base, icon: LayoutDashboard, label: '概览' },
    { to: `${base}/materials`, icon: FolderOpen, label: '资料库' },
    { to: `${base}/framework`, icon: FlaskConical, label: '命题框架' },
    { to: `${base}/knowledge`, icon: FolderTree, label: '知识目录' },
    { to: `${base}/paper`, icon: FileQuestion, label: '试卷' },
  ];

  return (
    <aside
      style={{
        position: 'fixed',
        left: 14,
        top: 14,
        bottom: 14,
        width: collapsed ? 68 : 220,
        display: 'flex',
        flexDirection: 'column',
        background: ISLAND_BG,
        border: HAIRLINE,
        borderRadius: RADIUS,
        boxShadow: ISLAND_SHADOW,
        backdropFilter: 'blur(12px)',
        WebkitBackdropFilter: 'blur(12px)',
        zIndex: 100,
        overflow: 'hidden',
        transition: 'width 220ms cubic-bezier(0.4, 0, 0.2, 1)',
      }}
    >
      {/* Header */}
      <div
        style={{
          height: 60,
          display: 'flex',
          alignItems: 'center',
          justifyContent: collapsed ? 'center' : 'space-between',
          padding: collapsed ? '0 10px' : '0 14px 0 16px',
          borderBottom: HAIRLINE,
          flexShrink: 0,
        }}
      >
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            color: TEXT_MAIN,
            fontWeight: 700,
            fontSize: 16,
            whiteSpace: 'nowrap',
          }}
        >
          <BookOpen size={20} color={ACCENT} />
          {!collapsed && <span>智卷</span>}
        </div>

        <button
          onClick={() => setCollapsed((v) => !v)}
          title={collapsed ? '展开' : '收起'}
          style={{
            width: 28,
            height: 28,
            border: 'none',
            borderRadius: 8,
            background: 'transparent',
            cursor: 'pointer',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            color: TEXT_SECONDARY,
            transition: 'background 150ms ease',
          }}
          onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--fill)')}
          onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
        >
          {collapsed ? <PanelLeft size={18} /> : <PanelLeftClose size={18} />}
        </button>
      </div>

      {/* Back to course space */}
      {courseId && (
        <div
          style={{
            padding: '10px 10px 0',
            borderBottom: HAIRLINE,
            flexShrink: 0,
          }}
        >
          <button
            onClick={() => navigate('/courses')}
            title="返回课程空间"
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
              transition: 'background 150ms ease',
            }}
            onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--accent-soft)')}
            onMouseLeave={(e) => (e.currentTarget.style.background = ACCENT_BG)}
          >
            <ArrowLeft size={18} />
            {!collapsed && (
              <div style={{ overflow: 'hidden', textAlign: 'left', minWidth: 0 }}>
                <div style={{ fontSize: 11, color: 'var(--accent-text)', lineHeight: 1.2 }}>
                  返回课程空间
                </div>
                <div
                  style={{
                    fontSize: 12,
                    fontWeight: 600,
                    color: ACCENT,
                    lineHeight: 1.4,
                    whiteSpace: 'nowrap',
                    textOverflow: 'ellipsis',
                    overflow: 'hidden',
                  }}
                >
                  {currentCourse?.name || '未命名课程'}
                </div>
              </div>
            )}
          </button>
        </div>
      )}

      {/* Navigation */}
      <nav style={{ flex: 1, padding: '10px 8px', overflowY: 'auto' }}>
        {navItems.map(({ to, icon: Icon, label }) => (

          <NavLink
            key={to}
            to={to}
            end={to === base}
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
              transition: 'all 150ms ease',
            })}
          >
            <Icon size={18} />
            {!collapsed && <span>{label}</span>}
          </NavLink>
        ))}
      </nav>

      {/* Logout */}
      <div
        style={{
          padding: '10px 8px',
          borderTop: HAIRLINE,
          flexShrink: 0,
        }}
      >
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
            transition: 'background 150ms ease',
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
