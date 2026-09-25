import { useState, useEffect, useRef } from 'react';
import { useParams, useSearchParams } from 'react-router-dom';
import { Plus, ChevronRight, ArrowLeft, ClipboardList, FileText, PlayCircle, Trash2 } from 'lucide-react';
import { api } from '@/api/client';
import { getErrorMessage, isApiError } from '@/api/errors';
import { useAuthStore } from '@/stores/auth';
import { useToastStore } from '@/stores/toast';
import { Button } from '@/components/ui/Button';
import { Badge, Input } from '@/components/ui';
import { SkeletonCardGrid, SkeletonList } from '@/components/ui/Skeleton';
import PipelinePanel from './PipelinePanel';
import PaperPanel from './PaperPanel';
import { EXAM_PROJECT_STATUS_META, PAPER_STATUS_META } from '@/lib/examDisplay';
import type { ExamProject, PaperVersion } from '@/types/api';

type TabKey = 'pipeline' | 'paper';

const TABS: Array<{ key: TabKey; label: string; icon: React.ReactNode }> = [
  { key: 'pipeline', label: '出卷流水线', icon: <ClipboardList size={15} /> },
  { key: 'paper', label: '试卷', icon: <FileText size={15} /> },
];

/**
 * 「试卷」模块：出卷流水线与试卷查看/审核/导出合为一个页面。
 * 流水线只到生成为止；试卷页签承载查看、编辑、定稿与导出，两者通过页签切换，
 * 不再分成两个入口、两套项目上下文。
 */
export default function PaperPage() {
  const { courseId: routeCourseId } = useParams<{ courseId: string }>();
  const courseId = routeCourseId || '';
  const token = useAuthStore((s) => s.token);
  const addToast = useToastStore((s) => s.addToast);
  const [searchParams, setSearchParams] = useSearchParams();

  const [projects, setProjects] = useState<ExamProject[]>([]);
  const [loading, setLoading] = useState(true);
  const [activeProject, setActiveProject] = useState<ExamProject | null>(null);
  const [tab, setTab] = useState<TabKey>('pipeline');
  const [paper, setPaper] = useState<PaperVersion | null>(null);
  const [paperLoading, setPaperLoading] = useState(false);

  const [createOpen, setCreateOpen] = useState(false);
  const [newName, setNewName] = useState('');
  // ?project=<id> 直达（从其它位置带项目过来时自动展开），只做一次
  const autoOpenedRef = useRef(false);

  const loadProjects = async () => {
    if (!courseId) return;
    try {
      setLoading(true);
      const res = await api.examProjects.list(courseId, token ?? undefined);
      setProjects(res);
      const pid = searchParams.get('project');
      const target = pid ? res.find((p) => p.id === pid) : undefined;
      if (target && !autoOpenedRef.current) {
        autoOpenedRef.current = true;
        openProject(target);
      }
    } catch {
      addToast('加载项目失败', 'error');
    } finally {
      setLoading(false);
    }
  };

  const loadPaper = async (projectId: string) => {
    setPaperLoading(true);
    try {
      const pv = await api.paperVersions.getCurrent(courseId, projectId, token ?? undefined);
      setPaper(pv);
    } catch (e) {
      if (!isApiError(e) || e.status !== 404) {
        addToast('加载试卷失败', 'error');
      }
      setPaper(null);
    } finally {
      setPaperLoading(false);
    }
  };

  const openProject = (proj: ExamProject) => {
    setActiveProject(proj);
    // 已生成试卷的项目直接落到「试卷」页签；未生成的落到流水线继续出题。
    // 项目摘要已经告诉我们有没有试卷，没有就完全不必去探 paper-versions/current
    // ——那只会拿到 404，在控制台刷错误还多一次往返。
    const hasPaper = (proj.total_score ?? 0) > 0 || (proj.item_count ?? 0) > 0 || !!proj.paper_version_id;
    setTab(hasPaper ? 'paper' : 'pipeline');
    setPaper(null);
    setSearchParams({ project: proj.id }, { replace: true });
    if (hasPaper) {
      void loadPaper(proj.id);
    }
  };

  const refreshPaperAndProject = async () => {
    if (!activeProject) return;
    const fresh = await api.examProjects.get(courseId, activeProject.id, token ?? undefined).catch(() => null);
    if (fresh) setActiveProject(fresh);
    const source = fresh ?? activeProject;
    // 没有试卷就不探 paper-versions/current：刚建完蓝图时必然没有，探了也是 404。
    // 已经加载过试卷时不依赖摘要判断——编辑保存后摘要可能短暂落后于实际数据。
    const hasPaper =
      (source.total_score ?? 0) > 0 || (source.item_count ?? 0) > 0
      || !!source.paper_version_id || paper !== null;
    if (!hasPaper) {
      setPaper(null);
    } else {
      // 生成刚完成时 paper 还是 null：不打 loading 就会先闪一屏
      // 「该项目还没有生成试卷」，再跳回试卷内容，像数据丢了。
      // paper 已有值时不亮 loading，继续展示旧内容，避免刷新闪烁。
      setPaperLoading(true);
      try {
        const pv = await api.paperVersions.getCurrent(courseId, activeProject.id, token ?? undefined).catch(() => null);
        setPaper(pv);
      } finally {
        setPaperLoading(false);
      }
    }
    const list = await api.examProjects.list(courseId, token ?? undefined).catch(() => null);
    if (list) setProjects(list);
  };

  /** 只刷项目摘要：确认合同会推进 status，页头徽章不能停在旧状态 */
  const refreshProject = async () => {
    if (!activeProject) return;
    const fresh = await api.examProjects.get(courseId, activeProject.id, token ?? undefined).catch(() => null);
    if (fresh) setActiveProject(fresh);
  };

  const handleCreateProject = async () => {
    const name = newName.trim();
    if (!name || !courseId) return;
    try {
      const proj = await api.examProjects.create(courseId, { name }, token ?? undefined);
      setProjects((s) => [...s, proj]);
      setCreateOpen(false);
      setNewName('');
      addToast('项目创建成功', 'success');
      openProject(proj);
    } catch {
      addToast('创建失败', 'error');
    }
  };

  const handleDeleteProject = async (proj: ExamProject) => {
    if (!courseId) return;
    const summary = (proj.total_score ?? 0) > 0 || (proj.item_count ?? 0) > 0;
    if (!window.confirm(
      `确认删除项目「${proj.name}」？\n\n将同时删除该项目的蓝图、题位、生成记录与试卷版本${summary ? `（${proj.item_count ?? 0} 道题）` : ''}，此操作不可恢复。`,
    )) return;
    try {
      await api.examProjects.remove(courseId, proj.id, token ?? undefined);
      addToast('项目已删除', 'success');
      setProjects((s) => s.filter((p) => p.id !== proj.id));
    } catch (e) {
      addToast('删除失败: ' + getErrorMessage(e), 'error');
    }
  };

  // 从试卷页签请求流水线切到指定阶段（重新生成走这条路径）
  const [stageRequest, setStageRequest] = useState<{ stage: 'blueprint' | 'contract' | 'generate'; nonce: number } | null>(null);
  const requestPipelineStage = (stage: 'blueprint' | 'contract' | 'generate') => {
    setStageRequest({ stage, nonce: Date.now() });
    setTab('pipeline');
  };

  useEffect(() => {
    loadProjects();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [courseId]);

  if (loading) {
    return (
      <div className="page-enter">
        <SkeletonCardGrid count={4} />
      </div>
    );
  }

  // ── 项目列表 ──
  if (!activeProject) {
    return (
      <div className="page-enter">
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '24px' }}>
          <div>
            <h1 style={{ fontSize: '1.75rem', fontWeight: 700, letterSpacing: '-0.03em', marginBottom: '6px' }}>试卷</h1>
            <p style={{ fontSize: '0.9375rem', color: 'var(--text-secondary)' }}>
              出卷流水线（蓝图 → 合同 → 生成）与试卷的查看、审核、导出，都在同一个项目里完成
            </p>
          </div>
          <Button onClick={() => { setNewName(''); setCreateOpen(true); }} icon={<Plus size={16} />}>新建项目</Button>
        </div>

        {projects.length === 0 ? (
          <div className="glass-card" style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', padding: '72px 24px', gap: '16px' }}>
            <div style={{ width: 60, height: 60, borderRadius: '18px', background: 'var(--accent-subtle)', color: 'var(--accent)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
              <ClipboardList size={30} />
            </div>
            <h3 style={{ fontWeight: 600, fontSize: '1.05rem' }}>暂无试卷项目</h3>
            <p style={{ fontSize: '0.85rem', color: 'var(--text-secondary)' }}>点击「新建项目」开始您的第一次出卷</p>
          </div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
            {projects.map((p) => {
              const sm = EXAM_PROJECT_STATUS_META[p.status] ?? { label: p.status, variant: 'default' as const };
              const psm = p.paper_version_status ? (PAPER_STATUS_META[p.paper_version_status] ?? null) : null;
              return (
                <div
                  key={p.id}
                  className="glass-card"
                  style={{ padding: '16px 24px', cursor: 'pointer', display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}
                  onClick={() => openProject(p)}
                >
                  <div style={{ display: 'flex', alignItems: 'center', gap: '14px' }}>
                    <div style={{
                      width: 42, height: 42, borderRadius: '12px',
                      background: 'rgba(0,113,227,0.08)', display: 'flex', alignItems: 'center', justifyContent: 'center',
                    }}>
                      <ClipboardList size={20} style={{ color: '#0071e3' }} />
                    </div>
                    <div>
                      <div style={{ fontWeight: 600, fontSize: '0.9rem' }}>{p.name}</div>
                      <div style={{ fontSize: '0.75rem', color: 'var(--text-tertiary)', marginTop: '3px' }}>
                        {p.total_score ? `${p.total_score} 分 · ${p.item_count ?? 0} 题` : '待生成'}
                        {p.paper_version_no ? ` · 试卷 v${p.paper_version_no}` : ''}
                      </div>
                    </div>
                  </div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                    {psm && <Badge variant={psm.variant}>试卷 {psm.label}</Badge>}
                    <Badge variant={sm.variant}>{sm.label}</Badge>
                    <button
                      onClick={(e) => { e.stopPropagation(); void handleDeleteProject(p); }}
                      title="删除项目"
                      style={{
                        background: 'none', border: 'none', cursor: 'pointer', padding: '6px',
                        borderRadius: 8, color: 'var(--text-tertiary)', display: 'flex',
                      }}
                      onMouseEnter={(e) => { e.currentTarget.style.background = 'var(--error-subtle)'; e.currentTarget.style.color = 'var(--error)'; }}
                      onMouseLeave={(e) => { e.currentTarget.style.background = 'none'; e.currentTarget.style.color = 'var(--text-tertiary)'; }}
                    >
                      <Trash2 size={16} />
                    </button>
                    <ChevronRight size={18} style={{ color: 'var(--text-tertiary)' }} />
                  </div>
                </div>
              );
            })}
          </div>
        )}

        {createOpen && (
          <div className="modal-overlay" onClick={() => setCreateOpen(false)}>
            <div className="modal-content" style={{ maxWidth: '420px' }} onClick={(e) => e.stopPropagation()}>
              <div className="modal-header">
                <h3 className="modal-title">新建项目</h3>
              </div>
              <div className="modal-body">
                <Input label="项目名称" placeholder="请输入项目名称" value={newName} onChange={(e) => setNewName(e.target.value)} autoFocus />
              </div>
              <div className="modal-footer">
                <Button variant="secondary" onClick={() => setCreateOpen(false)}>取消</Button>
                <Button onClick={handleCreateProject}>创建</Button>
              </div>
            </div>
          </div>
        )}
      </div>
    );
  }

  // ── 项目详情 ──
  const sp = activeProject;
  const statusMeta = EXAM_PROJECT_STATUS_META[sp.status] ?? { label: sp.status, variant: 'default' as const };
  const paperMeta = sp.paper_version_status ? (PAPER_STATUS_META[sp.paper_version_status] ?? null) : null;
  const pipelineStage = sp.status === 'generating' ? '生成中' : statusMeta.label;

  return (
    <div className="page-enter" style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
      <button
        onClick={() => { setActiveProject(null); setSearchParams({}, { replace: true }); }}
        style={{
          background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text-tertiary)',
          fontSize: '0.8125rem', display: 'flex', alignItems: 'center', gap: '4px',
          padding: 0, marginBottom: '-4px', alignSelf: 'flex-start',
        }}
      >
        <ArrowLeft size={16} /> 返回项目列表
      </button>

      <div className="glass-card" style={{ padding: '24px' }}>
        <div className="card-head">
          <div>
            <h1 style={{ fontWeight: 700, fontSize: '1.3rem', letterSpacing: '-0.02em' }}>{sp.name}</h1>
            <p style={{ fontSize: '0.8125rem', color: 'var(--text-tertiary)', marginTop: '4px' }}>
              {sp.total_score ? `${sp.total_score} 分 · ${sp.item_count ?? 0} 题` : '尚未生成试卷'}
              {' · 流水线：'}{pipelineStage}
            </p>
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
            <Badge variant={statusMeta.variant}>{statusMeta.label}</Badge>
            {paperMeta && sp.paper_version_no && (
              <Badge variant={paperMeta.variant}>试卷 {paperMeta.label} v{sp.paper_version_no}</Badge>
            )}
          </div>
        </div>

        {/* 页签：出卷流水线 / 试卷 */}
        <div style={{ display: 'flex', gap: '4px', marginTop: '16px', borderBottom: '1px solid rgba(0,0,0,0.07)' }}>
          {TABS.map((t) => (
            <button
              key={t.key}
              onClick={() => setTab(t.key)}
              style={{
                display: 'flex', alignItems: 'center', gap: '7px',
                padding: '10px 16px', border: 'none', cursor: 'pointer', background: 'none',
                fontSize: '0.875rem', fontWeight: tab === t.key ? 600 : 500,
                color: tab === t.key ? 'var(--accent)' : 'var(--text-secondary)',
                borderBottom: '2px solid ' + (tab === t.key ? 'var(--accent)' : 'transparent'),
                marginBottom: '-1px',
              }}
            >
              {t.icon}{t.label}
              {t.key === 'paper' && (sp.item_count ?? 0) > 0 && (
                <span style={{ fontSize: '0.75rem', color: 'var(--text-tertiary)' }}>{sp.item_count}</span>
              )}
            </button>
          ))}
        </div>
      </div>

      {/* 流水线常驻挂载，切到试卷页签只隐藏不卸载：生成任务的轮询在
          PipelinePanel 内部，条件渲染会让教师一切页签就中断轮询、
          永远等不到「生成完成」的提示。 */}
      <div className="glass-card" style={{ padding: '24px', display: tab === 'pipeline' ? undefined : 'none' }}>
        <PipelinePanel
          sp={sp}
          courseId={courseId}
          stageRequest={stageRequest}
          onOpenPaper={() => {
            void refreshPaperAndProject();
            setTab('paper');
          }}
          onBlueprintCreated={(blueprintVersionId) => {
            setActiveProject({ ...sp, active_blueprint_version_id: blueprintVersionId, status: 'blueprint' });
          }}
          onProjectChanged={() => { void refreshProject(); }}
        />
      </div>

      {tab === 'pipeline' ? null : paperLoading && !paper ? (
        <div className="glass-card" style={{ padding: '32px 24px' }}>
          <SkeletonList rows={4} />
        </div>
      ) : !paper ? (
        <div className="glass-card" style={{ padding: '48px 24px', textAlign: 'center' }}>
          <h3 style={{ fontWeight: 600, fontSize: '1rem', marginBottom: '8px' }}>该项目还没有生成试卷</h3>
          <p style={{ fontSize: '0.85rem', color: 'var(--text-secondary)', marginBottom: '18px' }}>
            在出卷流水线中完成蓝图、合同并生成后，试卷会出现在这里。
          </p>
          <Button onClick={() => setTab('pipeline')} icon={<PlayCircle size={16} />}>去出卷流水线</Button>
        </div>
      ) : (
        <PaperPanel
          pv={paper}
          project={sp}
          courseId={courseId}
          onChanged={refreshPaperAndProject}
          onRegenerate={() => requestPipelineStage('generate')}
        />
      )}
    </div>
  );
}
