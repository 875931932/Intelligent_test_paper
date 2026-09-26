import { useEffect, useMemo, useRef, useState } from 'react';
import { ExternalLink, Plus } from 'lucide-react';
import { api } from '@/api/client';
import { getErrorMessage } from '@/api/errors';
import { useAuthStore } from '@/stores/auth';
import { useToastStore } from '@/stores/toast';
import { Button } from '@/components/ui/Button';
import { Badge } from '@/components/ui/Badge';
import { Modal, FloatingPanel } from '@/components/ui';
import { useNameMaps } from '@/hooks/useNameMaps';
import { QUESTION_TYPE_ORDER, sectionLabel } from '@/lib/examDisplay';
import type { ExamProject, PaperVersion, PaperVersionItem } from '@/types/api';
import { AiCreatePanel } from './AiCreatePanel';
import { AiRevisePanel } from './AiRevisePanel';
import { PaperReviewPanel } from './PaperReviewPanel';
import {
  type EditorSubmit, type ExportKind, type PreviewKind,
  PREVIEW_TABS, draftFromProposal, emptyDraft, normalizeAnswer,
} from './questionShared';
import { QuestionEditor, type QuestionEditorHandle } from './QuestionEditor';
import { QuestionIndex } from './QuestionIndex';
import { QuestionDetail } from './QuestionDetail';
import { PaperProfile } from './PaperProfile';

// ═══════════════════════════════════════════════
//  试卷面板（双栏阅读器）
// ═══════════════════════════════════════════════
export default function PaperPanel({
  pv, project, courseId, onChanged, onRegenerate,
}: {
  pv: PaperVersion;
  project?: ExamProject;
  courseId: string;
  /** 增删改后通知父级刷新试卷与项目摘要 */
  onChanged: () => void;
  /** 请求重新生成：父级切到流水线生成阶段 */
  onRegenerate: () => void;
}) {
  const token = useAuthStore((s) => s.token);
  const addToast = useToastStore((s) => s.addToast);
  const { maps, reload: reloadMaps } = useNameMaps(courseId);

  const questions = pv.questions;
  const readonly = pv.status === 'finalized';

  const [selected, setSelected] = useState<number>(() => questions[0]?.item_index ?? 0);
  const [editing, setEditing] = useState(false);
  const [onlyNeedsReview, setOnlyNeedsReview] = useState(false);
  const [saving, setSaving] = useState(false);
  const [adding, setAdding] = useState(false);
  const [addOpen, setAddOpen] = useState(false);
  const [finalizeOpen, setFinalizeOpen] = useState(false);
  // 单题 AI 改题面板的展开状态：一次会话属于一道题，切题即收起
  const [aiOpen, setAiOpen] = useState(false);
  const [reviewOpen, setReviewOpen] = useState(false);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [previewKind, setPreviewKind] = useState<PreviewKind>('student');
  // 整体预览的带鉴权 blob object URL（作 iframe src）；加载中/失败时为 null
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  // 右栏编辑器草稿是否有未保存改动：切题/翻题/换序都会让编辑器随 key 重挂载、
  // 草稿蒸发，靠它在这些动作前拦一次确认。
  const [dirty, setDirty] = useState(false);
  const addEditorRef = useRef<QuestionEditorHandle | null>(null);

  // QuestionEditor 未挂载时不主动清脏标记：保存成功后由 handleSave 归零，
  // 否则「刚保存完就切题」仍会被自己拦住。
  const reportDirty = (v: boolean) => setDirty(v);

  /** 放行前确认丢弃未保存草稿；返回 false 表示教师选择留下 */
  const guardDirty = (): boolean => {
    if (!editing || !dirty) return true;
    const ok = window.confirm('当前题目的修改尚未保存，切换将丢弃这些改动。仍要切换吗？');
    if (ok) setDirty(false);
    return ok;
  };

  /** 定稿提示里的题号 chip：关弹窗、退出筛选、落到该题 */
  const jumpTo = (idx: number) => {
    setOnlyNeedsReview(false);
    setEditing(false);
    setDirty(false);
    setSelected(idx);
    setFinalizeOpen(false);
  };

  useEffect(() => {
    void reloadMaps();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [courseId]);

  // 试卷刷新后保持选中项；原选中题被删则落到最近的位置——删的是最后一题就
  // 落到新的最后一题（即原前一题），否则一律落到第一题。
  useEffect(() => {
    if (questions.length > 0 && !questions.some((q) => q.item_index === selected)) {
      const last = questions[questions.length - 1].item_index;
      setSelected(selected > last ? last : questions[0].item_index);
      setEditing(false);
      setDirty(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pv.id, questions.length]);

  // 换题即收起 AI 改题面板：提案与 diff 都是单题会话，留在新题上会误导
  useEffect(() => {
    setAiOpen(false);
  }, [selected]);

  // 分组（按题型分节，节内保持卷面顺序）
  const groups = useMemo(() => {
    const filtered = onlyNeedsReview
      ? questions.filter((q) => q.needs_review || q.needs_review_reason)
      : questions;
    const byType = new Map<string, PaperVersionItem[]>();
    filtered.forEach((q) => {
      const arr = byType.get(q.question_type) ?? [];
      arr.push(q);
      byType.set(q.question_type, arr);
    });
    return [...byType.entries()]
      .sort((a, b) => QUESTION_TYPE_ORDER.indexOf(a[0]) - QUESTION_TYPE_ORDER.indexOf(b[0]))
      .map(([t, items]) => ({ key: t, label: sectionLabel(t), items }));
  }, [questions, onlyNeedsReview]);

  // 上一题/下一题与键盘导航一律按「卷面顺序」走（不是左栏的分组顺序），
  // 否则从简答下一题会跳到另一节的简答，看着像漏了一题。
  const navList = useMemo(
    () => (onlyNeedsReview ? questions.filter((q) => q.needs_review || q.needs_review_reason) : questions),
    [questions, onlyNeedsReview],
  );
  const navIdx = navList.findIndex((q) => q.item_index === selected);
  // 右栏只渲染可见（未被筛选掉）的题目
  const current = navIdx >= 0 ? navList[navIdx] : undefined;
  const pendingItems = useMemo(
    () => questions.filter((q) => q.needs_review || q.needs_review_reason),
    [questions],
  );
  // 缺答案的题导出答卷时会标「缺答案」：定稿前必须让教师知道，
  // 否则定稿后才发现，只能撤销定稿再补。
  const missingItems = useMemo(
    () => questions.filter((q) => !normalizeAnswer(q.answer)),
    [questions],
  );

  const step = (dir: -1 | 1) => {
    const next = navList[navIdx + dir];
    if (!next || !guardDirty()) return;
    setSelected(next.item_index);
    setEditing(false);
  };

  // 打开「仅看待审核」时，若当前题被滤掉就跳到第一道待审题，避免右栏空着
  const toggleFilter = () => {
    if (!guardDirty()) return;
    const next = !onlyNeedsReview;
    setOnlyNeedsReview(next);
    setEditing(false);
    if (next) {
      const first = questions.find((q) => q.needs_review || q.needs_review_reason);
      if (first) setSelected(first.item_index);
    }
  };

  // 键盘 ↑/↓ 翻题：焦点在输入框内或正在编辑时不抢占
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (editing || addOpen || finalizeOpen) return;
      const t = e.target as HTMLElement | null;
      if (t && /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName)) return;
      if (e.key === 'ArrowUp') { e.preventDefault(); step(-1); }
      if (e.key === 'ArrowDown') { e.preventDefault(); step(1); }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [navList, navIdx, editing, dirty, addOpen, finalizeOpen]);

  // ── 编辑操作 ──

  const handleSave = async (idx: number, v: EditorSubmit) => {
    const { clear_needs_review, ...patch } = v;
    setSaving(true);
    try {
      await api.paperVersions.patchItem(courseId, pv.id, idx, {
        teacher_override_patch: patch,
        clear_needs_review,
      });
      addToast(`第 ${idx} 题已保存`, 'success');
      setEditing(false);
      setDirty(false);
      onChanged();
    } catch (e) {
      addToast('保存失败: ' + getErrorMessage(e), 'error');
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (idx: number) => {
    if (!window.confirm(`确认删除第 ${idx} 题？删除后其后的题目题号会前移。`)) return;
    setSaving(true);
    try {
      await api.paperVersions.deleteItem(courseId, pv.id, idx, token ?? undefined);
      addToast('题目已删除', 'success');
      setEditing(false);
      setDirty(false);
      onChanged();
    } catch (e) {
      addToast('删除失败: ' + getErrorMessage(e), 'error');
    } finally {
      setSaving(false);
    }
  };

  const handleMove = async (pos: number, dir: -1 | 1) => {
    // 换序同样会让编辑器重挂载（item_index 变了、key 变了），先过一遍脏检查
    if (!guardDirty()) return;
    const ordered = questions.map((q) => q.item_index);
    const newPos = pos + dir;
    if (newPos < 0 || newPos >= ordered.length) return;
    const movedIndex = ordered[pos];
    const tmp = ordered[pos];
    ordered[pos] = ordered[newPos];
    ordered[newPos] = tmp;
    setSaving(true);
    try {
      await api.paperVersions.reorderItems(courseId, pv.id, ordered, token ?? undefined);
      // display_order 会被后端重排成 1..N：被移动的题从原题号变成 newPos+1。
      // 不跟着改 selected 的话，右栏会停在同一个题号上、内容却换成了另一道题，
      // 此时若仍处编辑态，保存会把 A 题的草稿写进 B 题的槽位。
      if (movedIndex !== newPos + 1) {
        setSelected(newPos + 1);
        setEditing(false);
        setDirty(false);
      }
      onChanged();
    } catch (e) {
      addToast('调整顺序失败: ' + getErrorMessage(e), 'error');
    } finally {
      setSaving(false);
    }
  };

  const handleAdd = async (v: EditorSubmit) => {
    setAdding(true);
    try {
      await api.paperVersions.createItem(courseId, pv.id, { ...v }, token ?? undefined);
      addToast('新题已加入试卷末尾', 'success');
      setAddOpen(false);
      // 新题排在末尾，题号 = 原题数 + 1；加完直接跳过去，省一次手动找题。
      // 「仅看待审核」开着时新题不在左栏，顺手关掉，否则跳过去右栏是空的。
      setOnlyNeedsReview(false);
      setSelected(questions.length + 1);
      setEditing(false);
      setDirty(false);
      onChanged();
    } catch (e) {
      addToast('新增失败: ' + getErrorMessage(e), 'error');
    } finally {
      setAdding(false);
    }
  };

  // ── 定稿 ──

  const doFinalize = async (force: boolean) => {
    try {
      await api.paperVersions.confirm(courseId, pv.id, force ? { force_ignore_needs_review: true } : {}, token ?? undefined);
      addToast('试卷已定稿', 'success');
      setFinalizeOpen(false);
      onChanged();
    } catch (e) {
      addToast('定稿失败: ' + getErrorMessage(e), 'error');
    }
  };

  const handleFinalizeClick = () => {
    // 待审核与缺答案任一存在都先拦一道：前者后端会 409，后者不会——
    // 不在前端提示就只能等导出答卷时看到「缺答案」标注。
    if (pendingItems.length > 0 || missingItems.length > 0) {
      setFinalizeOpen(true);
      return;
    }
    void doFinalize(false);
  };

  const handleRevert = async () => {
    if (!window.confirm('撤销定稿并回到待审核状态？')) return;
    try {
      await api.paperVersions.revert(courseId, pv.id, token ?? undefined);
      addToast('已撤销定稿', 'success');
      onChanged();
    } catch (e) {
      addToast('撤销失败: ' + getErrorMessage(e), 'error');
    }
  };

  // ── 导出与整体预览 ──
  // 导出端点需要 Authorization 头：iframe / window.open 裸开 URL 都带不了，
  // 也禁止把 token 拼进 query（会进浏览器历史与日志）。统一做法是带鉴权
  // 拉取 Blob → 用 object URL 作 iframe src / 程序化 <a download> 下载。

  // 打开预览或切换页签时拉一次；关闭/切页签的 cleanup 负责释放 object URL。
  useEffect(() => {
    if (!previewOpen) return;
    const pid = project?.id ?? '';
    if (!pid) return;
    let created: string | null = null;
    let cancelled = false;
    setPreviewLoading(true);
    api.paperVersions
      .fetchExport(previewKind, courseId, pid, pv.id, token ?? undefined)
      .then(({ blob }) => {
        if (cancelled) return;
        created = URL.createObjectURL(blob);
        setPreviewUrl(created);
        setPreviewLoading(false);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setPreviewLoading(false);
        addToast('预览加载失败: ' + getErrorMessage(e), 'error');
        setPreviewOpen(false);
      });
    return () => {
      cancelled = true;
      if (created) URL.revokeObjectURL(created);
      setPreviewUrl(null);
    };
  }, [previewOpen, previewKind, project?.id, pv.id, courseId, token, addToast]);

  const handleExport = async (kind: ExportKind) => {
    const pid = project?.id ?? '';
    if (!pid) return;
    try {
      const { blob, filename } = await api.paperVersions.fetchExport(
        kind, courseId, pid, pv.id, token ?? undefined,
        // 答题卡下载为可编辑 docx；整体预览不走这里，恒为 html
        kind === 'card' ? 'docx' : undefined,
      );
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      // 下载启动后再回收；提前 revoke 会让个别浏览器拿不到文件
      setTimeout(() => URL.revokeObjectURL(url), 10_000);
    } catch (e) {
      addToast('导出失败: ' + getErrorMessage(e), 'error');
    }
  };

  const previewModal = (
    <Modal
      open={previewOpen}
      onClose={() => setPreviewOpen(false)}
      title="整体预览"
      maxWidth="min(1120px, 96vw)"
      footer={
        <div style={{
          display: 'flex', width: '100%', gap: '12px',
          alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap',
        }}>
          <div style={{ display: 'flex', gap: '6px', flexWrap: 'wrap' }}>
            {PREVIEW_TABS.map((t) => (
              <button
                key={t.key}
                onClick={() => setPreviewKind(t.key)}
                style={{
                  padding: '6px 14px', borderRadius: 999, border: 'none', cursor: 'pointer',
                  fontSize: '0.8rem', fontWeight: 600,
                  background: previewKind === t.key ? 'var(--accent)' : 'var(--accent-subtle)',
                  color: previewKind === t.key ? '#fff' : 'var(--accent)',
                }}
              >
                {t.label}
              </button>
            ))}
          </div>
          <Button
            variant="secondary"
            size="sm"
            icon={<ExternalLink size={14} />}
            onClick={() => {
              // 同一份已拉取的 Blob 换个标签页看；URL 是 blob: object URL，
              // 不含任何鉴权信息
              if (previewUrl) window.open(previewUrl, '_blank', 'noopener');
            }}
          >
            新标签打开
          </Button>
        </div>
      }
    >
      {previewUrl ? (
        <iframe
          key={previewKind}
          src={previewUrl}
          title="试卷整体预览"
          style={{
            display: 'block', width: '100%', height: '64vh',
            border: '1px solid rgba(0,0,0,0.08)', borderRadius: 8, background: '#fff',
          }}
        />
      ) : (
        <div
          style={{
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            width: '100%', height: '64vh',
            border: '1px solid rgba(0,0,0,0.08)', borderRadius: 8,
            background: '#fff', color: 'var(--text-tertiary)', fontSize: '0.85rem',
          }}
        >
          {previewLoading ? '预览加载中…' : '预览不可用'}
        </div>
      )}
    </Modal>
  );

  const addModal = (
    <Modal
      open={addOpen}
      onClose={() => setAddOpen(false)}
      title="新增题目"
      maxWidth="720px"
      footer={
        <>
          <Button variant="secondary" onClick={() => setAddOpen(false)}>取消</Button>
          <Button loading={adding} onClick={() => addEditorRef.current?.submit()} icon={<Plus size={14} />}>加入试卷</Button>
        </>
      }
    >
      {/* AI 生成整题：提案 → 填入表单 → 教师微调后走既有「加入试卷」落库 */}
      {!readonly && addOpen && (
        <div style={{ marginBottom: '14px' }}>
          <AiCreatePanel
            courseId={courseId}
            pvId={pv.id}
            onFill={(proposal) => addEditorRef.current?.setDraft(draftFromProposal(proposal))}
          />
        </div>
      )}
      <QuestionEditor
        ref={addEditorRef}
        initial={emptyDraft()}
        needsReview={false}
        submitting={adding}
        submitLabel="加入试卷"
        showActions={false}
        onSubmit={handleAdd}
        onCancel={() => setAddOpen(false)}
      />
    </Modal>
  );
  if (questions.length === 0) {
    return (
      <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
        <div className="glass-card" style={{ padding: '48px 24px', textAlign: 'center' }}>
          <h3 style={{ fontWeight: 600, fontSize: '1rem', marginBottom: '8px' }}>这份试卷还没有题目</h3>
          <p style={{ fontSize: '0.85rem', color: 'var(--text-secondary)', marginBottom: '18px' }}>
            生成完成后题目会出现在这里；也可以手动新增一道题目。
          </p>
          {/* 文案承诺了「手动新增」就得给出入口，否则空卷无路可走 */}
          {!readonly && (
            <Button onClick={() => setAddOpen(true)} icon={<Plus size={16} />}>新增题目</Button>
          )}
        </div>
        {addModal}
      </div>
    );
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
      <PaperProfile
        pv={pv}
        project={project}
        examPointCount={new Set(questions.map((q) => q.exam_point_id).filter(Boolean)).size}
        onExport={handleExport}
        onPreview={() => setPreviewOpen(true)}
        onFinalize={handleFinalizeClick}
        onRevert={handleRevert}
        onRegenerate={onRegenerate}
        onReview={() => setReviewOpen((v) => !v)}
      />

      {/* AI 质量评审面板：只读报告，工具栏按钮开关；试卷加载即可用（含 readonly/定稿态）。
          悬浮于右下角，不占页面流（旧版插在页首会把双栏整体下压） */}
      {reviewOpen && (
        <FloatingPanel
          title={<>AI 质量评审 <Badge variant="purple">只读报告</Badge></>}
          pillLabel="AI 质量评审"
          onClose={() => setReviewOpen(false)}
        >
          <PaperReviewPanel courseId={courseId} pvId={pv.id} />
        </FloatingPanel>
      )}

      {/* 双栏：左题号索引，右当前题目。两卡等高平齐：高度的唯一来源是这一行
          （max-height 从左卡上移到行本身），默认 stretch 让两张卡都由行高撑开，
          两卡底部即行底，天然平齐；左卡超长时仍靠自身 overflowY 内部滚动。 */}
      <div style={{ display: 'flex', gap: '16px', maxHeight: 'calc(100vh - 140px)' }}>
        <div
          className="glass-card paper-index-card"
          style={{
            width: 240, flexShrink: 0, padding: '0 8px',
            position: 'sticky', top: 16, overflowY: 'auto',
          }}
        >
          {/* 滤镜行 sticky top：列表内部滚动到哪操作都在（高度即 --index-toolbar-h，
              分组头按同一变量改钉在本行下方） */}
          <div style={{
            position: 'sticky', top: 0, zIndex: 2,
            height: 'var(--index-toolbar-h)', boxSizing: 'border-box',
            margin: '0 -8px', padding: '10px 14px 8px', background: 'var(--surface-solid)',
            display: 'flex', alignItems: 'center', gap: '6px',
          }}>
            <Button
              variant={onlyNeedsReview ? 'primary' : 'secondary'} size="sm"
              onClick={toggleFilter}
            >
              仅看待审核{`（${pendingItems.length}）`}
            </Button>
          </div>
          {groups.length === 0 ? (
            <p style={{ padding: '16px 8px', fontSize: '0.8rem', color: 'var(--text-tertiary)', textAlign: 'center' }}>
              没有待审核的题目
            </p>
          ) : (
            <QuestionIndex
              groups={groups}
              selected={selected}
              onSelect={(idx) => {
                // 同题重复点击不弹确认；换题才过脏检查（换题会让编辑器重挂载）
                if (idx === selected || !guardDirty()) return;
                setSelected(idx);
                setEditing(false);
                setDirty(false);
              }}
            />
          )}
          {/* 操作行 sticky bottom：列表停在顶部时「新增题目」不被推出视野（全出血盖住卡底内距） */}
          {!readonly && (
            <div style={{
              position: 'sticky', bottom: 0, margin: '6px -8px 0',
              padding: '10px 14px 14px', background: 'var(--surface-solid)',
              borderTop: '1px solid rgba(0,0,0,0.06)',
            }}>
              <Button variant="secondary" size="sm" onClick={() => setAddOpen(true)} icon={<Plus size={14} />} style={{ width: '100%' }}>
                新增题目
              </Button>
            </div>
          )}
        </div>

        {/* 右栏纵向 flex：本身被行 stretch 到与左卡同高，题目卡 flex:1 撑满，
            两卡底部平齐；AI 改题面板是卡片下方的兄弟节点，开启时贴行底对齐。 */}
        <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: '16px' }}>
          {current ? (
            <QuestionDetail
              key={current.item_index}
              item={current}
              examPointName={
                current.exam_point_title ||
                (current.exam_point_id ? maps.examPoints[current.exam_point_id] : undefined)
              }
              editing={editing}
              readonly={readonly}
              submitting={saving}
              hasPrev={navIdx > 0}
              hasNext={navIdx >= 0 && navIdx < navList.length - 1}
              onEdit={() => { setDirty(false); setEditing(true); setAiOpen(false); }}
              onAiRevise={() => setAiOpen((v) => !v)}
              onCancelEdit={() => { setDirty(false); setEditing(false); }}
              onSave={(v) => handleSave(current.item_index, v)}
              onDelete={() => handleDelete(current.item_index)}
              onMove={(dir) => handleMove(questions.findIndex((q) => q.item_index === current.item_index), dir)}
              onPrev={() => step(-1)}
              onNext={() => step(1)}
              onDirtyChange={reportDirty}
            />
          ) : (
            <div
              className="glass-card"
              style={{
                flex: 1, padding: '40px 24px', textAlign: 'center', color: 'var(--text-tertiary)', fontSize: '0.875rem',
                display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center',
              }}
            >
              请在左侧选择题号
            </div>
          )}
          {/* AI 改题面板：提案 → diff 预览 → 确认后走既有 PATCH 落库。
              悬浮于右下角，不占双栏布局 */}
          {current && !editing && aiOpen && (
            <FloatingPanel
              title={<>AI 改题 · 第 {current.item_index} 题 <Badge variant="purple">提案需确认</Badge></>}
              pillLabel={`AI 改题 · 第 ${current.item_index} 题`}
              onClose={() => setAiOpen(false)}
            >
              <AiRevisePanel
                key={current.item_index}
                courseId={courseId}
                pvId={pv.id}
                item={current}
                onApplied={onChanged}
              />
            </FloatingPanel>
          )}
        </div>
      </div>

      {addModal}
      {previewModal}

      <Modal
        open={finalizeOpen}
        onClose={() => setFinalizeOpen(false)}
        title={pendingItems.length > 0 ? '还有待审核的题目' : '有题目缺少答案'}
        maxWidth="520px"
        footer={
          <>
            <Button variant="secondary" onClick={() => setFinalizeOpen(false)}>返回处理</Button>
            <Button onClick={() => doFinalize(true)}>仍要定稿</Button>
          </>
        }
      >
        {pendingItems.length > 0 && (
          <>
            <p style={{ fontSize: '0.875rem', color: 'var(--text-secondary)', lineHeight: 1.7 }}>
              以下 {pendingItems.length} 道题被质量检查标记为待审核，建议先逐题处理：
            </p>
            <div style={{ display: 'flex', gap: '6px', flexWrap: 'wrap', marginTop: '12px' }}>
              {pendingItems.map((q) => (
                <button
                  key={q.item_index}
                  onClick={() => jumpTo(q.item_index)}
                  style={{
                    padding: '3px 10px', borderRadius: 999, fontSize: '0.78rem', fontWeight: 600,
                    background: 'var(--warning-subtle)', color: 'var(--warning)', border: 'none', cursor: 'pointer',
                  }}
                >
                  第 {q.item_index} 题
                </button>
              ))}
            </div>
          </>
        )}
        {missingItems.length > 0 && (
          <>
            <p style={{
              fontSize: '0.875rem', color: 'var(--text-secondary)', lineHeight: 1.7,
              marginTop: pendingItems.length > 0 ? '16px' : 0,
            }}>
              以下 {missingItems.length} 道题没有参考答案，导出答卷时会标注「缺答案」：
            </p>
            <div style={{ display: 'flex', gap: '6px', flexWrap: 'wrap', marginTop: '12px' }}>
              {missingItems.map((q) => (
                <button
                  key={q.item_index}
                  onClick={() => jumpTo(q.item_index)}
                  style={{
                    padding: '3px 10px', borderRadius: 999, fontSize: '0.78rem', fontWeight: 600,
                    background: 'var(--error-subtle)', color: 'var(--error)', border: 'none', cursor: 'pointer',
                  }}
                >
                  第 {q.item_index} 题
                </button>
              ))}
            </div>
          </>
        )}
        <p style={{ fontSize: '0.8rem', color: 'var(--text-tertiary)', marginTop: '14px', lineHeight: 1.6 }}>
          也可以打开「仅看待审核」逐题核对。确已知悉时可选择「仍要定稿」。
        </p>
      </Modal>
    </div>
  );
}