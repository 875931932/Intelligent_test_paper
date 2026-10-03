import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { BookOpen, Plus, ChevronRight, LogOut } from 'lucide-react';
import { useCourseStore, type Course } from '@/stores/course';
import { useAuthStore } from '@/stores/auth';
import { useToastStore } from '@/stores/toast';
import { api } from '@/api/client';
import { Modal, Input, Select } from '@/components/ui';
import type { CourseCategoryInfo } from '@/types/api';

export default function CourseSpacePage() {
  const navigate = useNavigate();
  const courses = useCourseStore((s) => s.courses);
  const setCourses = useCourseStore((s) => s.setCourses);
  const setActiveCourse = useCourseStore((s) => s.setActiveCourse);
  const removeCourse = useCourseStore((s) => s.removeCourse);
  const logout = useAuthStore((s) => s.logout);
  const addToast = useToastStore((s) => s.addToast);

  const [loading, setLoading] = useState(true);
  const [createOpen, setCreateOpen] = useState(false);
  const [newName, setNewName] = useState('');
  const [creating, setCreating] = useState(false);
  // 课程类别：清单来自后端类别档案（单一来源）；空 = 尚未加载/加载失败，
  // 此时不渲染下拉且创建请求不带 category（后端落默认 general）
  const [categories, setCategories] = useState<CourseCategoryInfo[]>([]);
  const [newCategory, setNewCategory] = useState('');
  // 长按删除：确认弹窗目标与提交中标志
  const [deleteTarget, setDeleteTarget] = useState<Course | null>(null);
  const [deleting, setDeleting] = useState(false);

  // 打开创建弹窗时懒加载类别清单（失败不阻塞创建，走默认类别）
  useEffect(() => {
    if (!createOpen) return;
    let cancelled = false;
    (async () => {
      try {
        const list = await api.courses.categories();
        if (!cancelled && list.length) {
          setCategories(list);
          setNewCategory((cur) => (cur && list.some((c) => c.key === cur) ? cur : list[0].key));
        }
      } catch {
        // 类别清单不可用时静默降级：仅用名称创建
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [createOpen]);

  // 挂载时从后端加载当前登录用户的课程（登录/刷新后也能看到历史课程）
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const list = await api.courses.list();
        if (!cancelled) setCourses(list);
      } catch {
        if (!cancelled) addToast('课程加载失败，请重试', 'error');
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

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
      const course = await api.courses.create({
        name,
        ...(newCategory ? { category: newCategory } : {}),
      });
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

  // ── 长按删除课程卡 ──
  const pressTimer = useRef<number | null>(null);
  const pressOrigin = useRef<{ x: number; y: number } | null>(null);
  const longPressFired = useRef(false);

  const cancelPress = () => {
    if (pressTimer.current !== null) {
      window.clearTimeout(pressTimer.current);
      pressTimer.current = null;
    }
    pressOrigin.current = null;
  };

  const handleDeleteCourse = async () => {
    if (!deleteTarget) return;
    setDeleting(true);
    try {
      await api.courses.remove(deleteTarget.id);
      removeCourse(deleteTarget.id);
      addToast(`已删除课程「${deleteTarget.name}」`, 'success');
      setDeleteTarget(null);
    } catch {
      addToast('删除课程失败，请重试', 'error');
    } finally {
      setDeleting(false);
    }
  };

  /** 每张课程卡的长按手势绑定（pointer 事件统一鼠标与触摸）：按下 600ms 触发删除确认 */
  const cardPressProps = (course: Course) => ({
    onPointerDown: (e: React.PointerEvent<HTMLDivElement>) => {
      const el = e.currentTarget; // currentTarget 在事件处理器返回后即被置空，定时器要用必须先捕获
      cancelPress();
      longPressFired.current = false;
      pressOrigin.current = { x: e.clientX, y: e.clientY };
      el.style.transform = 'scale(0.97)'; // 按下反馈
      pressTimer.current = window.setTimeout(() => {
        pressTimer.current = null;
        pressOrigin.current = null;
        longPressFired.current = true;
        el.style.transform = '';
        setDeleteTarget(course);
      }, 600);
    },
    onPointerMove: (e: React.PointerEvent<HTMLDivElement>) => {
      const origin = pressOrigin.current;
      if (!origin || pressTimer.current === null) return;
      // 手抖超过 10px 视为滑动/误触，取消长按
      if (Math.abs(e.clientX - origin.x) > 10 || Math.abs(e.clientY - origin.y) > 10) {
        cancelPress();
        e.currentTarget.style.transform = '';
      }
    },
    onPointerUp: (e: React.PointerEvent<HTMLDivElement>) => {
      cancelPress();
      e.currentTarget.style.transform = '';
    },
    onPointerLeave: (e: React.PointerEvent<HTMLDivElement>) => {
      cancelPress();
      e.currentTarget.style.transform = '';
    },
    onPointerCancel: (e: React.PointerEvent<HTMLDivElement>) => {
      cancelPress();
      e.currentTarget.style.transform = '';
    },
    // 屏蔽原生长按/右键菜单，避免与删除手势冲突
    onContextMenu: (e: React.MouseEvent) => e.preventDefault(),
  });

  return (
    <div style={{ minHeight: '100vh', display: 'flex', flexDirection: 'column' }}>
      {/* 顶部栏 */}
      <header style={{
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        padding: '16px 32px', borderBottom: '1px solid var(--line-soft)',
        position: 'sticky', top: 0, zIndex: 10, background: 'var(--surface-solid)',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
          <div style={{
            width: 38, height: 38, borderRadius: '12px',
            background: 'var(--brand)',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            color: 'white',
          }}>
            <BookOpen size={20} />
          </div>
          <div>
            <div style={{ fontSize: '1rem', fontWeight: 600, lineHeight: 1.2 }}>PaperPact</div>
            <div style={{ fontSize: '0.6875rem', color: 'var(--text-tertiary)' }}>AI 命题系统</div>
          </div>
        </div>
        <button onClick={logout} title="退出登录" style={{
          display: 'flex', alignItems: 'center', gap: '6px',
          background: 'none', border: 'none', cursor: 'pointer',
          padding: '8px 12px', borderRadius: 'var(--radius-sm)',
          color: 'var(--text-secondary)', fontSize: '0.8125rem',
        }}
          onMouseEnter={(e) => { e.currentTarget.style.background = 'var(--fill-strong)'; }}
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
        <div className="page-enter page-stack">
          <div>
            <h1 className="page-title">课程空间</h1>
            <p className="page-subtitle">选择一门课程开始命题工作，或创建新课程</p>
          </div>

          <div className="bento bento-3">
            {loading ? (
              // 直接铺进父网格：形状与真实课程卡一致，加载完不跳变
              Array.from({ length: 4 }).map((_, i) => (
                <div key={i} className="skeleton skeleton-card" style={{ minHeight: '200px' }} />
              ))
            ) : (
              <>
            {courses.map((course) => (
              <div
                key={course.id}
                role="button"
                tabIndex={0}
                title="点击进入，长按可删除课程"
                {...cardPressProps(course)}
                onClick={() => {
                  if (longPressFired.current) {
                    longPressFired.current = false; // 长按触发后吞掉随后的 click，避免误入课程
                    return;
                  }
                  handleSelectCourse(course.id);
                }}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    handleSelectCourse(course.id);
                  }
                }}
                style={{ cursor: 'pointer', userSelect: 'none' }}
                className="stagger-item"
              >
                <div className="glass-card card-hover" style={{ padding: '24px', height: '100%', display: 'flex', flexDirection: 'column' }}>
                  <div className="icon-box" style={{
                    width: 46, height: 46,
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
                minHeight: '200px', borderRadius: 'var(--radius-lg)',
                border: '1.5px dashed var(--line-strong)', background: 'var(--fill)',
                cursor: 'pointer', display: 'flex', flexDirection: 'column',
                alignItems: 'center', justifyContent: 'center', gap: '10px',
                color: 'var(--text-secondary)', fontSize: '0.875rem', fontWeight: 500,
                transition: 'transform 0.2s cubic-bezier(0, 0, 0.2, 1), box-shadow 0.2s cubic-bezier(0, 0, 0.2, 1), background 0.2s cubic-bezier(0, 0, 0.2, 1)',
              }}
              onMouseEnter={(e) => { e.currentTarget.style.background = 'var(--fill-strong)'; e.currentTarget.style.transform = 'translateY(-2px)'; e.currentTarget.style.boxShadow = 'var(--shadow-hover)'; }}
              onMouseLeave={(e) => { e.currentTarget.style.background = 'var(--fill)'; e.currentTarget.style.transform = 'none'; e.currentTarget.style.boxShadow = 'none'; }}
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
            </>
            )}
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
        {categories.length > 0 && (
          <>
            <Select
              label="课程类别"
              value={newCategory}
              options={categories.map((c) => ({ value: c.key, label: c.label }))}
              onChange={(e) => setNewCategory(e.target.value)}
            />
            <p style={{ fontSize: '0.75rem', color: 'var(--text-secondary)', margin: 0 }}>
              {categories.find((c) => c.key === newCategory)?.description ?? ''}
            </p>
          </>
        )}
      </Modal>

      {/* 长按课程卡的删除确认弹窗 */}
      <Modal
        open={!!deleteTarget}
        onClose={() => setDeleteTarget(null)}
        title="删除课程"
        confirmLabel="确认删除"
        danger
        loading={deleting}
        onConfirm={handleDeleteCourse}
      >
        <p style={{ margin: '0 0 8px' }}>
          确定删除课程「<strong>{deleteTarget?.name}</strong>」吗？
        </p>
        <p style={{ margin: 0, fontSize: '0.8125rem', color: 'var(--text-secondary)' }}>
          该课程下的全部资料、命题框架、知识目录与试卷将一并删除，此操作不可恢复。
        </p>
      </Modal>
    </div>
  );
}
