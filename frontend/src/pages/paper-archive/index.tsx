import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { ArrowLeft, FileDown, Package, RotateCcw } from 'lucide-react';
import { api } from '@/api/client';
import { getErrorMessage } from '@/api/errors';
import { downloadBlob } from '@/lib/download';
import { useAuthStore } from '@/stores/auth';
import { useToastStore } from '@/stores/toast';
import { Button } from '@/components/ui/Button';
import { Badge } from '@/components/ui/Badge';
import { Modal } from '@/components/ui';
import { QUESTION_TYPE_ORDER, questionNumbers, sectionLabel } from '@/lib/examDisplay';
import type { PaperArchiveDetail, PaperVersionItem } from '@/types/api';
import { QuestionDetail } from '@/pages/paper/QuestionDetail';
import { QuestionIndex } from '@/pages/paper/QuestionIndex';
import { type EditorSubmit, normalizeAnswer } from '@/pages/paper/questionShared';

// ═══════════════════════════════════════════════
//  归档试卷编辑页（资料库「试卷」文件夹 → 编辑）
// ═══════════════════════════════════════════════

/**
 * 资料库里一份归档快照的双栏阅读/编辑页：复用试卷页的 QuestionIndex /
 * QuestionDetail / QuestionEditor，只把读写端点换成 paper-archives。
 *
 * 与试卷页的差别（都是刻意的）：
 * - 只有改题/删题/换序，没有新增题目、没有定稿/撤销——归档不是生产线上的卷；
 * - 没有 AI 改题入口（不传 onAiRevise，按钮即不渲染），改题全部由教师手工完成；
 * - 所有写操作只落在归档副本上，原试卷（无论是否已被「只留 3 份」清掉）不受影响。
 */
export default function PaperArchivePage() {
  const { courseId = '', archiveId = '' } = useParams<{ courseId: string; archiveId: string }>();
  const token = useAuthStore((s) => s.token);
  const addToast = useToastStore((s) => s.addToast);
  const navigate = useNavigate();

  const [detail, setDetail] = useState<PaperArchiveDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [editing, setEditing] = useState(false);
  const [selected, setSelected] = useState(0);
  const [dirty, setDirty] = useState(false);
  const [restoreOpen, setRestoreOpen] = useState(false);
  const [restoring, setRestoring] = useState(false);
  const [bundleBusy, setBundleBusy] = useState(false);

  const questions = useMemo(() => detail?.questions ?? [], [detail]);

  const load = useCallback(async () => {
    if (!courseId || !archiveId) return;
    try {
      const d = await api.paperArchives.get(courseId, archiveId);
      setDetail(d);
      setSelected((prev) => (d.questions.some((q) => q.item_index === prev) ? prev : d.questions[0]?.item_index ?? 0));
    } catch (e) {
      addToast('加载归档失败: ' + getErrorMessage(e), 'error');
      navigate('/courses/' + courseId + '/materials?folder=papers', { replace: true });
    } finally {
      setLoading(false);
    }
  }, [courseId, archiveId, addToast, navigate]);

  useEffect(() => {
    void load();
  }, [load]);

  /** 放行前确认丢弃未保存草稿；返回 false 表示教师选择留下 */
  const guardDirty = (): boolean => {
    if (!editing || !dirty) return true;
    const ok = window.confirm('当前题目的修改尚未保存，切换将丢弃这些改动。仍要切换吗？');
    if (ok) setDirty(false);
    return ok;
  };

  // 分组：按题型分节，节内保持卷面顺序（与试卷页同口径）
  const groups = useMemo(() => {
    const byType = new Map<string, PaperVersionItem[]>();
    questions.forEach((q) => {
      const arr = byType.get(q.question_type) ?? [];
      arr.push(q);
      byType.set(q.question_type, arr);
    });
    return [...byType.entries()]
      .sort((a, b) => QUESTION_TYPE_ORDER.indexOf(a[0]) - QUESTION_TYPE_ORDER.indexOf(b[0]))
      .map(([t, items]) => ({ key: t, label: sectionLabel(t), items }));
  }, [questions]);

  // 卷面题号（每题型从 1），与试卷页同口径
  const nos = useMemo(() => questionNumbers(questions), [questions]);
  const displayNo = (itemIndex: number) => nos.get(itemIndex) ?? itemIndex;

  const navIdx = questions.findIndex((q) => q.item_index === selected);
  const current = navIdx >= 0 ? questions[navIdx] : undefined;
  const missingCount = useMemo(() => questions.filter((q) => !normalizeAnswer(q.answer)).length, [questions]);

  const step = (dir: -1 | 1) => {
    const next = questions[navIdx + dir];
    if (!next || !guardDirty()) return;
    setSelected(next.item_index);
    setEditing(false);
  };

  // 键盘 ↑/↓ 翻题：焦点在表单里或正在编辑时不抢占
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (editing || restoreOpen) return;
      const t = e.target as HTMLElement | null;
      if (t && /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName)) return;
      if (e.key === 'ArrowUp') { e.preventDefault(); step(-1); }
      if (e.key === 'ArrowDown') { e.preventDefault(); step(1); }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [questions, navIdx, editing, dirty, restoreOpen]);

  // ── 写操作：全部落在归档快照上，响应体即新快照，直接替换本地状态 ──

  const handleSave = async (idx: number, v: EditorSubmit) => {
    setSaving(true);
    try {
      setDetail(await api.paperArchives.patchQuestion(courseId, archiveId, idx, { ...v }, token ?? undefined));
      addToast(`第 ${displayNo(idx)} 题已保存`, 'success');
      setEditing(false);
      setDirty(false);
    } catch (e) {
      addToast('保存失败: ' + getErrorMessage(e), 'error');
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (idx: number) => {
    if (!window.confirm(`确认删除第 ${displayNo(idx)} 题？删除后该题型后续题目的题号会前移。`)) return;
    setSaving(true);
    try {
      setDetail(await api.paperArchives.deleteQuestion(courseId, archiveId, idx, token ?? undefined));
      addToast('题目已删除', 'success');
      setEditing(false);
      setDirty(false);
    } catch (e) {
      addToast('删除失败: ' + getErrorMessage(e), 'error');
    } finally {
      setSaving(false);
    }
  };

  const handleMove = async (pos: number, dir: -1 | 1) => {
    // 换序会让编辑器重挂载（item_index 变了），先过一遍脏检查
    if (!guardDirty()) return;
    const ordered = questions.map((q) => q.item_index);
    const newPos = pos + dir;
    if (newPos < 0 || newPos >= ordered.length) return;
    const movedIndex = ordered[pos];
    [ordered[pos], ordered[newPos]] = [ordered[newPos], ordered[pos]];
    setSaving(true);
    try {
      setDetail(await api.paperArchives.reorder(courseId, archiveId, ordered, token ?? undefined));
      // 题号被后端重排成 1..N：选中项要跟着换，否则右栏停在同一题号、内容却换了题
      if (movedIndex !== newPos + 1) {
        setSelected(newPos + 1);
        setEditing(false);
        setDirty(false);
      }
    } catch (e) {
      addToast('调整顺序失败: ' + getErrorMessage(e), 'error');
    } finally {
      setSaving(false);
    }
  };

  // ── 存回试卷区：按快照新建一版并设为项目当前卷（归档本身不动） ──

  const handleRestore = async () => {
    setRestoring(true);
    try {
      const out = await api.paperArchives.restore(courseId, archiveId, token ?? undefined);
      setRestoreOpen(false);
      addToast(`已存回试卷区：${out.item_count} 道题，当前卷 v${out.version_no}`, 'success');
    } catch (e) {
      addToast('存回失败: ' + getErrorMessage(e), 'error');
    } finally {
      setRestoring(false);
    }
  };

  // ── 导出：带鉴权拉 Blob → 本地触发下载（token 不进 URL） ──

  const handleExport = async (kind: 'student' | 'card' | 'answer' | 'json') => {
    try {
      const { blob, filename } = await api.paperArchives.fetchExport(
        kind, courseId, archiveId, token ?? undefined,
        kind === 'card' || kind === 'student' ? 'docx' : undefined,
      );
      downloadBlob(blob, filename);
    } catch (e) {
      addToast('导出失败: ' + getErrorMessage(e), 'error');
    }
  };

  const handleBundle = async () => {
    setBundleBusy(true);
    try {
      const { blob, filename } = await api.paperArchives.fetchBundle(
        courseId, archiveId, detail?.snapshot.version_no ?? 1, token ?? undefined,
      );
      downloadBlob(blob, filename);
    } catch (e) {
      addToast('打包下载失败: ' + getErrorMessage(e), 'error');
    } finally {
      setBundleBusy(false);
    }
  };

  const backToFolder = () => navigate('/courses/' + courseId + '/materials?folder=papers');

  if (loading) {
    return (
      <div style={{ display: 'flex', justifyContent: 'center', padding: '80px 0' }}>
        <div className="spinner spinner-lg" />
      </div>
    );
  }

  if (!detail) return null;

  const header = (
    <div className="glass-card" style={{ padding: '18px 20px', display: 'flex', flexDirection: 'column', gap: '12px' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '12px', flexWrap: 'wrap' }}>
        <Button variant="secondary" size="sm" onClick={backToFolder} icon={<ArrowLeft size={14} />}>
          返回试卷文件夹
        </Button>
        <h2 style={{ fontSize: '1.1rem', fontWeight: 600, margin: 0 }}>{detail.name}</h2>
        <Badge variant="default">{detail.item_count} 题</Badge>
        <Badge variant="info">{detail.total_score} 分</Badge>
        {detail.source_version_no != null && <Badge variant="info">源卷 v{detail.source_version_no}</Badge>}
        <span style={{ fontSize: '0.78rem', color: 'var(--text-tertiary)' }}>
          来源项目：{detail.project_name ?? '未知'}
        </span>
        <span style={{ marginLeft: 'auto', fontSize: '0.75rem', color: 'var(--text-tertiary)' }}>
          归档是独立副本：这里的改动不会影响原试卷
        </span>
      </div>

      <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap', alignItems: 'center' }}>
        <Button size="sm" onClick={() => setRestoreOpen(true)} icon={<RotateCcw size={14} />}>
          存回试卷区
        </Button>
        <span style={{ width: 1, height: 22, background: 'var(--line-soft)' }} />
        <Button variant="secondary" size="sm" onClick={() => void handleExport('student')} icon={<FileDown size={14} />}>
          学生卷 docx
        </Button>
        <Button variant="secondary" size="sm" onClick={() => void handleExport('answer')}>答卷 html</Button>
        <Button variant="secondary" size="sm" onClick={() => void handleExport('card')}>答题卡 docx</Button>
        <Button variant="secondary" size="sm" onClick={() => void handleExport('json')}>答案细则 json</Button>
        <Button variant="secondary" size="sm" loading={bundleBusy} onClick={() => void handleBundle()} icon={<Package size={14} />}>
          一键打包
        </Button>
      </div>
    </div>
  );

  if (questions.length === 0) {
    return (
      <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
        {header}
        <div className="glass-card" style={{ padding: '48px 24px', textAlign: 'center' }}>
          <h3 style={{ fontWeight: 600, fontSize: '1rem', marginBottom: '8px' }}>这份归档里没有题目</h3>
          <p style={{ fontSize: '0.85rem', color: 'var(--text-secondary)' }}>
            归档存的是保存当时的卷面快照；空卷没有可编辑或导出的内容。
          </p>
        </div>
      </div>
    );
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
      {header}

      {/* 双栏：与试卷页同一套阅读器（左题号索引，右当前题目） */}
      <div className="paper-reader" style={{ display: 'flex', gap: '16px', maxHeight: 'calc(100vh - 240px)' }}>
        <div
          className="glass-card paper-index-card"
          style={{ width: 240, flexShrink: 0, padding: '0 8px', position: 'sticky', top: 16, overflowY: 'auto' }}
        >
          <div style={{ padding: '12px 8px 4px', fontSize: '0.75rem', color: 'var(--text-tertiary)' }}>
            {missingCount > 0 ? `有 ${missingCount} 题缺答案` : '全部题目已填答案'}
          </div>
          <QuestionIndex
            groups={groups}
            nos={nos}
            selected={selected}
            onSelect={(idx) => {
              if (idx === selected || !guardDirty()) return;
              setSelected(idx);
              setEditing(false);
              setDirty(false);
            }}
          />
        </div>

        <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: '16px' }}>
          {current ? (
            <QuestionDetail
              key={current.item_index}
              item={current}
              no={displayNo(current.item_index)}
              examPointName={current.exam_point_title || undefined}
              editing={editing}
              readonly={false}
              submitting={saving}
              hasPrev={navIdx > 0}
              hasNext={navIdx >= 0 && navIdx < questions.length - 1}
              onEdit={() => { setDirty(false); setEditing(true); }}
              // 归档改题不走 AI：提案端点挂在 paper-versions 上，归档没有版本行
              onCancelEdit={() => { setDirty(false); setEditing(false); }}
              onSave={(v) => void handleSave(current.item_index, v)}
              onDelete={() => void handleDelete(current.item_index)}
              onMove={(dir) => void handleMove(navIdx, dir)}
              onPrev={() => step(-1)}
              onNext={() => step(1)}
              onDirtyChange={setDirty}
            />
          ) : (
            <div
              className="glass-card"
              style={{
                flex: 1, padding: '40px 24px', textAlign: 'center',
                color: 'var(--text-tertiary)', fontSize: '0.875rem',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
              }}
            >
              请在左侧选择题号
            </div>
          )}
        </div>
      </div>

      <Modal
        open={restoreOpen}
        onClose={() => setRestoreOpen(false)}
        title="存回试卷区"
        maxWidth="560px"
        footer={
          <>
            <Button variant="secondary" onClick={() => setRestoreOpen(false)}>取消</Button>
            <Button loading={restoring} onClick={() => void handleRestore()}>确认存回</Button>
          </>
        }
      >
        <p style={{ fontSize: '0.875rem', lineHeight: 1.8, color: 'var(--text-secondary)' }}>
          将按这份归档的快照新建一版试卷，并设为该项目的<strong>当前卷</strong>：
        </p>
        <ul style={{ fontSize: '0.85rem', lineHeight: 1.9, color: 'var(--text-secondary)', paddingLeft: '1.2em', marginTop: 8 }}>
          <li>版本号在现有基础上追加，项目状态回到「待审核」；</li>
          <li>与生成建卷一样走「每个项目只留最近 3 份」，更早的版本会被物理删除；</li>
          <li>归档本身不受影响，之后仍可再次编辑与导出。</li>
        </ul>
      </Modal>
    </div>
  );
}
