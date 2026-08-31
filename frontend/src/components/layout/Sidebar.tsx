import { useState, type CSSProperties } from 'react';
import { NavLink, useNavigate, useParams } from 'react-router-dom';
import {
  BookOpen, FlaskConical, FolderOpen, LayoutDashboard,
  ChevronLeft, ChevronRight, ArrowLeft,
  FolderTree, FileQuestion, LogOut,
} from 'lucide-react';
import { useCourseStore } from '@/stores/course';

interface Props {
  onLogout: () => void;
}

// ── 共享样式常量（去重） ──
const HAIRLINE = '1px solid rgba(0, 0, 0, 0.06)';
const flexCenter: CSSProperties = { display: 'flex', alignItems: 'center', justifyContent: 'center' };
const flexBetween: CSSProperties = { display: 'flex', alignItems: 'center', justifyContent: 'space-between' };
const plainBtn: CSSProperties = { background: 'none', border: 'none', cursor: 'pointer', padding: 0 };
const iconBtn40: CSSProperties = { width: 40, height: 40, borderRadius: 12, ...flexCenter };

export function Sidebar({ onLogout }: Props) {
  const navigate = useNavigate();
  const { courseId } = useParams<{ courseId: string }>();
  const [collapsed, setCollapsed] = useState(false);
  const courses = useCourseStore((s) => s.courses);
  const currentCourse = courses.find((c) => c.id === courseId);

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
      style={{ width: collapsed ? 'var(--sidebar-collapsed)' : 'var(--sidebar-width)' }}
    >
      {/* ═══ Logo header ═══ */}
      <div style={{
        padding: collapsed ? '14px 10px' : '16px 18px',
        ...(collapsed ? flexCenter : flexBetween),
        borderBottom: HAIRLINE,
        minHeight: 'var(--topbar-height)',
        gap: '8px',
      }}>
        <button onClick={() => navigate('/courses')} title="返回课程空间" style={{ ...plainBtn, display: 'flex', alignItems: 'center', gap: '8px' }}>
          <div style={{
            width: 34, height: 34, borderRadius: 10,
            background: 'linear-gradient(135deg, var(--accent), #5856d6)',
            color: 'white', flexShrink: 0, ...flexCenter,
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
          <button onClick={() => setCollapsed(true)} title="收起侧边栏" style={{ ...plainBtn, padding: '4px', borderRadius: 6, color: 'var(--text-tertiary)', display: 'flex', flexShrink: 0 }}>
            <ChevronLeft size={16} />
          </button>
        )}
      </div>

      {collapsed ? (
        /* ═══ 折叠态：图标条 ═══ */
        <div style={{ flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', padding: '8px 0', gap: '2px' }}>
          <button onClick={() => setCollapsed(false)} title="展开侧边栏" style={{ ...iconBtn40, background: 'rgba(0,113,227,0.08)', color: '#0071e3', marginBottom: 8 }}>
            <ChevronRight size={18} />
          </button>

          <button onClick={() => navigate('/courses')} title="返回课程空间" style={{ ...iconBtn40, color: 'var(--text-secondary)', marginBottom: 8 }}>
            <ArrowLeft size={18} />
          </button>

          {navGroup.map((group) => (
            <div key={group.label} style={{ width: '100%', display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '2px', marginBottom: 12 }}>
              {group.items.map((item) => (
                <NavLink
                  key={item.path}
                  to={item.path}
                  title={item.label}
                  style={({ isActive }) => ({
                    ...iconBtn40,
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
        /* ═══ 展开态 ═══ */
        <>
          {/* 当前课程 + 返回入口 */}
          <div style={{ padding: '12px 14px', borderBottom: HAIRLINE, display: 'flex', flexDirection: 'column', gap: 10 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 9 }}>
              <div style={{ width: 30, height: 30, borderRadius: 9, background: 'var(--accent-subtle)', color: 'var(--accent)', flexShrink: 0, ...flexCenter }}>
                <BookOpen size={15} />
              </div>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: '0.6875rem', color: 'var(--text-tertiary)' }}>当前课程</div>
                <div style={{ fontSize: '0.8125rem', fontWeight: 600, color: 'var(--text-primary)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                  {currentCourse?.name || '未选择课程'}
                </div>
              </div>
            </div>
            <button
              onClick={() => navigate('/courses')}
              style={{
                display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 6,
                width: '100%', padding: '8px 10px', borderRadius: 10,
                background: 'rgba(0,0,0,0.04)', border: HAIRLINE, cursor: 'pointer',
                fontSize: '0.8125rem', fontWeight: 500, color: 'var(--text-secondary)',
                transition: 'all 0.15s ease',
              }}
              onMouseEnter={(e) => { e.currentTarget.style.background = 'rgba(0,113,227,0.08)'; e.currentTarget.style.color = '#0071e3'; }}
              onMouseLeave={(e) => { e.currentTarget.style.background = 'rgba(0,0,0,0.04)'; e.currentTarget.style.color = 'var(--text-secondary)'; }}
            >
              <ArrowLeft size={15} />
              返回课程空间
            </button>
          </div>

          <nav style={{ flex: 1, overflowY: 'auto', padding: '4px 10px' }}>
            {navGroup.map((group) => (
              <div key={group.label} style={{ marginBottom: 16 }}>
                <div style={{ fontSize: '0.6875rem', fontWeight: 600, color: 'var(--text-tertiary)', textTransform: 'uppercase', letterSpacing: '0.05em', padding: '8px 10px 4px' }}>
                  {group.label}
                </div>
                {group.items.map((item) => (
                  <NavLink
                    key={item.path}
                    to={item.path}
                    style={({ isActive }) => ({
                      display: 'flex', alignItems: 'center', gap: 10,
                      padding: '9px 12px', borderRadius: 10,
                      fontSize: '0.875rem', fontWeight: isActive ? 600 : 400,
                      color: isActive ? '#0071e3' : 'var(--text-secondary)',
                      background: isActive ? 'rgba(0,113,227,0.07)' : 'transparent',
                      textDecoration: 'none',
                      transition: 'all 0.15s ease',
                      marginBottom: 2,
                    })}
                  >
                    <item.icon size={17} style={{ flexShrink: 0 }} />
                    {item.label}
                  </NavLink>
                ))}
              </div>
            ))}
          </nav>
        </>
      )}

      {/* ═══ 用户信息 ═══ */}
      <div style={{
        padding: collapsed ? 10 : '12px 14px',
        borderTop: HAIRLINE,
        gap: collapsed ? 0 : 10,
        ...(collapsed ? flexCenter : flexBetween),
      }}>
        <div style={{ width: 32, height: 32, borderRadius: '50%', background: 'linear-gradient(135deg, #0071e3, #5856d6)', color: 'white', fontWeight: 600, fontSize: '0.8125rem', flexShrink: 0, ...flexCenter }}>
          T
        </div>
        {!collapsed && (
          <>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontSize: '0.8125rem', fontWeight: 600 }}>教师</div>
              <div style={{ fontSize: '0.6875rem', color: 'var(--text-tertiary)' }}>teacher@edu.cn</div>
            </div>
            <button onClick={onLogout} title="退出登录" style={{ ...plainBtn, padding: 5, borderRadius: 7, color: 'var(--text-tertiary)', display: 'flex' }}>
              <LogOut size={15} />
            </button>
          </>
        )}
      </div>
    </aside>
  );
}
