import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ArrowRight, Share2, Upload } from 'lucide-react';
import { api } from '@/api/client';
import type { CourseMaterialResource } from '@/api/domains/knowledgeBase';
import type { CourseResponse } from '@/types/api';
import { Button, Input, Modal, Select } from '@/components/ui';
import { ShareCourseMaterialModal } from '@/components/ShareCourseMaterialModal';
import { useAuthStore } from '@/stores/auth';
import { useCourseResourcesStore } from '@/stores/courseResources';

const STATE_LABEL: Record<string, string> = {
  processing: '解析中', ready: '待发布', published: '已发布', failed: '解析失败',
  withdrawn: '已撤回', superseded: '历史版本', draft: '草稿',
};
const sourceLinkStyle = { border: '1px solid var(--line)', background: 'var(--fill)', color: 'var(--text-secondary)', borderRadius: 6, padding: '4px 8px', cursor: 'pointer', textAlign: 'left' as const, whiteSpace: 'normal' as const, overflowWrap: 'anywhere' as const };

export function CourseMaterialsPanel({ fromCourse, targetSpaceId }: { fromCourse: string | null; targetSpaceId: string | null }) {
  const navigate = useNavigate();
  const resources = useCourseResourcesStore();
  const load = resources.load;
  const userId = useAuthStore((s) => s.user?.id);
  const [query, setQuery] = useState('');
  const [courseFilter, setCourseFilter] = useState(fromCourse || '');
  const [selected, setSelected] = useState<CourseMaterialResource | null>(null);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [courses, setCourses] = useState<CourseResponse[]>([]);
  const [uploadCourse, setUploadCourse] = useState(fromCourse || '');
  const [courseError, setCourseError] = useState('');
  useEffect(() => { void load(); }, [load, userId]);
  useEffect(() => {
    let cancelled = false;
    api.courses.list().then((value) => {
      if (cancelled) return;
      setCourses(value);
      setUploadCourse((old) => value.some((course) => course.id === old) ? old : value[0]?.id || '');
    }).catch(() => { if (!cancelled) setCourseError('课程列表读取失败，请刷新重试'); });
    return () => { cancelled = true; };
  }, [userId]);
  const items = resources.ownerId === userId ? resources.items : [];
  const visible = items.filter((item) =>
    (!courseFilter || item.course_id === courseFilter) &&
    (!query.trim() || `${item.title} ${item.course_name}`.toLowerCase().includes(query.trim().toLowerCase()))
  );

  return <section style={{ marginTop: 22 }}>
    <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', alignItems: 'end', marginBottom: 18 }}>
      <div style={{ flex: '1 1 240px' }}><Input label="查找我的资料" placeholder="资料名或课程名" value={query} onChange={(e) => setQuery(e.target.value)} /></div>
      <div style={{ flex: '1 1 180px' }}><Select label="所属课程" value={courseFilter} options={[
        { value: '', label: '全部课程' }, ...courses.map((course) => ({ value: course.id, label: course.name })),
      ]} onChange={(e) => setCourseFilter(e.target.value)} /></div>
      <Button icon={<Upload size={16} />} onClick={() => setUploadOpen(true)}>上传资料</Button>
    </div>
    {targetSpaceId && <p style={{ color: 'var(--brand-ink)', marginBottom: 16 }}>选择已有资料，添加到刚才的共享库。</p>}
    {resources.error ? <div role="alert"><p>{resources.error}</p><Button variant="secondary" onClick={() => void resources.load()}>重试</Button></div>
      : resources.loading ? <p>正在读取已有课程资料…</p>
      : !visible.length ? <p style={{ padding: '40px 0', color: 'var(--text-secondary)' }}>没有符合条件的资料。已有课程文件会直接显示在这里。</p>
      : <div style={{ borderTop: '1px solid var(--line)' }}>
        {visible.map((item) => <article key={item.version_id} style={{ padding: '18px 0', borderBottom: '1px solid var(--line)', display: 'flex', gap: 16, flexWrap: 'wrap', alignItems: 'center' }}>
          <div style={{ flex: '1 1 300px', minWidth: 0 }}>
            <h3 style={{ fontSize: '0.9375rem', margin: 0, overflowWrap: 'anywhere' }}>{item.title}</h3>
            <p style={{ fontSize: '0.8125rem', color: 'var(--text-secondary)', marginTop: 6 }}>{item.course_name} · v{item.version_no} · {Math.ceil(item.size_bytes / 1024)} KB</p>
            {item.shared_versions.length ? <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginTop: 8 }}>
              {item.shared_versions.map((shared) => <button className="badge badge-default" key={shared.document_id}
                style={sourceLinkStyle}
                onClick={() => navigate(`/knowledge/${shared.space_id}?fromCourse=${item.course_id}&document=${shared.document_id}`)}>
                {shared.space_name} · {STATE_LABEL[shared.state] || shared.state}{shared.source_version_id !== item.version_id ? ' · 有新源版本' : ''}
              </button>)}
            </div> : item.knowledge_origin ? <button className="badge badge-default" style={{ ...sourceLinkStyle, marginTop: 8 }} onClick={() => navigate(
              `/knowledge/${item.knowledge_origin!.space_id}?fromCourse=${item.course_id}` + (item.knowledge_origin!.document_id ? `&document=${item.knowledge_origin!.document_id}` : '')
            )}>来自{item.knowledge_origin.space_name} · v{item.knowledge_origin.version_no}{item.knowledge_origin.locally_modified ? ' · 已在课程修改' : ''}</button>
              : <span className="badge badge-default" style={{ marginTop: 8 }}>仅当前课程</span>}
            {!item.can_share && <p style={{ fontSize: '0.75rem', color: 'var(--text-tertiary)', marginTop: 6 }}>{item.share_blocked_reason}</p>}
          </div>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            <Button variant="secondary" size="sm" icon={<ArrowRight size={14} />} onClick={() => navigate(`/courses/${item.course_id}/materials?material=${item.material_id}`)}>在课程中查看</Button>
            <Button size="sm" icon={<Share2 size={14} />} disabled={!item.can_share} onClick={() => setSelected(item)}>发布到共享库</Button>
          </div>
        </article>)}
      </div>}
    <ShareCourseMaterialModal source={selected} targetSpaceId={targetSpaceId} onClose={() => setSelected(null)} onShared={() => void resources.load()} />
    <Modal open={uploadOpen} onClose={() => setUploadOpen(false)} title="添加教学资料" footer={<>
      <Button variant="secondary" onClick={() => setUploadOpen(false)}>取消</Button>
      <Button disabled={!uploadCourse} onClick={() => navigate(`/courses/${uploadCourse}/materials?upload=1`)}>选择文件</Button>
    </>}>
      <p style={{ marginBottom: 16 }}>选择资料所属课程。上传后会同时显示在课程与“我的资料”中，再按需发布共享。</p>
      {courseError ? <p role="alert">{courseError}</p> : courses.length ? <Select label="所属课程" value={uploadCourse} options={courses.map((course) => ({ value: course.id, label: course.name }))} onChange={(e) => setUploadCourse(e.target.value)} />
        : <Button variant="secondary" onClick={() => navigate('/courses')}>前往创建课程</Button>}
    </Modal>
  </section>;
}
