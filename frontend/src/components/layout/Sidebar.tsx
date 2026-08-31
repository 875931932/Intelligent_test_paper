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
  ChevronLeft,
  ChevronRight,
} from 'lucide-react';
import { useState } from 'react';
import { useCourseStore } from '@/stores/course';

interface Props {
  onLogout: () => void;
}

export function Sidebar({ onLogout }: Props) {
  const { courseId } = useParams<{ courseId: string }>();
  const navigate = useNavigate();
  const [collapsed, setCollapsed] = useState(false);
  const courses = useCourseStore((s) => s.courses);
  const currentCourse = courses.find((c) => c.id === courseId);

  const base = courseId ? `/courses/${courseId}` : '/courses';

  const nav = [
    { to: base, icon: LayoutDashboard, label: '概览' },
    { to: `${base}/materials`, icon: FolderOpen, label: '资料库' },
    { to: `${base}/framework`, icon: FlaskConical, label: '命题框架' },
    { to: `${base}/knowledge`, icon: FolderTree, label: '知识目录' },
    { to: `${base}/exam-projects`, icon: FileQuestion, label: '试卷项目' },
  ];

  return (
    <aside
      style={{
        width: collapsed ? 72 : 220,
        height: '100vh',
        position: 'fixed',
        left: 0,
        top: 0,
        display: 'flex',
        flexDirection: 'column',
        background: '#fff',
        borderRight: '1px solid #e8e8e8',
        transition: 'width 200ms ease',
        zIndex: 100,
      }}
    >
      {/* Header */}
      <div
        style={{
          height: 64,
          display: 'flex',
          alignItems: 'center',
          justifyContent: collapsed ? 'center' : 'space-between',
          padding: collapsed ? '0 12px' : '0 16px',
          borderBottom: '1px solid #e8e8e8',
        }}
      >
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            color: '#1a1a1a',
            fontWeight: 700,
            fontSize: 16,
          }}
        >
          <BookOpen size={22} />
          {!collapsed && <span>智卷</span>}
        </div>
        <button
          onClick={() => setCollapsed((v) => !v)}
          style={{
            width: 28,
            height: 28,
            border: '1px solid #e8e8e8',
            borderRadius: 6,
            background: '#fff',
            cursor: 'pointer',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            color: '#666',
          }}
          title={collapsed ? '展开' : '收起'}
        >
          {collapsed ? <ChevronRight size={14} /> : <ChevronLeft size={14} />}
        </button>
      </div>

      {/* Course switcher */}
      {courseId && (
        <div
          style={{
            padding: '12px 12px 0',
            borderBottom: '1px solid #e8e8e8',
            paddingBottom: 12,
          }}
        >
          <button
            onClick={() => navigate('/courses')}
            style={{
              width: '100%',
              display: 'flex',
              alignItems: 'center',
              gap: 8,
              padding: collapsed ? '8px 0' : '8px 10px',
              border: '1px solid #e8e8e8',
              borderRadius: 8,
              background: '#fafafa',
              cursor: 'pointer',
              justifyContent: collapsed ? 'center' : 'flex-start',
            }}
            title="返回课程空间"
          >
            <ArrowLeft size={16} color="#666" />
            {!collapsed && (
              <div style={{ overflow: 'hidden', textAlign: 'left' }}>
                <div
                  style={{
                    fontSize: 11,
                    color: '#999',
                    lineHeight: 1.2,
                  }}
                >
                  当前课程
                </div>
                <div
                  style={{
                    fontSize: 13,
                    fontWeight: 600,
                    color: '#1a1a1a',
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

      {/* Nav */}
      <nav style={{ flex: 1, padding: '12px 10px', overflowY: 'auto' }}>
        {nav.map(({ to, icon: Icon, label }) => (
          <NavLink
            key={to}
            to={to}
            end={to === base}
            style={({ isActive }) => ({
              display: 'flex',
              alignItems: 'center',
              gap: 12,
              padding: collapsed ? '10px 0' : '10px 12px',
              borderRadius: 8,
              marginBottom: 4,
              color: isActive ? '#1677ff' : '#595959',
              background: isActive ? '#f0f5ff' : 'transparent',
              textDecoration: 'none',
              fontSize: 14,
              fontWeight: isActive ? 600 : 500,
              justifyContent: collapsed ? 'center' : 'flex-start',
              transition: 'all 150ms ease',
            })}
          >
            <Icon size={18} />
            {!collapsed && <span>{label}</span>}
          </NavLink>
        ))}
      </nav>

      {/* Logout */}
      <div style={{ padding: '12px 10px', borderTop: '1px solid #e8e8e8' }}>
        <button
          onClick={onLogout}
          style={{
            width: '100%',
            display: 'flex',
            alignItems: 'center',
            gap: 12,
            padding: collapsed ? '10px 0' : '10px 12px',
            borderRadius: 8,
            border: 'none',
            background: 'transparent',
            color: '#595959',
            cursor: 'pointer',
            fontSize: 14,
            fontWeight: 500,
            justifyContent: collapsed ? 'center' : 'flex-start',
          }}
        >
          <LogOut size={18} />
          {!collapsed && <span>退出登录</span>}
        </button>
      </div>
    </aside>
  );
}
