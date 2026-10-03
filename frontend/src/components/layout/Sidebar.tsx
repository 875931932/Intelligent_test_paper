import { useEffect, useState } from 'react';
import { NavLink, useParams, useNavigate } from 'react-router-dom';
import {
  BookOpen,
  Bot,
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
import { api } from '@/api/client';

interface Props {
  onLogout: () => void;
}

/* 侧栏色板全部走令牌：bento 实底白面板（禁玻璃态），基底样式在
   .sidebar-island（global.css），本文件只保留宽度与内容配色 */
const TEXT_MAIN = 'var(--text)';
const TEXT_SECONDARY = 'var(--text-secondary)';
const ACCENT = 'var(--accent)';
const ACCENT_BG = 'var(--accent-subtle)';

export function Sidebar({ onLogout }: Props) {
  const { courseId } = useParams<{ courseId: string }>();
  const navigate = useNavigate();
  const [collapsed, setCollapsed] = useState(false);
  const courses = useCourseStore((s) => s.courses);
  const addCourse = useCourseStore((s) => s.addCourse);
  const currentCourse = courses.find((c) => c.id === courseId);

  // 课程名单一来源是 course store（此前只由课程空间页 setCourses 填充），
  // 刷新/直达课程页时 store 为空 → 复用既有 get 接口补拉当前课程进 store；
  // 拿不到则维持「未命名课程」兜底，不开第二条查询路线
  useEffect(() => {
    if (!courseId || courses.some((c) => c.id === courseId)) return;
    let cancelled = false;
    api.courses
      .get(courseId)
      .then((c) => {
        if (!cancelled) addCourse(c);
      })
      .catch(() => {
        // 静默兜底：名称维持「未命名课程」，不影响导航
      });
    return () => {
      cancelled = true;
    };
  }, [courseId, courses, addCourse]);

  const base = courseId ? `/courses/${courseId}` : '/courses';

  // 出卷流水线与试卷查看/审核/导出已合并为同一个「试卷」页面
  const navItems: Array<{ to: string; icon: LucideIcon; label: string }> = [
    { to: base, icon: LayoutDashboard, label: '概览' },
    { to: `${base}/materials`, icon: FolderOpen, label: '资料库' },
    { to: `${base}/framework`, icon: FlaskConical, label: '命题框架' },
    { to: `${base}/knowledge`, icon: FolderTree, label: '知识目录' },
    { to: `${base}/paper`, icon: FileQuestion, label: '试卷' },
    { to: `${base}/assistant`, icon: Bot, label: 'AI 助手' },
  ];

  return (
    <aside
      className={`sidebar-island${collapsed ? ' is-collapsed' : ''}`}
      style={{
        // 宽度走令牌：Layout 主区经 :has(.is-collapsed) 同步让位，收起即重排
        width: collapsed ? 'var(--sidebar-collapsed)' : 'var(--sidebar-width)',
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
          <span style={{
            display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
            width: 30, height: 30, borderRadius: 9, background: 'var(--brand)', color: '#ffffff', flexShrink: 0,
          }}>
            <BookOpen size={17} />
          </span>
          {!collapsed && <span>PaperPact</span>}
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
            transition: 'background 0.2s cubic-bezier(0, 0, 0.2, 1)',
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
              transition: 'background 0.2s cubic-bezier(0, 0, 0.2, 1)',
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
              transition: 'all 0.2s cubic-bezier(0, 0, 0.2, 1)',
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
          borderTop: '1px solid var(--line)',
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
            transition: 'background 0.2s cubic-bezier(0, 0, 0.2, 1)',
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
