import { useEffect, useMemo, useState } from 'react';
import type { CSSProperties, ReactNode } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import {
  Activity,
  AlertTriangle,
  ArrowLeft,
  BookMarked,
  BookOpen,
  ChevronRight,
  FolderPlus,
  Plus,
  Send,
  ShieldCheck,
  Users,
} from 'lucide-react';
import { useAuthStore } from '@/stores/auth';
import { useToastStore } from '@/stores/toast';
import { useKnowledgeBaseStore } from '@/stores/knowledgeBase';
import { api } from '@/api/client';
import type {
  KnowledgeGovernanceSummary,
  KnowledgeMeta,
  KnowledgeRole,
  KnowledgeSessionDetail,
  ReadScope,
} from '@/api/domains/knowledgeBase';
import { Button, Input, Modal, Select } from '@/components/ui';
import { TopNav } from '@/components/layout/TopNav';
import { CourseMaterialsPanel } from './CourseMaterialsPanel';

const ROLE_LABEL: Record<KnowledgeRole, string> = {
  owner: '负责人',
  publisher: '发布者',
  editor: '编辑者',
  reader: '阅读者',
};

const ROLE_STYLE: Record<KnowledgeRole, { bg: string; ink: string }> = {
  owner: { bg: 'var(--brand-subtle)', ink: 'var(--brand-ink)' },
  publisher: { bg: 'var(--purple-subtle)', ink: 'var(--purple-ink)' },
  editor: { bg: 'var(--info-subtle)', ink: 'var(--info-ink)' },
  reader: { bg: 'var(--fill-strong)', ink: 'var(--text-secondary)' },
};

export default function KnowledgeCenterPage() {
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const view = params.get('view') === 'shared' ? 'shared' : 'mine';
  const fromCourse = params.get('fromCourse');
  const courseQuery = fromCourse ? `?fromCourse=${fromCourse}` : '';
  const user = useAuthStore((s) => s.user);
  const addToast = useToastStore((s) => s.addToast);

  const spaces = useKnowledgeBaseStore((s) => s.spaces);
  const loading = useKnowledgeBaseStore((s) => s.loading);
  const error = useKnowledgeBaseStore((s) => s.error);
  const load = useKnowledgeBaseStore((s) => s.load);
  const create = useKnowledgeBaseStore((s) => s.create);

  const [meta, setMeta] = useState<KnowledgeMeta | null>(null);
  const [activeTag, setActiveTag] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState('');
  const [scope, setScope] = useState<ReadScope>('members');
  const [tagText, setTagText] = useState('');
  const [allowCourseImport, setAllowCourseImport] = useState(false);
  const [governance, setGovernance] = useState<KnowledgeGovernanceSummary | null>(null);
  const [governanceError, setGovernanceError] = useState<string | null>(null);

  // 跨库问答（最多 5 个有权空间）
  const [crossOpen, setCrossOpen] = useState(false);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [crossSession, setCrossSession] = useState<KnowledgeSessionDetail | null>(null);
  const [crossQuestion, setCrossQuestion] = useState('');
  const [crossAsking, setCrossAsking] = useState(false);

  useEffect(() => {
    void load();
    let cancelled = false;
    api.knowledgeBase
      .meta()
      .then((value) => {
        if (!cancelled) setMeta(value);
      })
      .catch(() => {
        // 选项说明不可用不阻塞列表；创建弹窗退回内置文案。
      });
    return () => {
      cancelled = true;
    };
  }, [load]);

  useEffect(() => {
    const role = user?.role;
    if (role !== 'admin' && role !== 'org_admin') return;
    let cancelled = false;
    api.knowledgeBase
      .adminSummary()
      .then((value) => {
        if (!cancelled) {
          setGovernance(value);
          setGovernanceError(null);
        }
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        const status = (err as { status?: number }).status;
        setGovernanceError(
          status === 403
            ? '当前账号没有可用的学校归属，无法查看治理汇总；这不影响你访问已授权的共享资料。'
            : '治理汇总暂时不可用，请稍后重试。'
        );
      });
    return () => {
      cancelled = true;
    };
  }, [user?.role]);

  const tags = useMemo(
    () => Array.from(new Set(spaces.flatMap((s) => s.subject_tags))).slice(0, 12),
    [spaces]
  );

  const visible = useMemo(
    () => (activeTag ? spaces.filter((s) => s.subject_tags.includes(activeTag)) : spaces),
    [spaces, activeTag]
  );

  const ownedCount = spaces.filter((s) => s.role === 'owner').length;
  const docCount = spaces.reduce((sum, s) => sum + s.document_count, 0);

  const handleCreate = async () => {
    const trimmed = name.trim();
    if (!trimmed) {
      addToast('请填写知识库名称', 'info');
      return;
    }
    setCreating(true);
    try {
      const subjectTags = tagText
        .split(/[,，\s]+/)
        .map((tag) => tag.trim())
        .filter(Boolean)
        .slice(0, 8);
      const space = await create({ name: trimmed, read_scope: scope, subject_tags: subjectTags, allow_course_import: allowCourseImport });
      addToast('知识库已创建', 'success');
      setCreateOpen(false);
      setName('');
      setTagText('');
      setAllowCourseImport(false);
      setScope('members');
      navigate('/knowledge/' + space.id + courseQuery);
    } catch (err) {
      addToast(err instanceof Error ? err.message : '创建失败', 'error');
    } finally {
      setCreating(false);
    }
  };

  const readerScopeHint =
    meta?.read_scopes.find((item) => item.value === scope)?.description ??
    '默认仅成员可读，后续可随时调整。';

  const openCrossAsk = () => {
    setSelectedIds(spaces.slice(0, 5).map((space) => space.id));
    setCrossSession(null);
    setCrossQuestion('');
    setCrossOpen(true);
  };

  const toggleSpace = (spaceId: string) => {
    if (selectedIds.includes(spaceId)) {
      setSelectedIds(selectedIds.filter((id) => id !== spaceId));
      return;
    }
    if (selectedIds.length >= 5) {
      addToast('一次最多选择 5 个知识库', 'info');
      return;
    }
    setSelectedIds([...selectedIds, spaceId]);
  };

  const handleCrossAsk = async () => {
    const text = crossQuestion.trim();
    if (!text || !selectedIds.length) return;
    setCrossAsking(true);
    try {
      let current = crossSession;
      const sameSelection =
        current &&
        current.selected_space_ids.length === selectedIds.length &&
        current.selected_space_ids.every((id) => selectedIds.includes(id));
      if (!sameSelection) {
        current = await api.knowledgeBase.createSession(selectedIds, '跨库问答');
      }
      const updated = await api.knowledgeBase.ask(current!.id, text);
      setCrossSession(updated);
      setCrossQuestion('');
    } catch (err) {
      addToast(err instanceof Error ? err.message : '提问失败', 'error');
    } finally {
      setCrossAsking(false);
    }
  };

  return (
    <div style={{ minHeight: '100vh', background: 'var(--bg)' }}>
      <TopNav />
      <div style={{ maxWidth: 1180, margin: '0 auto', padding: '28px 32px 64px' }}>
        {/* 来自课程时的上下文回跳；顶层入口已统一在 TopNav */}
        {fromCourse && (
          <button onClick={() => navigate(`/courses/${fromCourse}`)} style={ghostButton}>
            <ArrowLeft size={15} />
            返回当前课程
          </button>
        )}

        {/* 页面标题与统计：与课程工作台保持同一标题/工具栏层级 */}
        <section
          style={{
            marginTop: 26,
            display: 'flex',
            alignItems: 'flex-end',
            justifyContent: 'space-between',
            gap: 24,
            flexWrap: 'wrap',
          }}
        >
          <div style={{ maxWidth: 640 }}>
            <h1 className="page-title">资料与知识中心</h1>
            <p className="page-subtitle" style={{ marginTop: 6 }}>
              管理教学资料、发布共享版本，并在课程中选用资料开展命题。
            </p>
          </div>
          <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
            <StatTile icon={<BookMarked size={17} />} value={spaces.length} label="可见知识库" />
            <StatTile icon={<ShieldCheck size={17} />} value={ownedCount} label="我负责的" />
            <StatTile icon={<Users size={17} />} value={docCount} label="已发布资料" />
          </div>
        </section>

        <div role="tablist" aria-label="资料范围" style={{ display: 'flex', gap: 8, marginTop: 24, borderBottom: '1px solid var(--line)', paddingBottom: 12 }}>
          {([{ id: 'mine', label: '我的资料' }, { id: 'shared', label: '校级共享库' }] as const).map((item) => (
            <button key={item.id} role="tab" aria-selected={view === item.id} className={view === item.id ? 'btn btn-primary' : 'btn btn-secondary'} onClick={() => {
              const next = new URLSearchParams(params); next.set('view', item.id); setParams(next);
            }}>{item.label}</button>
          ))}
        </div>
        {view === 'mine' ? <CourseMaterialsPanel fromCourse={fromCourse} targetSpaceId={params.get('targetSpace')} /> : <>

        {governanceError && (
          <section
            className="glass-panel"
            style={{
              marginTop: 18,
              padding: '14px 18px',
              display: 'flex',
              alignItems: 'center',
              gap: 10,
              color: 'var(--text-secondary)',
              fontSize: '0.8125rem',
            }}
          >
            <AlertTriangle size={16} color="var(--warning-ink, #b45309)" />
            {governanceError}
          </section>
        )}

        {governance && (
          <section
            className="glass-panel"
            style={{ marginTop: 18, padding: '18px 22px', display: 'flex', flexDirection: 'column', gap: 14 }}
          >
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: '0.9375rem', fontWeight: 650 }}>
                <Activity size={17} color="var(--brand-ink)" />
                治理概览
                <span className="badge badge-default" style={{ fontWeight: 500 }}>
                  脱敏统计
                </span>
              </div>
              <span style={{ fontSize: '0.6875rem', color: 'var(--text-tertiary)' }}>
                模型费用：{governance.model_cost.spend === 'unknown' ? '未知' : governance.model_cost.spend}
              </span>
            </div>
            <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
              <GovTile value={governance.spaces.total} label="知识库" />
              <GovTile value={governance.documents.published} label="已发布资料" />
              <GovTile value={governance.index.chunks} label="索引块" />
              <GovTile
                value={governance.parsing.failed}
                label="解析失败"
                warn={governance.parsing.failed > 0}
              />
              <GovTile
                value={(governance.jobs.queued ?? 0) + (governance.jobs.running ?? 0)}
                label="待处理任务"
                warn={(governance.jobs.failed ?? 0) > 0}
              />
            </div>
            <div style={{ fontSize: '0.6875rem', color: 'var(--text-tertiary)' }}>
              汇总不包含资料正文与他人私人问答；管理身份不代表可读取内容。
            </div>
          </section>
        )}

        {/* Toolbar */}
        <div
          style={{
            marginTop: 26,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            gap: 12,
            flexWrap: 'wrap',
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
            <FilterChip active={activeTag === null} onClick={() => setActiveTag(null)}>
              全部
            </FilterChip>
            {tags.map((tag) => (
              <FilterChip key={tag} active={activeTag === tag} onClick={() => setActiveTag(tag)}>
                {tag}
              </FilterChip>
            ))}
          </div>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            {spaces.length > 0 && (
              <Button
                variant="secondary"
                icon={<BookOpen size={16} />}
                onClick={openCrossAsk}
              >
                跨库问答
              </Button>
            )}
            <Button icon={<Plus size={16} />} onClick={() => setCreateOpen(true)}>
              新建知识库
            </Button>
          </div>
        </div>

        {/* Content */}
        {error ? (
          <div
            className="glass-panel"
            style={{ marginTop: 20, padding: 24, display: 'flex', alignItems: 'center', gap: 12 }}
          >
            <span style={{ color: 'var(--error-ink)', fontSize: '0.875rem' }}>{error}</span>
            <Button variant="secondary" size="sm" onClick={() => void load(activeTag ?? undefined)}>
              重试
            </Button>
          </div>
        ) : loading && !spaces.length ? (
          <div className="bento bento-3" style={{ marginTop: 20 }}>
            {[0, 1, 2].map((key) => (
              <div key={key} className="skeleton skeleton-card" />
            ))}
          </div>
        ) : visible.length ? (
          <div className="bento bento-3" style={{ marginTop: 20 }}>
            {visible.map((space) => (
              <button
                key={space.id}
                onClick={() => navigate('/knowledge/' + space.id + courseQuery)}
                className="card-hover"
                style={{
                  textAlign: 'left',
                  border: '1px solid var(--line)',
                  borderRadius: 'var(--radius-lg)',
                  background: 'var(--surface)',
                  padding: '20px 20px 18px',
                  cursor: 'pointer',
                  display: 'flex',
                  flexDirection: 'column',
                  gap: 14,
                  minHeight: 190,
                }}
              >
                <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 10 }}>
                  <span
                    style={{
                      width: 38,
                      height: 38,
                      borderRadius: 12,
                      background: ROLE_STYLE[space.role].bg,
                      color: ROLE_STYLE[space.role].ink,
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                      flexShrink: 0,
                    }}
                  >
                    <BookMarked size={19} />
                  </span>
                  <span
                    className="badge"
                    style={{ background: ROLE_STYLE[space.role].bg, color: ROLE_STYLE[space.role].ink }}
                  >
                    {ROLE_LABEL[space.role]}
                  </span>
                </div>

                <div style={{ flex: 1 }}>
                  <div style={{ fontSize: '1.0625rem', fontWeight: 650, lineHeight: 1.4 }}>
                    {space.name}
                  </div>
                  <div style={{ marginTop: 4, fontSize: '0.75rem', color: 'var(--text-tertiary)' }}>
                    {space.org_name || '校内单位'} · {space.read_scope_label}
                  </div>
                </div>

                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                  <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                    {space.subject_tags.slice(0, 2).map((tag) => (
                      <span key={tag} className="badge badge-default" style={{ fontSize: '0.6875rem' }}>
                        {tag}
                      </span>
                    ))}
                    {space.status === 'archived' && (
                      <span className="badge badge-warning" style={{ fontSize: '0.6875rem' }}>
                        已归档
                      </span>
                    )}
                  </div>
                  <span
                    style={{
                      display: 'inline-flex',
                      alignItems: 'center',
                      gap: 4,
                      fontSize: '0.75rem',
                      color: 'var(--text-tertiary)',
                    }}
                  >
                    {space.document_count} 份资料
                    <ChevronRight size={14} />
                  </span>
                </div>
              </button>
            ))}
          </div>
        ) : (
          <div
            className="glass-panel"
            style={{
              marginTop: 20,
              padding: '56px 24px',
              display: 'flex',
              flexDirection: 'column',
              alignItems: 'center',
              textAlign: 'center',
              gap: 12,
            }}
          >
            <span
              style={{
                width: 56,
                height: 56,
                borderRadius: 18,
                background: 'var(--brand-subtle)',
                color: 'var(--brand-ink)',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
              }}
            >
              <FolderPlus size={26} />
            </span>
            <div style={{ fontSize: '1.0625rem', fontWeight: 650 }}>
              {activeTag ? '该学科还没有可见的知识库' : '还没有可见的共享知识库'}
            </div>
            <div style={{ color: 'var(--text-secondary)', fontSize: '0.875rem', maxWidth: 420 }}>
              新建一个知识库，把可共享的讲义、例题与教学说明沉淀下来，
              同校老师就能在同一份资料上继续备课。
            </div>
            <Button icon={<Plus size={16} />} onClick={() => setCreateOpen(true)} style={{ marginTop: 6 }}>
              新建知识库
            </Button>
          </div>
        )}
        </>}
      </div>

      <Modal
        open={createOpen}
        onClose={() => (creating ? undefined : setCreateOpen(false))}
        title="新建共享知识库"
        onConfirm={handleCreate}
        confirmLabel="创建"
        loading={creating}
      >
        <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
          <Input
            label="知识库名称"
            placeholder="例如：数据结构 · 期末复习资料"
            value={name}
            maxLength={200}
            onChange={(e) => setName(e.target.value)}
          />
          <Select
            label="读者范围"
            value={scope}
            options={
              meta?.read_scopes.map((item) => ({ value: item.value, label: item.label })) ?? [
                { value: 'members', label: '仅指定教师' },
                { value: 'organization', label: '单位内教师' },
                { value: 'school', label: '本校教师' },
              ]
            }
            onChange={(e) => setScope(e.target.value as ReadScope)}
          />
          <div style={{ fontSize: '0.75rem', color: 'var(--text-tertiary)', marginTop: -8 }}>
            {readerScopeHint}扩大范围需要先查看影响预览并确认。
          </div>
          <Input
            label="学科标签（可选）"
            placeholder="用逗号分隔，例如：数据结构, 算法"
            value={tagText}
            onChange={(e) => setTagText(e.target.value)}
          />
          <label style={{ display: 'flex', gap: 10, alignItems: 'flex-start', fontSize: '0.8125rem', lineHeight: 1.7 }}>
            <input type="checkbox" checked={allowCourseImport} onChange={(e) => setAllowCourseImport(e.target.checked)} style={{ marginTop: 5 }} />
            允许有权阅读的教师选用到自己的课程。选用后保留独立副本，源库撤回不会收回已有课程副本。
          </label>
        </div>
      </Modal>

      <Modal
        open={crossOpen}
        onClose={() => (crossAsking ? undefined : setCrossOpen(false))}
        title="跨库问答"
        onConfirm={() => setCrossOpen(false)}
        confirmLabel="关闭"
      >
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          <div>
            <div style={{ fontSize: '0.75rem', color: 'var(--text-secondary)', marginBottom: 8 }}>
              选择知识库（最多 5 个，仅显示你有权访问的空间）
            </div>
            <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
              {spaces.map((space) => (
                <button
                  key={space.id}
                  onClick={() => toggleSpace(space.id)}
                  style={{
                    padding: '5px 10px',
                    borderRadius: 999,
                    border:
                      '1px solid ' +
                      (selectedIds.includes(space.id) ? 'var(--brand)' : 'var(--line)'),
                    background: selectedIds.includes(space.id)
                      ? 'var(--brand-subtle)'
                      : 'var(--surface)',
                    color: selectedIds.includes(space.id)
                      ? 'var(--brand-ink)'
                      : 'var(--text-secondary)',
                    fontSize: '0.75rem',
                    cursor: 'pointer',
                  }}
                >
                  {space.name}
                </button>
              ))}
            </div>
          </div>

          <div
            style={{
              minHeight: 180,
              maxHeight: 340,
              overflowY: 'auto',
              display: 'flex',
              flexDirection: 'column',
              gap: 10,
              padding: '10px 2px',
              borderTop: '1px solid var(--line)',
              borderBottom: '1px solid var(--line)',
            }}
          >
            {!crossSession?.messages.length && (
              <div style={{ padding: '26px 4px', textAlign: 'center', color: 'var(--text-tertiary)', fontSize: '0.8125rem' }}>
                综合所选知识库的已发布资料回答，并标注来源版本。
              </div>
            )}
            {crossSession?.messages.map((message) => (
              <div
                key={message.id}
                style={{
                  alignSelf: message.role === 'user' ? 'flex-end' : 'flex-start',
                  maxWidth: '85%',
                  background: message.role === 'user' ? 'var(--brand-subtle)' : 'var(--fill)',
                  borderRadius: 'var(--radius-md)',
                  padding: '10px 12px',
                }}
              >
                <div style={{ fontSize: '0.8125rem', lineHeight: 1.7, whiteSpace: 'pre-wrap' }}>
                  {message.content}
                </div>
                {message.sources.length > 0 && (
                  <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginTop: 8 }}>
                    {message.sources.map((source) => (
                      <button
                        key={source.source_id}
                        title={source.quote}
                        onClick={() => {
                          setCrossOpen(false);
                          navigate('/knowledge/' + source.space_id + courseQuery);
                        }}
                        style={{
                          border: '1px solid var(--line-strong)',
                          background: 'var(--surface)',
                          borderRadius: 999,
                          padding: '3px 9px',
                          fontSize: '0.6875rem',
                          color: 'var(--brand-ink)',
                          cursor: 'pointer',
                        }}
                      >
                        [{source.source_id}] {source.document_title} · v{source.version_no}
                      </button>
                    ))}
                  </div>
                )}
              </div>
            ))}
          </div>

          <div style={{ display: 'flex', gap: 10 }}>
            <Input
              placeholder="跨资料提问，例如：两种教材对同一概念的定义差异？"
              value={crossQuestion}
              onChange={(e) => setCrossQuestion(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) void handleCrossAsk();
              }}
            />
            <Button
              onClick={() => void handleCrossAsk()}
              loading={crossAsking}
              icon={<Send size={14} />}
              disabled={!selectedIds.length}
            >
              提问
            </Button>
          </div>
          <div style={{ fontSize: '0.6875rem', color: 'var(--text-tertiary)' }}>
            引用仅来自当前选中的有权知识库；撤权或撤回后，历史回答会同步隐藏。
          </div>
        </div>
      </Modal>
    </div>
  );
}

function GovTile({ value, label, warn = false }: { value: number; label: string; warn?: boolean }) {
  return (
    <div
      style={{
        minWidth: 108,
        padding: '10px 14px',
        borderRadius: 'var(--radius-md)',
        background: warn ? 'var(--warning-subtle)' : 'var(--fill)',
        border: '1px solid var(--line)',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 5, fontSize: '0.6875rem', color: 'var(--text-secondary)' }}>
        {warn && <AlertTriangle size={12} color="var(--warning-ink)" />}
        {label}
      </div>
      <div style={{ marginTop: 4, fontSize: '1.125rem', fontWeight: 700 }}>{value}</div>
    </div>
  );
}

function StatTile({ icon, value, label }: { icon: ReactNode; value: number; label: string }) {
  return (
    <div
      style={{
        minWidth: 92,
        padding: '12px 14px',
        borderRadius: 'var(--radius-lg)',
        background: 'var(--fill)',
        border: '1px solid var(--line)',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, color: 'var(--text-secondary)' }}>
        {icon}
        <span style={{ fontSize: '0.6875rem', whiteSpace: 'nowrap' }}>{label}</span>
      </div>
      <div style={{ marginTop: 6, fontSize: '1.25rem', fontWeight: 700, letterSpacing: '-0.02em' }}>
        {value}
      </div>
    </div>
  );
}

function FilterChip({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      style={{
        padding: '6px 12px',
        borderRadius: 999,
        border: '1px solid ' + (active ? 'var(--brand)' : 'var(--line)'),
        background: active ? 'var(--brand-subtle)' : 'var(--surface)',
        color: active ? 'var(--brand-ink)' : 'var(--text-secondary)',
        fontSize: '0.8125rem',
        fontWeight: active ? 600 : 500,
        cursor: 'pointer',
      }}
    >
      {children}
    </button>
  );
}

const ghostButton: CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 6,
  background: 'none',
  border: 'none',
  cursor: 'pointer',
  padding: '8px 12px',
  borderRadius: 'var(--radius-sm)',
  color: 'var(--text-secondary)',
  fontSize: '0.8125rem',
};
