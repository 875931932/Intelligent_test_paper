import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { BookOpen, Plus, ChevronRight, LogOut } from 'lucide-react';
import { useCourseStore } from '@/stores/course';
import { useAuthStore } from '@/stores/auth';
import { useToastStore } from '@/stores/toast';
import { api } from '@/api/client';
import { Modal, Input } from '@/components/ui';

export default function CourseSpacePage() {
  const navigate = useNavigate();
  const courses = useCourseStore((s) => s.courses);
  const setActiveCourse = useCourseStore((s) => s.setActiveCourse);
  const logout = useAuthStore((s) => s.logout);
  const addToast = useToastStore((s) => s.addToast);

  const [createOpen, setCreateOpen] = useState(false);
  const [newName, setNewName] = useState('');
  const [creating, setCreating] = useState(false);

  const handleSelectCourse = (courseId: string) => {
    setActiveCourse(courseId);
    navigate('/courses/' + courseId);
  };

  const handleCreate = async () => {
    const name = newName.trim();
    if (!name) {
      addToast('请输入课程名称', 'info');
      return;
    }
    setCreating(true);
    try {
      const course = await api.courses.create({ name });
      useCourseStore.getState().addCourse(course);
      setActiveCourse(course.id);
      addToast('课程创建成功', 'success');
      setCreateOpen(false);
      setNewName('');
      navigate('/courses/' + course.id);
    } catch {
      addToast('创建课程失败', 'error');
    } finally {
      setCreating(false);
    }
  };

  return (
    <div style={{ minHeight: '100vh', display: 'flex', flexDirection: 'column' }}>
      {/* 顶部栏 */}
      <header style={{
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        padding: '16px 32px', borderBottom: '1px solid rgba(0,0,0,0.06)',
        backdropFilter: 'var(--glass-blur)', WebkitBackdropFilter: 'var(--glass-blur)',
        position: 'sticky', top: 0, zIndex: 10, background: 'var(--sidebar-glass)',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
          <div style={{
            width: 38, height: 38, borderRadius: '11px',
            background: 'linear-gradient(135deg, var(--accent), #5856d6)',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            color: 'white',
          }}>
            <BookOpen size={20} />
          </div>
          <div>
            <div style={{ fontSize: '1rem', fontWeight: 700, lineHeight: 1.2 }}>智能出卷</div>
            <div style={{ fontSize: '0.6875rem', color: 'var(--text-tertiary)' }}>AI Exam System</div>
          </div>
        </div>
        <button onClick={logout} title="退出登录" style={{
          display: 'flex', alignItems: 'center', gap: '6px',
          background: 'none', border: 'none', cursor: 'pointer',
          padding: '8px 12px', borderRadius: '10px',
          color: 'var(--text-secondary)', fontSize: '0.8125rem',
        }}
          onMouseEnter={(e) => { e.currentTarget.style.background = 'rgba(0,0,0,0.05)'; }}
          onMouseLeave={(e) => { e.currentTarget.style.background = 'none'; }}
        >
          <LogOut size={15} />
          退出登录
        </button>
      </header>

      {/* 内容区 */}
      <main style={{
        flex: 1, width: '100%', maxWidth: '1200px', margin: '0 auto',
        padding: '48px 32px',
      }}>
        <div className="page-enter">
          <div style={{ marginBottom: '32px' }}>
            <h1 style={{ fontSize: '2rem', fontWeight: 700, letterSpacing: '-0.03em', marginBottom: '8px' }}>课程空间</h1>
            <p style={{ fontSize: '0.9375rem', color: 'var(--text-secondary)' }}>选择一门课程开始命题工作，或创建新课程</p>
          </div>

          <div style={{
            display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(260px, 1fr))', gap: '20px',
          }}>
            {courses.map((course) => (
              <div
                key={course.id}
                role="button"
                tabIndex={0}
                onClick={() => handleSelectCourse(course.id)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    handleSelectCourse(course.id);
                  }
                }}
                style={{ cursor: 'pointer' }}
                className="stagger-item"
              >
                <div className="glass-card card-hover" style={{ padding: '24px', height: '100%', display: 'flex', flexDirection: 'column' }}>
                  <div style={{
                    width: 46, height: 46, borderRadius: '14px',
                    background: 'var(--accent-subtle)', color: 'var(--accent)',
                    display: 'flex', alignItems: 'center', justifyContent: 'center',
                    marginBottom: '16px',
                  }}>
                    <BookOpen size={22} />
                  </div>
                  <h3 style={{ fontSize: '1.125rem', fontWeight: 600, marginBottom: '6px' }}>{course.name}</h3>
                  <p style={{
                    fontSize: '0.8125rem', color: 'var(--text-secondary)', flex: 1,
                    display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical', overflow: 'hidden',
                  }}>
                    {course.description || '暂无课程描述'}
                  </p>
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'flex-end', marginTop: '12px', color: 'var(--accent)', fontSize: '0.8125rem', fontWeight: 500 }}>
                    进入课程 <ChevronRight size={16} />
                  </div>
                </div>
              </div>
            ))}

            {/* 新建课程卡片 */}
            <button
              onClick={() => { setNewName(''); setCreateOpen(true); }}
              style={{
                minHeight: '180px', borderRadius: 'var(--radius-lg)',
                border: '1.5px dashed rgba(0,113,227,0.3)', background: 'rgba(0,113,227,0.03)',
                cursor: 'pointer', display: 'flex', flexDirection: 'column',
                alignItems: 'center', justifyContent: 'center', gap: '10px',
                color: 'var(--accent)', fontSize: '0.875rem', fontWeight: 500,
                transition: 'all 0.2s ease',
              }}
              onMouseEnter={(e) => { e.currentTarget.style.background = 'rgba(0,113,227,0.07)'; }}
              onMouseLeave={(e) => { e.currentTarget.style.background = 'rgba(0,113,227,0.03)'; }}
            >
              <div style={{
                width: 44, height: 44, borderRadius: '14px',
                background: 'var(--accent-subtle)',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
              }}>
                <Plus size={22} />
              </div>
              新建课程
            </button>
          </div>
        </div>
      </main>

      {/* 新建课程弹窗 */}
      <Modal
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        title="新建课程"
        confirmLabel="创建"
        loading={creating}
        onConfirm={handleCreate}
      >
        <Input
          label="课程名称"
          placeholder="请输入课程名称"
          value={newName}
          onChange={(e) => setNewName(e.target.value)}
          autoFocus
        />
      </Modal>
    </div>
  );
}
