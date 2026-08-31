import { useState } from 'react';
import { NavLink, useNavigate, useParams } from 'react-router-dom';
import {
  BookOpen, FlaskConical, FolderOpen, LayoutDashboard,
  ChevronLeft, ChevronRight,
  FolderTree, FileQuestion, LogOut,
} from 'lucide-react';

interface Props {
  onLogout: () => void;
}

export function Sidebar({ onLogout }: Props) {
  const navigate = useNavigate();
  const { courseId } = useParams<{ courseId: string }>();
  const [collapsed, setCollapsed] = useState(false);

  const courseBase = courseId ? `/courses/${courseId}` : '/courses';

  const navGroup = [
    {
      label: '课程空间',
      items: [
        { icon: LayoutDashboard, label: '概览', path: courseBase },
        { icon: FolderOpen, label: '资料库', path: `${courseBase}/materials` },
        { icon: FlaskConical, label: '命题框架', path: `${courseBase}/framework` },
        { icon: FolderTree, label: '知识目录', path: `${courseBase}/knowledge` },
      ],
    },
    {
      label: '命题',
      items: [
        { icon: FileQuestion, label: '试卷项目', path: `${courseBase}/exam-projects` },
      ],
    },
  ];

  return (
    <aside
      className="sidebar-island"
      style={{
        width: collapsed ? 'var(--sidebar-collapsed)' : 'var(--sidebar-width)',
      }}
    >
      {/* Logo header */}
      <div style={{
        padding: collapsed ? '14px 10px' : '16px 18px',
        display: 'flex',
        alignItems: 'center',
        justifyContent: collapsed ? 'center' : 'space-between',
        borderBottom: '1px solid rgba(0,0,0,0.06)',
        minHeight: 'var(--topbar-height)',
        gap: '8px',
      }}>
        <button
          onClick={() => navigate('/courses')}
          title="返回课程空间"
          style={{
            display: 'flex', alignItems: 'center', gap: '8px',
            background: 'none', border: 'none', cursor: 'pointer', padding: 0,
          }}
        >
          <div style={{
            width: 34, height: 34, borderRadius: '10px',
            background: 'linear-gradient(135deg, var(--accent), #5856d6)',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            color: 'white', flexShrink: 0,
          }}>
            <BookOpen size={18} />
          </div>
          {!collapsed && (
            <div style={{ textAlign: 'left' }}>
              <div style={{ fontSize: '0.9375rem', fontWeight: 700 }}>智能出卷</div>
              <div style={{ fontSize: '0.6875rem', color: 'var(--text-tertiary)' }}>AI Exam System</div>
            </div>
          )}
        </button>
        {!collapsed && (
          <button
            onClick={() => setCollapsed(true)}
            style={{
              background: 'none', border: 'none', cursor: 'pointer',
              padding: '4px', borderRadius: '6px', color: 'var(--text-tertiary)',
              display: 'flex', flexShrink: 0,
            }}
          >
            <ChevronLeft size={16} />
          </button>
        )}
      </div>

      {collapsed ? (
        /* ═══ Collapsed state: icon strip ═══ */
        <div style={{ flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', padding: '8px 0', gap: '2px' }}>
          <button
            onClick={() => setCollapsed(false)}
            title="展开侧边栏"
            style={{
              width: '40px', height: '40px', borderRadius: '12px',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              background: 'rgba(0,113,227,0.08)',
              border: 'none', cursor: 'pointer', color: '#0071e3',
              marginBottom: '8px',
            }}
          >
            <ChevronRight size={18} />
          </button>

          {navGroup.map((group) => (
            <div key={group.label} style={{ width: '100%', display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '2px', marginBottom: '12px' }}>
              {group.items.map((item) => (
                <NavLink
                  key={item.path}
                  to={item.path}
                  title={item.label}
                  style={({ isActive }) => ({
                    width: '40px', height: '40px', borderRadius: '12px',
                    display: 'flex', alignItems: 'center', justifyContent: 'center',
                    color: isActive ? '#0071e3' : 'var(--text-tertiary)',
                    background: isActive ? 'rgba(0,113,227,0.1)' : 'transparent',
                    textDecoration: 'none',
                    transition: 'all 0.15s ease',
                  })}
                >
                  <item.icon size={18} />
                </NavLink>
              ))}
            </div>
          ))}
        </div>
      ) : (
        /* ═══ Expanded state ═══ */
        <nav style={{ flex: 1, overflowY: 'auto', padding: '4px 10px' }}>
          {navGroup.map((group) => (
            <div key={group.label} style={{ marginBottom: '16px' }}>
              <div style={{
                fontSize: '0.6875rem', fontWeight: 600, color: 'var(--text-tertiary)',
                textTransform: 'uppercase', letterSpacing: '0.05em',
                padding: '8px 10px 4px',
              }}>
                {group.label}
              </div>
              {group.items.map((item) => (
                <NavLink
                  key={item.path}
                  to={item.path}
                  style={({ isActive }) => ({
                    display: 'flex', alignItems: 'center', gap: '10px',
                    padding: '9px 12px', borderRadius: '10px',
                    fontSize: '0.875rem', fontWeight: isActive ? 600 : 400,
                    color: isActive ? '#0071e3' : 'var(--text-secondary)',
                    background: isActive ? 'rgba(0,113,227,0.07)' : 'transparent',
                    textDecoration: 'none',
                    transition: 'all 0.15s ease',
                    marginBottom: '2px',
                  })}
                >
                  <item.icon size={17} style={{ flexShrink: 0 }} />
                  {item.label}
                </NavLink>
              ))}
            </div>
          ))}
        </nav>
      )}

      {/* User section */}
      <div style={{
        padding: collapsed ? '10px' : '12px 14px',
        borderTop: '1px solid rgba(0,0,0,0.06)',
        display: 'flex', alignItems: 'center', justifyContent: collapsed ? 'center' : 'space-between',
        gap: collapsed ? '0' : '10px',
      }}>
        <div style={{
          width: 32, height: 32, borderRadius: '50%',
          background: 'linear-gradient(135deg, #0071e3, #5856d6)',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          color: 'white', fontWeight: 600, fontSize: '0.8125rem', flexShrink: 0,
        }}>
          T
        </div>
        {!collapsed && (
          <>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontSize: '0.8125rem', fontWeight: 600 }}>教师</div>
              <div style={{ fontSize: '0.6875rem', color: 'var(--text-tertiary)' }}>teacher@edu.cn</div>
            </div>
            <button onClick={onLogout} title="退出登录" style={{
              background: 'none', border: 'none', cursor: 'pointer',
              padding: '5px', borderRadius: '7px', color: 'var(--text-tertiary)',
              display: 'flex',
            }}>
              <LogOut size={15} />
            </button>
          </>
        )}
      </div>
    </aside>
  );
}
