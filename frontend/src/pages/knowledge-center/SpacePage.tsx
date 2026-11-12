import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { CSSProperties, ReactNode } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import {
  AlertTriangle,
  Archive,
  ArrowLeft,
  BookOpen,
  Crown,
  Eye,
  FileText,
  RefreshCw,
  Send,
  Settings2,
  Trash2,
  Undo2,
  UploadCloud,
  UserPlus,
  Users,
} from 'lucide-react';
import { useAuthStore } from '@/stores/auth';
import { useToastStore } from '@/stores/toast';
import { useKnowledgeBaseStore } from '@/stores/knowledgeBase';
import { api } from '@/api/client';
import { computeSha256 } from '@/lib/sha256';
import { coursesApi } from '@/api/domains/courses';
import type { CourseResponse } from '@/types/api';
import type {
  KnowledgeDocument,
  KnowledgeDocumentDetail,
  KnowledgeMember,
  KnowledgeMeta,
  KnowledgeRole,
  KnowledgeSessionDetail,
  KnowledgeSource,
  KnowledgeSpace,
  KnowledgeVersionContent,
  PolicyPreview,
  ReadScope,
  UploadSessionResult,
} from '@/api/domains/knowledgeBase';
import { Button, Input, Modal, Select } from '@/components/ui';

const ROLE_LABEL: Record<KnowledgeRole, string> = {
  owner: '负责人',
  publisher: '发布者',
  editor: '编辑者',
  reader: '阅读者',
};

type Tab = 'documents' | 'ask' | 'settings';

export default function KnowledgeSpacePage() {
  const { spaceId = '' } = useParams<{ spaceId: string }>();
  const navigate = useNavigate();
  const addToast = useToastStore((s) => s.addToast);
  const currentUser = useAuthStore((s) => s.user);
  const upsertSpace = useKnowledgeBaseStore((s) => s.upsert);

  const [space, setSpace] = useState<KnowledgeSpace | null>(null);
  const [documents, setDocuments] = useState<KnowledgeDocument[]>([]);
  const [members, setMembers] = useState<KnowledgeMember[]>([]);
  const [meta, setMeta] = useState<KnowledgeMeta | null>(null);
  const [tab, setTab] = useState<Tab>('documents');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // 共享范围调整：先预览，再显式确认
  const [pendingScope, setPendingScope] = useState<ReadScope | null>(null);
  const [preview, setPreview] = useState<PolicyPreview | null>(null);
  const [confirmScopeOpen, setConfirmScopeOpen] = useState(false);

  // 成员维护
  const [newMemberId, setNewMemberId] = useState('');
  const [newMemberRole, setNewMemberRole] = useState<KnowledgeRole>('reader');
  const [transferTarget, setTransferTarget] = useState('');
  const [transferOpen, setTransferOpen] = useState(false);
  const [archiveOpen, setArchiveOpen] = useState(false);

  // 资料上传与发布
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [uploadTitle, setUploadTitle] = useState('');
  const [uploadClassification, setUploadClassification] = useState<
    'shareable_teaching' | 'limited_teaching'
  >('shareable_teaching');
  const [uploading, setUploading] = useState(false);

  const [docDetail, setDocDetail] = useState<KnowledgeDocumentDetail | null>(null);
  const [docContent, setDocContent] = useState<KnowledgeVersionContent | null>(null);
  const [docLoading, setDocLoading] = useState(false);
  const [publishTarget, setPublishTarget] = useState<KnowledgeDocument | null>(null);
  const [withdrawTarget, setWithdrawTarget] = useState<KnowledgeDocument | null>(null);

  // 空间内问答
  const [qaSession, setQaSession] = useState<KnowledgeSessionDetail | null>(null);
  const [question, setQuestion] = useState('');
  const [asking, setAsking] = useState(false);

  // 导入课程
  const [courses, setCourses] = useState<CourseResponse[]>([]);
  const [importCourseId, setImportCourseId] = useState('');
  const [importing, setImporting] = useState(false);

  const isOwner = space?.role === 'owner';
  const canEdit = Boolean(space && space.role !== 'reader' && space.status === 'active');
  const canPublish = Boolean(
    space && (space.role === 'owner' || space.role === 'publisher') && space.status === 'active'
  );

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const detail = await api.knowledgeBase.getSpace(spaceId);
      setSpace(detail);
      upsertSpace(detail);
      const docs = await api.knowledgeBase.listDocuments(spaceId);
      setDocuments(docs);
      if (detail.role === 'owner') {
        setMembers(await api.knowledgeBase.listMembers(spaceId));
      } else {
        setMembers([]);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : '知识库加载失败');
    } finally {
      setLoading(false);
    }
  }, [spaceId, upsertSpace]);

  useEffect(() => {
    void refresh();
    api.knowledgeBase.meta().then(setMeta).catch(() => undefined);
  }, [refresh]);

  useEffect(() => {
    if (tab !== 'ask' || !space || qaSession) return;
    let cancelled = false;
    (async () => {
      try {
        const sessions = await api.knowledgeBase.listSessions();
        const existing = sessions.find(
          (item) =>
            item.selected_space_ids.length === 1 && item.selected_space_ids[0] === space.id
        );
        const detail = existing
          ? await api.knowledgeBase.getSession(existing.id)
          : await api.knowledgeBase.createSession([space.id], `${space.name}问答`);
        if (!cancelled) setQaSession(detail);
      } catch {
        /* 问答初始化失败时保持空态，用户可重试发送 */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [tab, space, qaSession]);

  useEffect(() => {
    if (!space?.allow_course_import) return;
    let cancelled = false;
    coursesApi
      .list()
      .then((items) => {
        if (!cancelled) {
          setCourses(items);
          setImportCourseId((current) => current || items[0]?.id || '');
        }
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [space?.allow_course_import]);

  const scopeOptions = useMemo(
    () =>
      meta?.read_scopes.map((item) => ({ value: item.value, label: item.label })) ?? [
        { value: 'members', label: '仅指定教师' },
        { value: 'organization', label: '单位内教师' },
        { value: 'school', label: '本校教师' },
      ],
    [meta]
  );

  const applyScope = async (next: ReadScope, confirmed: boolean) => {
    if (!space) return;
    setBusy(true);
    try {
      const updated = await api.knowledgeBase.updateSpace(space.id, {
        expected_policy_version: space.policy_version,
        read_scope: next,
        confirm_scope_expansion: confirmed,
      });
      setSpace(updated);
      upsertSpace(updated);
      addToast('读者范围已更新', 'success');
      setConfirmScopeOpen(false);
      setPreview(null);
      setPendingScope(null);
    } catch (err) {
      addToast(err instanceof Error ? err.message : '更新失败', 'error');
      await refresh();
    } finally {
      setBusy(false);
    }
  };

  const requestScopeChange = async (next: ReadScope) => {
    if (!space || next === space.read_scope) return;
    setPendingScope(next);
    setBusy(true);
    try {
      const impact = await api.knowledgeBase.previewPolicy(space.id, next);
      if (!impact.requires_confirmation) {
        setBusy(false);
        await applyScope(next, false);
        return;
      }
      setPreview(impact);
      setConfirmScopeOpen(true);
    } catch (err) {
      addToast(err instanceof Error ? err.message : '影响预览失败', 'error');
    } finally {
      setBusy(false);
    }
  };

  const handleAddMember = async () => {
    if (!space) return;
    const target = newMemberId.trim();
    if (!target) {
      addToast('请填写教师账号 ID', 'info');
      return;
    }
    setBusy(true);
    try {
      await api.knowledgeBase.upsertMember(space.id, target, newMemberRole);
      setNewMemberId('');
      addToast('成员已更新', 'success');
      await refresh();
    } catch (err) {
      addToast(err instanceof Error ? err.message : '添加成员失败', 'error');
    } finally {
      setBusy(false);
    }
  };

  const handleChangeRole = async (member: KnowledgeMember, role: KnowledgeRole) => {
    if (!space) return;
    setBusy(true);
    try {
      await api.knowledgeBase.upsertMember(space.id, member.user_id, role);
      await refresh();
      addToast('角色已更新', 'success');
    } catch (err) {
      addToast(err instanceof Error ? err.message : '更新角色失败', 'error');
    } finally {
      setBusy(false);
    }
  };

  const handleRemoveMember = async (member: KnowledgeMember) => {
    if (!space) return;
    setBusy(true);
    try {
      await api.knowledgeBase.removeMember(space.id, member.user_id);
      addToast('已移除成员', 'success');
      await refresh();
    } catch (err) {
      addToast(err instanceof Error ? err.message : '移除失败', 'error');
    } finally {
      setBusy(false);
    }
  };

  const handleTransfer = async () => {
    if (!space) return;
    const target = transferTarget.trim();
    if (!target) {
      addToast('请填写新负责人账号 ID', 'info');
      return;
    }
    setBusy(true);
    try {
      await api.knowledgeBase.transferOwner(space.id, target, space.policy_version);
      addToast('负责人已移交', 'success');
      setTransferOpen(false);
      setTransferTarget('');
      await refresh();
    } catch (err) {
      addToast(err instanceof Error ? err.message : '移交失败', 'error');
    } finally {
      setBusy(false);
    }
  };

  const handleArchive = async () => {
    if (!space) return;
    setBusy(true);
    try {
      await api.knowledgeBase.archiveSpace(space.id);
      addToast('知识库已归档', 'success');
      setArchiveOpen(false);
      await refresh();
    } catch (err) {
      addToast(err instanceof Error ? err.message : '归档失败', 'error');
    } finally {
      setBusy(false);
    }
  };

  const handleUpload = async () => {
    if (!space) return;
    if (!uploadFile) {
      addToast('请选择要上传的资料', 'info');
      return;
    }
    const mime = uploadFile.type || mimeFromName(uploadFile.name);
    if (!mime) {
      addToast('仅支持 Markdown、TXT、PDF 与 DOCX', 'error');
      return;
    }
    setUploading(true);
    try {
      const sha256 = await computeSha256(uploadFile);
      const session = await api.knowledgeBase.createUploadSession(space.id, {
        filename: uploadFile.name,
        size_bytes: uploadFile.size,
        mime_type: mime,
        sha256,
      });
      await putObject(session, uploadFile, sha256);
      const completed = await api.knowledgeBase.completeUpload(space.id, session.session_id, {
        title: uploadTitle.trim() || undefined,
        classification: uploadClassification,
      });
      addToast('已上传，正在解析', 'success');
      setUploadOpen(false);
      setUploadFile(null);
      setUploadTitle('');
      await refresh();
      // 正式链路：入队后由 knowledge 队列 worker 处理，页面只轮询状态。
      let state = 'queued';
      for (let attempt = 0; attempt < 8; attempt += 1) {
        await new Promise((resolve) => setTimeout(resolve, 1200));
        try {
          const job = await api.knowledgeBase.getJob(space.id, completed.job_id);
          state = job.state;
          if (state === 'succeeded' || state === 'failed') break;
        } catch {
          break;
        }
      }
      // 仅本地开发没有常驻 worker 时，才显式在本机执行一次。
      if ((state === 'queued' || state === 'pending') && import.meta.env.DEV) {
        try {
          await api.knowledgeBase.runJob(space.id, completed.job_id);
        } catch {
          /* 事件保持 pending，worker 启动后继续处理 */
        }
      }
      await refresh();
      if (state === 'failed') {
        addToast('解析失败，可查看文档状态后重试', 'error');
      }
    } catch (err) {
      addToast(err instanceof Error ? err.message : '上传失败', 'error');
    } finally {
      setUploading(false);
    }
  };

  const openDocument = async (doc: KnowledgeDocument) => {
    if (!space) return;
    setDocLoading(true);
    setDocDetail(null);
    setDocContent(null);
    try {
      const detail = await api.knowledgeBase.getDocument(space.id, doc.id);
      setDocDetail(detail);
      const version =
        detail.versions.find((item) => item.id === detail.published_version_id) ??
        detail.versions[0];
      if (version) {
        try {
          setDocContent(
            await api.knowledgeBase.getVersionContent(space.id, doc.id, version.id)
          );
        } catch {
          /* 解析未完成或无权预览正文 */
        }
      }
    } catch (err) {
      addToast(err instanceof Error ? err.message : '资料打开失败', 'error');
    } finally {
      setDocLoading(false);
    }
  };

  const handlePublish = async () => {
    if (!space || !publishTarget) return;
    setBusy(true);
    try {
      await api.knowledgeBase.publishDocument(space.id, publishTarget.id, {
        expected_access_version: publishTarget.access_version,
      });
      addToast('资料已发布', 'success');
      setPublishTarget(null);
      await refresh();
    } catch (err) {
      addToast(err instanceof Error ? err.message : '发布失败', 'error');
      await refresh();
    } finally {
      setBusy(false);
    }
  };

  const handleWithdraw = async () => {
    if (!space || !withdrawTarget) return;
    setBusy(true);
    try {
      await api.knowledgeBase.withdrawDocument(space.id, withdrawTarget.id);
      addToast('资料已撤回，读者立即不可见', 'success');
      setWithdrawTarget(null);
      await refresh();
    } catch (err) {
      addToast(err instanceof Error ? err.message : '撤回失败', 'error');
    } finally {
      setBusy(false);
    }
  };

  const handleAsk = async () => {
    if (!space) return;
    const text = question.trim();
    if (!text) return;
    setAsking(true);
    try {
      let current = qaSession;
      if (!current) {
        current = await api.knowledgeBase.createSession([space.id], `${space.name}问答`);
      }
      const updated = await api.knowledgeBase.ask(current.id, text);
      setQaSession(updated);
      setQuestion('');
    } catch (err) {
      addToast(err instanceof Error ? err.message : '提问失败', 'error');
    } finally {
      setAsking(false);
    }
  };

  const handleImport = async () => {
    if (!space || !docDetail || !importCourseId) {
      addToast('请先选择目标课程', 'info');
      return;
    }
    const version =
      docDetail.versions.find((item) => item.id === docDetail.published_version_id) ?? null;
    if (!version) {
      addToast('只有已发布版本可以导入课程', 'error');
      return;
    }
    setImporting(true);
    try {
      const result = await api.knowledgeBase.importToCourse(
        space.id,
        docDetail.id,
        version.id,
        { course_id: importCourseId }
      );
      addToast(
        result.idempotent ? '该版本已导入过，已返回原副本' : '已复制到课程，可继续原命题流程',
        'success'
      );
    } catch (err) {
      addToast(err instanceof Error ? err.message : '导入失败', 'error');
    } finally {
      setImporting(false);
    }
  };

  const openSource = async (source: KnowledgeSource) => {
    if (!space) return;
    setDocLoading(true);
    setDocDetail(null);
    setDocContent(null);
    try {
      setDocDetail(await api.knowledgeBase.getDocument(space.id, source.document_id));
      setDocContent(
        await api.knowledgeBase.getVersionContent(
          space.id,
          source.document_id,
          source.version_id
        )
      );
    } catch (err) {
      addToast(err instanceof Error ? err.message : '引用已不可访问', 'error');
    } finally {
      setDocLoading(false);
    }
  };

  if (loading && !space) {
    return (
      <div style={{ maxWidth: 1080, margin: '0 auto', padding: '48px 32px' }}>
        <div className="skeleton skeleton-title" />
        <div className="skeleton skeleton-card" style={{ marginTop: 20 }} />
      </div>
    );
  }

  if (error || !space) {
    return (
      <div style={{ maxWidth: 1080, margin: '0 auto', padding: '48px 32px' }}>
        <Button variant="secondary" icon={<ArrowLeft size={16} />} onClick={() => navigate('/knowledge')}>
          返回知识中心
        </Button>
        <div className="glass-panel" style={{ marginTop: 20, padding: 28 }}>
          <div style={{ color: 'var(--error-ink)', fontWeight: 600 }}>
            {error || '知识库不存在或无权访问'}
          </div>
          <Button
            variant="secondary"
            size="sm"
            icon={<RefreshCw size={14} />}
            style={{ marginTop: 12 }}
            onClick={() => void refresh()}
          >
            重试
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div style={{ minHeight: '100vh', background: 'var(--bg)' }}>
      <div style={{ maxWidth: 1080, margin: '0 auto', padding: '28px 32px 72px' }}>
        <button onClick={() => navigate('/knowledge')} style={backLink}>
          <ArrowLeft size={15} />
          共享知识中心
        </button>

        <header
          style={{
            marginTop: 16,
            display: 'flex',
            alignItems: 'flex-start',
            justifyContent: 'space-between',
            gap: 20,
            flexWrap: 'wrap',
          }}
        >
          <div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
              <h1 style={{ margin: 0, fontSize: '1.625rem', letterSpacing: '-0.02em' }}>{space.name}</h1>
              <span className="badge badge-accent">{ROLE_LABEL[space.role]}</span>
              {space.status === 'archived' && <span className="badge badge-warning">已归档</span>}
            </div>
            <div style={{ marginTop: 8, color: 'var(--text-secondary)', fontSize: '0.8125rem' }}>
              {space.org_name || '校内单位'} · {space.read_scope_label} · {space.document_count} 份已发布资料
            </div>
            {space.subject_tags.length > 0 && (
              <div style={{ marginTop: 10, display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                {space.subject_tags.map((tag) => (
                  <span key={tag} className="badge badge-default" style={{ fontSize: '0.6875rem' }}>
                    {tag}
                  </span>
                ))}
              </div>
            )}
          </div>
          <div style={{ display: 'flex', gap: 8 }}>
            <Button variant="secondary" size="sm" icon={<RefreshCw size={14} />} onClick={() => void refresh()}>
              刷新
            </Button>
            {canEdit && (
              <Button size="sm" icon={<UploadCloud size={14} />} onClick={() => setUploadOpen(true)}>
                上传资料
              </Button>
            )}
            {isOwner && space.status === 'active' && (
              <Button variant="secondary" size="sm" icon={<Archive size={14} />} onClick={() => setArchiveOpen(true)}>
                归档
              </Button>
            )}
          </div>
        </header>

        {/* Tabs */}
        <div
          style={{
            marginTop: 24,
            display: 'flex',
            gap: 6,
            borderBottom: '1px solid var(--line)',
            paddingBottom: 1,
          }}
        >
          <TabButton active={tab === 'documents'} onClick={() => setTab('documents')} icon={<FileText size={15} />}>
            资料
          </TabButton>
          <TabButton active={tab === 'ask'} onClick={() => setTab('ask')} icon={<BookOpen size={15} />}>
            问答
          </TabButton>
          {isOwner && (
            <TabButton active={tab === 'settings'} onClick={() => setTab('settings')} icon={<Settings2 size={15} />}>
              成员与权限
            </TabButton>
          )}
        </div>

        {tab === 'documents' ? (
          <DocumentsPanel
            documents={documents}
            canEdit={canEdit}
            canPublish={canPublish}
            onView={(doc) => void openDocument(doc)}
            onPublish={setPublishTarget}
            onWithdraw={setWithdrawTarget}
          />
        ) : tab === 'ask' ? (
          <QaPanel
            session={qaSession}
            question={question}
            asking={asking}
            onQuestionChange={setQuestion}
            onAsk={() => void handleAsk()}
            onSource={(source) => void openSource(source)}
          />
        ) : (
          <div style={{ marginTop: 22, display: 'flex', flexDirection: 'column', gap: 22 }}>
            {/* 共享范围 */}
            <section className="glass-panel" style={{ padding: '22px 24px' }}>
              <SectionTitle icon={<Users size={17} />} title="共享范围" />
              <p style={{ margin: '6px 0 16px', color: 'var(--text-secondary)', fontSize: '0.8125rem', lineHeight: 1.7 }}>
                成员角色优先于范围读者。扩大范围会立即影响现有已发布资料，
                因此需要先查看影响预览并确认；限指定教师资料不会被自动放开。
              </p>
              <div style={{ display: 'flex', alignItems: 'flex-end', gap: 12, flexWrap: 'wrap' }}>
                <div style={{ minWidth: 220 }}>
                  <Select
                    label="读者范围"
                    value={pendingScope ?? space.read_scope}
                    options={scopeOptions}
                    onChange={(e) => void requestScopeChange(e.target.value as ReadScope)}
                    disabled={busy || space.status === 'archived'}
                  />
                </div>
                <div style={{ fontSize: '0.75rem', color: 'var(--text-tertiary)', paddingBottom: 10 }}>
                  策略版本 v{space.policy_version}
                </div>
              </div>
            </section>

            {/* 成员 */}
            <section className="glass-panel" style={{ padding: '22px 24px' }}>
              <SectionTitle icon={<Users size={17} />} title="成员" />
              <div style={{ display: 'flex', gap: 10, margin: '14px 0 18px', flexWrap: 'wrap' }}>
                <div style={{ flex: '1 1 220px' }}>
                  <Input
                    placeholder="教师账号 ID"
                    value={newMemberId}
                    onChange={(e) => setNewMemberId(e.target.value)}
                  />
                </div>
                <div style={{ width: 150 }}>
                  <Select
                    value={newMemberRole}
                    options={[
                      { value: 'reader', label: '阅读者' },
                      { value: 'editor', label: '编辑者' },
                      { value: 'publisher', label: '发布者' },
                    ]}
                    onChange={(e) => setNewMemberRole(e.target.value as KnowledgeRole)}
                  />
                </div>
                <Button icon={<UserPlus size={15} />} onClick={() => void handleAddMember()} loading={busy}>
                  添加
                </Button>
              </div>

              <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                {members.map((member) => {
                  const isSelf = member.user_id === currentUser?.id;
                  const isSpaceOwner = member.role === 'owner';
                  return (
                    <div
                      key={member.user_id}
                      style={{
                        display: 'flex',
                        alignItems: 'center',
                        gap: 12,
                        padding: '10px 12px',
                        border: '1px solid var(--line)',
                        borderRadius: 'var(--radius-md)',
                        background: 'var(--surface)',
                        flexWrap: 'wrap',
                      }}
                    >
                      <div
                        style={{
                          width: 32,
                          height: 32,
                          borderRadius: '50%',
                          background: 'var(--fill-strong)',
                          display: 'flex',
                          alignItems: 'center',
                          justifyContent: 'center',
                          color: 'var(--text-secondary)',
                          fontSize: '0.75rem',
                          fontWeight: 700,
                        }}
                      >
                        {(member.name || member.username || member.user_id).slice(0, 1).toUpperCase()}
                      </div>
                      <div style={{ flex: 1, minWidth: 140 }}>
                        <div style={{ fontSize: '0.875rem', fontWeight: 600 }}>
                          {member.name || member.username || member.user_id}
                          {isSelf && (
                            <span style={{ marginLeft: 6, color: 'var(--text-tertiary)', fontWeight: 400 }}>
                              （我）
                            </span>
                          )}
                        </div>
                        <div style={{ fontSize: '0.6875rem', color: 'var(--text-tertiary)' }}>{member.user_id}</div>
                      </div>
                      {isSpaceOwner ? (
                        <span className="badge badge-warning" style={{ display: 'inline-flex', gap: 4 }}>
                          <Crown size={12} />
                          负责人
                        </span>
                      ) : (
                        <div style={{ width: 130 }}>
                          <Select
                            value={member.role}
                            options={[
                              { value: 'reader', label: '阅读者' },
                              { value: 'editor', label: '编辑者' },
                              { value: 'publisher', label: '发布者' },
                            ]}
                            onChange={(e) => void handleChangeRole(member, e.target.value as KnowledgeRole)}
                            disabled={busy}
                          />
                        </div>
                      )}
                      {!isSpaceOwner && (
                        <button
                          onClick={() => void handleRemoveMember(member)}
                          title="移除成员"
                          style={iconButton}
                          disabled={busy}
                        >
                          <Trash2 size={15} />
                        </button>
                      )}
                    </div>
                  );
                })}
                {!members.length && (
                  <div style={{ color: 'var(--text-tertiary)', fontSize: '0.8125rem', padding: '6px 0' }}>
                    还没有显式成员；范围读者仍可通过共享范围访问。
                  </div>
                )}
              </div>

              <div style={{ marginTop: 18, paddingTop: 16, borderTop: '1px solid var(--line)' }}>
                <div style={{ fontSize: '0.8125rem', fontWeight: 600, marginBottom: 8 }}>移交负责人</div>
                <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
                  <div style={{ flex: '1 1 220px' }}>
                    <Input
                      placeholder="新负责人账号 ID"
                      value={transferTarget}
                      onChange={(e) => setTransferTarget(e.target.value)}
                    />
                  </div>
                  <Button
                    variant="secondary"
                    icon={<Crown size={15} />}
                    onClick={() => setTransferOpen(true)}
                    disabled={space.status === 'archived'}
                  >
                    移交
                  </Button>
                </div>
              </div>
            </section>
          </div>
        )}
      </div>

      {/* 范围影响确认 */}
      <Modal
        open={confirmScopeOpen}
        onClose={() => (busy ? undefined : setConfirmScopeOpen(false))}
        title="确认扩大共享范围"
        onConfirm={() => {
          if (pendingScope) void applyScope(pendingScope, true);
        }}
        confirmLabel="确认扩大"
        loading={busy}
      >
        {preview && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
            <div style={{ display: 'flex', gap: 10, padding: 12, borderRadius: 'var(--radius-md)', background: 'var(--warning-subtle)' }}>
              <AlertTriangle size={18} color="var(--warning-ink)" style={{ flexShrink: 0, marginTop: 1 }} />
              <div style={{ fontSize: '0.8125rem', color: 'var(--warning-ink)', lineHeight: 1.6 }}>
                调整后，下列已发布资料将对更多教师可见。限指定教师资料不会自动放开。
              </div>
            </div>
            <div style={{ fontSize: '0.8125rem', color: 'var(--text-secondary)' }}>
              预计校内有效账号：{preview.active_school_user_count} 个
            </div>
            <div style={{ maxHeight: 220, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 6 }}>
              {preview.newly_visible_documents.map((doc) => (
                <div
                  key={doc.document_id}
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'space-between',
                    gap: 10,
                    padding: '8px 10px',
                    border: '1px solid var(--line)',
                    borderRadius: 'var(--radius-sm)',
                    fontSize: '0.8125rem',
                  }}
                >
                  <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
                    <BookOpen size={14} />
                    {doc.title}
                  </span>
                  <span className="badge badge-default" style={{ fontSize: '0.6875rem' }}>
                    {doc.classification === 'limited_teaching' ? '限指定教师' : '可共享'}
                  </span>
                </div>
              ))}
              {!preview.newly_visible_documents.length && (
                <div style={{ color: 'var(--text-tertiary)', fontSize: '0.8125rem' }}>
                  当前没有已发布资料，扩大范围暂时不会新增可见内容。
                </div>
              )}
            </div>
            {preview.notes.map((note) => (
              <div key={note} style={{ fontSize: '0.75rem', color: 'var(--text-tertiary)' }}>
                · {note}
              </div>
            ))}
          </div>
        )}
      </Modal>

      {/* 移交确认 */}
      <Modal
        open={transferOpen}
        onClose={() => (busy ? undefined : setTransferOpen(false))}
        title="确认移交负责人"
        onConfirm={handleTransfer}
        confirmLabel="确认移交"
        loading={busy}
      >
        <div style={{ fontSize: '0.875rem', lineHeight: 1.8, color: 'var(--text-secondary)' }}>
          移交后你将降为发布者，不能再管理成员与共享范围。
          目标账号：<strong style={{ color: 'var(--text)' }}>{transferTarget || '（未填写）'}</strong>
        </div>
      </Modal>

      {/* 归档确认 */}
      <Modal
        open={archiveOpen}
        onClose={() => (busy ? undefined : setArchiveOpen(false))}
        title="归档知识库"
        onConfirm={handleArchive}
        confirmLabel="归档"
        loading={busy}
        danger
      >
        <div style={{ fontSize: '0.875rem', lineHeight: 1.8, color: 'var(--text-secondary)' }}>
          归档后普通读者与成员将不再看到该库资料，负责人仍可查看。
          此操作不会删除任何文件或历史版本。
        </div>
      </Modal>

      {/* 上传资料 */}
      <Modal
        open={uploadOpen}
        onClose={() => (uploading ? undefined : setUploadOpen(false))}
        title="上传共享资料"
        onConfirm={handleUpload}
        confirmLabel="上传并解析"
        loading={uploading}
      >
        <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
          <div
            onClick={() => fileInputRef.current?.click()}
            style={{
              border: '1px dashed var(--line-strong)',
              borderRadius: 'var(--radius-md)',
              padding: '22px 16px',
              textAlign: 'center',
              cursor: 'pointer',
              background: 'var(--fill)',
            }}
          >
            <UploadCloud size={24} color="var(--brand-ink)" />
            <div style={{ marginTop: 8, fontSize: '0.875rem', fontWeight: 600 }}>
              {uploadFile ? uploadFile.name : '选择 Markdown / TXT / PDF / DOCX'}
            </div>
            <div style={{ marginTop: 4, fontSize: '0.75rem', color: 'var(--text-tertiary)' }}>
              {uploadFile ? formatSize(uploadFile.size) : '上传后自动解析章节与页码'}
            </div>
            <input
              ref={fileInputRef}
              type="file"
              accept=".md,.markdown,.txt,.pdf,.docx"
              style={{ display: 'none' }}
              onChange={(e) => setUploadFile(e.target.files?.[0] ?? null)}
            />
          </div>
          <Input
            label="资料标题（可选）"
            placeholder="默认使用文件名"
            value={uploadTitle}
            onChange={(e) => setUploadTitle(e.target.value)}
          />
          <Select
            label="资料分类"
            value={uploadClassification}
            options={[
              { value: 'shareable_teaching', label: '可共享教学资料' },
              { value: 'limited_teaching', label: '限指定教师资料' },
            ]}
            onChange={(e) =>
              setUploadClassification(
                e.target.value as 'shareable_teaching' | 'limited_teaching'
              )
            }
          />
          <div style={{ fontSize: '0.75rem', color: 'var(--text-tertiary)', lineHeight: 1.7 }}>
            未公开试卷、答案细则等考试保密材料禁止上传；限指定教师资料只能放在仅成员可读的知识库。
          </div>
        </div>
      </Modal>

      {/* 资料正文预览 */}
      <Modal
        open={docLoading || Boolean(docDetail)}
        onClose={() => {
          if (docLoading) return;
          setDocDetail(null);
          setDocContent(null);
        }}
        title={docDetail?.title || '资料正文'}
        confirmLabel="关闭"
        onConfirm={() => {
          setDocDetail(null);
          setDocContent(null);
        }}
        loading={docLoading}
      >
        {docDetail && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
              <span className="badge badge-default">
                {docDetail.classification === 'limited_teaching' ? '限指定教师' : '可共享教学资料'}
              </span>
              <span className={`badge ${docDetail.status === 'published' ? 'badge-success' : 'badge-default'}`}>
                {docDetail.status === 'published' ? '已发布' : docDetail.status}
              </span>
              {docContent && (
                <span style={{ fontSize: '0.75rem', color: 'var(--text-tertiary)' }}>
                  v{docContent.version_no} · {docContent.blocks.length} 个内容块
                </span>
              )}
            </div>
            <div style={{ maxHeight: 420, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 10 }}>
              {docContent?.blocks.map((block) => (
                <div
                  key={block.block_index}
                  style={{
                    padding: '10px 12px',
                    border: '1px solid var(--line)',
                    borderRadius: 'var(--radius-sm)',
                    background: 'var(--surface)',
                  }}
                >
                  {block.heading_path.length > 0 && (
                    <div style={{ fontSize: '0.6875rem', color: 'var(--brand-ink)', marginBottom: 4 }}>
                      {block.heading_path.join(' / ')}
                      {block.page_index != null ? ` · 第 ${block.page_index + 1} 页` : ''}
                    </div>
                  )}
                  <div style={{ fontSize: '0.8125rem', lineHeight: 1.75, whiteSpace: 'pre-wrap' }}>
                    {block.text}
                  </div>
                </div>
              ))}
              {!docContent && (
                <div style={{ fontSize: '0.8125rem', color: 'var(--text-tertiary)' }}>
                  解析尚未完成，完成后再查看正文。
                </div>
              )}
            </div>
            {space?.allow_course_import && docDetail.status === 'published' && courses.length > 0 && (
              <div
                style={{
                  borderTop: '1px solid var(--line)',
                  paddingTop: 12,
                  display: 'flex',
                  gap: 10,
                  alignItems: 'flex-end',
                  flexWrap: 'wrap',
                }}
              >
                <div style={{ flex: 1, minWidth: 200 }}>
                  <Select
                    label="导入我的课程"
                    value={importCourseId}
                    options={courses.map((course) => ({
                      value: course.id,
                      label: course.name,
                    }))}
                    onChange={(e) => setImportCourseId(e.target.value)}
                  />
                </div>
                <Button
                  variant="secondary"
                  size="sm"
                  loading={importing}
                  onClick={() => void handleImport()}
                >
                  复制固定版本
                </Button>
                <div style={{ width: '100%', fontSize: '0.6875rem', color: 'var(--text-tertiary)', lineHeight: 1.7 }}>
                  导入为课程独立副本；知识库后续撤回或更新不会改动已复制的版本。
                </div>
              </div>
            )}
          </div>
        )}
      </Modal>

      {/* 发布确认 */}
      <Modal
        open={Boolean(publishTarget)}
        onClose={() => (busy ? undefined : setPublishTarget(null))}
        title="发布资料"
        onConfirm={handlePublish}
        confirmLabel="发布给读者"
        loading={busy}
      >
        <div style={{ fontSize: '0.875rem', lineHeight: 1.8, color: 'var(--text-secondary)' }}>
          发布后，当前共享范围内的教师可以检索、阅读并引用该资料的固定版本。
          后续修改需创建新版本再发布，历史版本权限不会随范围扩大自动放开。
        </div>
      </Modal>

      {/* 撤回确认 */}
      <Modal
        open={Boolean(withdrawTarget)}
        onClose={() => (busy ? undefined : setWithdrawTarget(null))}
        title="撤回资料"
        onConfirm={handleWithdraw}
        confirmLabel="确认撤回"
        loading={busy}
        danger
      >
        <div style={{ fontSize: '0.875rem', lineHeight: 1.8, color: 'var(--text-secondary)' }}>
          撤回后普通读者立即不可再检索或读取该资料，已存在的历史引用也将失效。
          已由读者复制到自己课程的内容无法自动收回。
        </div>
      </Modal>
    </div>
  );
}

function QaPanel({
  session,
  question,
  asking,
  onQuestionChange,
  onAsk,
  onSource,
}: {
  session: KnowledgeSessionDetail | null;
  question: string;
  asking: boolean;
  onQuestionChange: (value: string) => void;
  onAsk: () => void;
  onSource: (source: KnowledgeSource) => void;
}) {
  const messages = session?.messages ?? [];
  return (
    <div className="glass-panel" style={{ marginTop: 22, padding: '20px 22px' }}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 14, minHeight: 220, maxHeight: 520, overflowY: 'auto' }}>
        {messages.length === 0 && (
          <div style={{ padding: '28px 4px', textAlign: 'center', color: 'var(--text-tertiary)', fontSize: '0.8125rem' }}>
            基于当前知识库已发布资料回答，并给出可点击的固定版本引用。
          </div>
        )}
        {messages.map((message) => (
          <div
            key={message.id}
            style={{
              alignSelf: message.role === 'user' ? 'flex-end' : 'flex-start',
              maxWidth: '82%',
              background: message.role === 'user' ? 'var(--brand-subtle)' : 'var(--fill)',
              color: 'var(--text-primary)',
              borderRadius: 'var(--radius-md)',
              padding: '12px 14px',
            }}
          >
            <div style={{ fontSize: '0.8125rem', lineHeight: 1.8, whiteSpace: 'pre-wrap' }}>
              {message.content}
            </div>
            {message.sources.length > 0 && (
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 10 }}>
                {message.sources.map((source) => (
                  <button
                    key={source.source_id}
                    onClick={() => onSource(source)}
                    title={source.quote}
                    style={{
                      border: '1px solid var(--line-strong)',
                      background: 'var(--surface)',
                      borderRadius: 999,
                      padding: '4px 10px',
                      fontSize: '0.6875rem',
                      cursor: 'pointer',
                      color: 'var(--brand-ink)',
                    }}
                  >
                    [{source.source_id}] {source.document_title} · v{source.version_no}
                    {source.page_index != null ? ` · 第 ${source.page_index + 1} 页` : ''}
                  </button>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>
      <div style={{ display: 'flex', gap: 10, marginTop: 16 }}>
        <Input
          placeholder="向当前知识库提问，例如：栈和队列的区别是什么？"
          value={question}
          onChange={(e) => onQuestionChange(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) onAsk();
          }}
        />
        <Button onClick={onAsk} loading={asking} icon={<Send size={14} />}>
          提问
        </Button>
      </div>
      <div style={{ marginTop: 8, fontSize: '0.6875rem', color: 'var(--text-tertiary)' }}>
        仅检索你有权访问的已发布资料；撤回资料后，历史回答会同步隐藏。
      </div>
    </div>
  );
}

function DocumentsPanel({
  documents,
  canEdit,
  canPublish,
  onView,
  onPublish,
  onWithdraw,
}: {
  documents: KnowledgeDocument[];
  canEdit: boolean;
  canPublish: boolean;
  onView: (doc: KnowledgeDocument) => void;
  onPublish: (doc: KnowledgeDocument) => void;
  onWithdraw: (doc: KnowledgeDocument) => void;
}) {
  if (!documents.length) {
    return (
      <div
        className="glass-panel"
        style={{
          marginTop: 22,
          padding: '52px 24px',
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          textAlign: 'center',
          gap: 10,
        }}
      >
        <span
          style={{
            width: 54,
            height: 54,
            borderRadius: 18,
            background: 'var(--fill)',
            color: 'var(--text-secondary)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
          }}
        >
          <FileText size={24} />
        </span>
        <div style={{ fontSize: '1rem', fontWeight: 650 }}>
          {canEdit ? '还没有资料' : '还没有已发布资料'}
        </div>
        <div style={{ fontSize: '0.8125rem', color: 'var(--text-secondary)', maxWidth: 460 }}>
          {canEdit
            ? '上传教学资料后会自动解析章节与页码，确认预览无误后再发布给共享范围内的教师。'
            : '维护者发布资料后，你可以在这里检索、阅读并引用固定版本。'}
        </div>
      </div>
    );
  }

  return (
    <div className="glass-panel" style={{ marginTop: 22, overflow: 'hidden' }}>
      {documents.map((doc, index) => {
        const isPublished = doc.status === 'published';
        const parseState = doc.version ? undefined : doc.status;
        return (
          <div
            key={doc.id}
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 14,
              padding: '14px 20px',
              borderTop: index === 0 ? 'none' : '1px solid var(--line)',
              flexWrap: 'wrap',
            }}
          >
            <span
              style={{
                width: 36,
                height: 36,
                borderRadius: 10,
                background: 'var(--brand-subtle)',
                color: 'var(--brand-ink)',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                flexShrink: 0,
              }}
            >
              <FileText size={17} />
            </span>
            <button
              onClick={() => onView(doc)}
              style={{
                flex: 1,
                minWidth: 160,
                textAlign: 'left',
                border: 'none',
                background: 'transparent',
                cursor: 'pointer',
                padding: 0,
              }}
            >
              <div style={{ fontSize: '0.9375rem', fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {doc.title}
              </div>
              <div style={{ fontSize: '0.6875rem', color: 'var(--text-tertiary)', marginTop: 3 }}>
                {doc.version
                  ? `v${doc.version.version_no} · ${formatSize(doc.version.size_bytes)}`
                  : `解析状态：${STATUS_LABEL[parseState || doc.status] || doc.status}`}
                {doc.version?.published_at ? ` · 发布于 ${formatDate(doc.version.published_at)}` : ''}
              </div>
            </button>
            <span className="badge badge-default" style={{ flexShrink: 0 }}>
              {doc.classification === 'limited_teaching' ? '限指定教师' : '可共享教学资料'}
            </span>
            <span className={`badge ${isPublished ? 'badge-success' : 'badge-default'}`} style={{ flexShrink: 0 }}>
              {STATUS_LABEL[doc.status] || doc.status}
            </span>
            <div style={{ display: 'flex', gap: 6, flexShrink: 0 }}>
              <button onClick={() => onView(doc)} title="阅读正文" style={iconButton}>
                <Eye size={15} />
              </button>
              {canPublish && !isPublished && doc.status === 'ready' && (
                <button onClick={() => onPublish(doc)} title="发布给读者" style={iconButton}>
                  <Send size={15} />
                </button>
              )}
              {canPublish && isPublished && (
                <button onClick={() => onWithdraw(doc)} title="撤回资料" style={iconButton}>
                  <Undo2 size={15} />
                </button>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function SectionTitle({ icon, title }: { icon: ReactNode; title: string }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: '0.9375rem', fontWeight: 650 }}>
      <span style={{ color: 'var(--brand-ink)' }}>{icon}</span>
      {title}
    </div>
  );
}

function TabButton({
  active,
  onClick,
  icon,
  children,
}: {
  active: boolean;
  onClick: () => void;
  icon: ReactNode;
  children: ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 6,
        padding: '9px 14px',
        border: 'none',
        background: 'transparent',
        color: active ? 'var(--brand-ink)' : 'var(--text-secondary)',
        fontSize: '0.875rem',
        fontWeight: active ? 650 : 500,
        cursor: 'pointer',
        borderBottom: active ? '2px solid var(--brand)' : '2px solid transparent',
        marginBottom: -1,
      }}
    >
      {icon}
      {children}
    </button>
  );
}

function formatSize(bytes: number): string {
  if (!bytes) return '0 B';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function formatDate(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(
    date.getDate()
  ).padStart(2, '0')}`;
}

const STATUS_LABEL: Record<string, string> = {
  draft: '草稿',
  processing: '解析中',
  ready: '待发布',
  published: '已发布',
  superseded: '已被新版替换',
  withdrawn: '已撤回',
  rejected: '已拒绝',
};

/** 未配置 S3 时后端返回本地存储路径，需要带登录态走统一请求。 */
async function putObject(
  session: UploadSessionResult,
  file: File,
  sha256: string
): Promise<void> {
  if (session.upload_url.includes('/_local-storage/')) {
    await api.uploadBinary('/_local-storage/' + session.object_key, file);
    return;
  }
  const response = await fetch(session.upload_url, {
    method: 'PUT',
    body: file,
    headers: {
      'Content-Type': file.type || 'application/octet-stream',
      'x-amz-meta-sha256': sha256,
      ...session.headers,
    },
  });
  if (!response.ok) {
    throw new Error('对象上传失败(' + response.status + ')');
  }
}

function mimeFromName(filename: string): string | null {
  const lower = filename.toLowerCase();
  if (lower.endsWith('.md') || lower.endsWith('.markdown')) return 'text/markdown';
  if (lower.endsWith('.txt')) return 'text/plain';
  if (lower.endsWith('.pdf')) return 'application/pdf';
  if (lower.endsWith('.docx')) {
    return 'application/vnd.openxmlformats-officedocument.wordprocessingml.document';
  }
  return null;
}

const backLink: CSSProperties = {
  display: 'inline-flex',
  alignItems: 'center',
  gap: 6,
  border: 'none',
  background: 'transparent',
  color: 'var(--text-secondary)',
  fontSize: '0.8125rem',
  cursor: 'pointer',
  padding: '4px 0',
};

const iconButton: CSSProperties = {
  width: 32,
  height: 32,
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  border: '1px solid var(--line)',
  borderRadius: 'var(--radius-sm)',
  background: 'var(--surface)',
  color: 'var(--text-secondary)',
  cursor: 'pointer',
};
