import { useEffect, useMemo, useState } from 'react';
import { Check, RotateCcw, Sparkles } from 'lucide-react';
import { api } from '@/api/client';
import { getErrorMessage } from '@/api/errors';
import { useAuthStore } from '@/stores/auth';
import { useToastStore } from '@/stores/toast';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { COGNITIVE_LABELS, PLAN_DIFFICULTY_OPTIONS, clabel, dlabel, toText } from '@/lib/examDisplay';
import type { AiReviseResult, AiReviseTargets, PaperVersionItem, ReviseTargetsResponse, TaskRun } from '@/types/api';

/** 任务终态（与后端 task_runs 状态机一致） */
const TERMINAL = new Set(['succeeded', 'failed', 'cancelled']);

const PRESETS = [
  '让题干更简洁',
  '重写选项，使四个选项表述更平行',
  '优化解析，讲清楚为什么',
  '换一种问法，但考点和难度不变',
];

type FieldKey = 'stem' | 'options' | 'answer' | 'explanation';

const FIELD_LABEL: Record<FieldKey, string> = {
  stem: '题干',
  options: '选项',
  answer: '答案',
  explanation: '解析',
};

const FIELD_KEYS: FieldKey[] = ['stem', 'options', 'answer', 'explanation'];

/** 属性键（与后端 _PROPOSAL_ATTR_KEYS 同名单）：全部进 PATCH；撤销时显式 null 回滚 */
type AttrKey =
  | 'difficulty' | 'cognitive_level'
  | 'exam_point_id' | 'exam_point_title' | 'exam_point_code'
  | 'knowledge_card_id' | 'coverage_atom' | 'answer_boundary' | 'unit_id' | 'backfilled_from';

const ATTR_KEYS: AttrKey[] = [
  'difficulty', 'cognitive_level',
  'exam_point_id', 'exam_point_title', 'exam_point_code',
  'knowledge_card_id', 'coverage_atom', 'answer_boundary', 'unit_id', 'backfilled_from',
];

/** 结构化比较：提案与原题一致的字段不进 diff（也就不进 PATCH） */
function sameValue(a: unknown, b: unknown): boolean {
  return JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
}

const selectStyle: React.CSSProperties = {
  padding: '4px 8px', borderRadius: 8, fontSize: '0.8rem',
  border: '1px solid var(--line)', background: 'var(--surface-sunken)',
  fontFamily: 'inherit', maxWidth: 200,
};

const targetLabelStyle: React.CSSProperties = {
  display: 'flex', flexDirection: 'column', gap: 3,
  fontSize: '0.72rem', fontWeight: 600, color: 'var(--text-tertiary)',
};

/**
 * 单题 AI 改题面板：指令（可选目标属性）→ 轮询提案任务 → 内容/属性 diff 预览 +
 * 校验徽标 → 确认后走既有 PATCH teacher_override 落库；应用后可一键撤销。
 * 目标属性（换难度/认知/考点）由后端确定性解析与预检，模型只写题面内容。
 */
export function AiRevisePanel({
  courseId, pvId, item, no, onApplied,
}: {
  courseId: string;
  pvId: string;
  item: PaperVersionItem;
  /** 卷面题号（每题型从 1）；item_index 仍作提案/端点的稳定 id */
  no: number;
  /** 应用/撤销后通知父级刷新试卷 */
  onApplied: () => void;
}) {
  const token = useAuthStore((s) => s.token);
  const addToast = useToastStore((s) => s.addToast);

  const [instruction, setInstruction] = useState('');
  const [busy, setBusy] = useState(false);
  const [taskRunId, setTaskRunId] = useState<string | null>(null);
  const [result, setResult] = useState<AiReviseResult | null>(null);
  const [applying, setApplying] = useState(false);
  // 已应用字段的「应用前旧值」+ 用过的属性键：撤销 = 内容旧值写回、属性键显式 null
  //（属性读端走 or 链回落 payload/plan 原值，override 里不留教师没改过的痕迹）
  const [snapshot, setSnapshot] = useState<{ content: Partial<Record<FieldKey, unknown>>; attrs: AttrKey[] } | null>(null);

  // 目标属性选择（"不变"为默认；考点菜单是确定性数据，懒加载一次）
  const [difficultyTarget, setDifficultyTarget] = useState('');
  const [cognitiveTarget, setCognitiveTarget] = useState('');
  const [epTarget, setEpTarget] = useState('');
  const [cardTarget, setCardTarget] = useState('');
  const [targetsData, setTargetsData] = useState<ReviseTargetsResponse | null>(null);
  const [targetsLoading, setTargetsLoading] = useState(false);

  // 轮询提案任务（与 PipelinePanel 的生成轮询同款：依赖只取 id，终态自停）
  useEffect(() => {
    if (!taskRunId) return;
    const timer = setInterval(async () => {
      try {
        const tr: TaskRun = await api.examProjects.getTaskRun(courseId, taskRunId, token ?? undefined);
        if (!TERMINAL.has(tr.status)) return;
        clearInterval(timer);
        setTaskRunId(null);
        setBusy(false);
        if (tr.status === 'succeeded' && tr.result) {
          setResult(tr.result as unknown as AiReviseResult);
        } else if (tr.status === 'failed') {
          addToast('AI 改题失败: ' + (tr.error_message || '未知错误'), 'error');
        }
      } catch {
        /* 瞬时轮询错误忽略，下一轮重试 */
      }
    }, 1500);
    return () => clearInterval(timer);
  }, [taskRunId, courseId, token, addToast]);

  // 换考点目标菜单：拉取失败不阻塞改题（教师仍可纯内容/难度认知调整）
  useEffect(() => {
    let alive = true;
    setTargetsLoading(true);
    api.paperVersions
      .reviseTargets(courseId, pvId, item.item_index, token ?? undefined)
      .then((data) => { if (alive) setTargetsData(data); })
      .catch(() => { /* 菜单不可用时考点下拉只剩「不变」 */ })
      .finally(() => { if (alive) setTargetsLoading(false); });
    return () => { alive = false; };
  }, [courseId, pvId, item.item_index, token]);

  const epCards = useMemo(
    () => targetsData?.exam_points.find((ep) => ep.id === epTarget)?.cards ?? [],
    [targetsData, epTarget],
  );

  const buildTargets = (): AiReviseTargets | undefined => {
    const t: AiReviseTargets = {};
    if (difficultyTarget) t.difficulty = difficultyTarget;
    if (cognitiveTarget) t.cognitive_level = cognitiveTarget;
    if (epTarget) {
      t.exam_point_id = epTarget;
      if (cardTarget) t.card_id = cardTarget;
    }
    return Object.keys(t).length ? t : undefined;
  };

  const submit = async (text?: string) => {
    const ins = (text ?? instruction).trim();
    if (!ins) {
      addToast('请先填写修改要求', 'info');
      return;
    }
    setInstruction(ins);
    setBusy(true);
    setResult(null);
    setSnapshot(null);
    try {
      const res = await api.paperVersions.aiRevise(courseId, pvId, item.item_index, ins, buildTargets(), token ?? undefined);
      setTaskRunId(res.task_run_id);
    } catch (e) {
      setBusy(false);
      addToast('发起 AI 改题失败: ' + getErrorMessage(e), 'error');
    }
  };

  const changedKeys = result
    ? FIELD_KEYS.filter((k) => !sameValue(result.proposal[k], result.current[k]))
    : [];
  const changedAttrKeys = result
    ? ATTR_KEYS.filter((k) => !sameValue(result.proposal[k], result.current[k]))
    : [];
  const hasChanges = changedKeys.length > 0 || changedAttrKeys.length > 0;
  const canApply = !!result?.validation.passed && hasChanges && !snapshot;

  const apply = async () => {
    if (!result || !canApply) return;
    setApplying(true);
    const patch: Record<string, unknown> = {};
    const prev: Partial<Record<FieldKey, unknown>> = {};
    for (const k of changedKeys) {
      patch[k] = result.proposal[k];
      prev[k] = result.current[k];
    }
    // 属性键一并落库（系统盖章值），撤销时按同一名单显式 null 回滚
    for (const k of changedAttrKeys) {
      patch[k] = result.proposal[k] ?? null;
    }
    try {
      await api.paperVersions.patchItem(courseId, pvId, item.item_index, { teacher_override_patch: patch }, token ?? undefined);
      setSnapshot({ content: prev, attrs: changedAttrKeys });
      addToast(`第 ${no} 题已应用 AI 修改`, 'success');
      onApplied();
    } catch (e) {
      addToast('应用失败: ' + getErrorMessage(e), 'error');
    } finally {
      setApplying(false);
    }
  };

  const undo = async () => {
    if (!snapshot) return;
    setApplying(true);
    const patch: Record<string, unknown> = { ...snapshot.content };
    for (const k of snapshot.attrs) patch[k] = null;
    try {
      await api.paperVersions.patchItem(courseId, pvId, item.item_index, { teacher_override_patch: patch }, token ?? undefined);
      setSnapshot(null);
      setResult(null);
      addToast('已撤销本次 AI 修改', 'success');
      onApplied();
    } catch (e) {
      addToast('撤销失败: ' + getErrorMessage(e), 'error');
    } finally {
      setApplying(false);
    }
  };

  // 属性 diff 的可读行（难度/认知/考点/原子/答案域）；其余键只进 PATCH 不单列
  const attrRows: Array<{ label: string; from: string; to: string }> = [];
  if (result) {
    if (!sameValue(result.proposal.difficulty, result.current.difficulty)) {
      attrRows.push({
        label: '难度',
        from: dlabel(String(result.current.difficulty ?? '')),
        to: dlabel(String(result.proposal.difficulty ?? '')),
      });
    }
    if (!sameValue(result.proposal.cognitive_level, result.current.cognitive_level)) {
      attrRows.push({
        label: '认知层级',
        from: clabel(String(result.current.cognitive_level ?? '')),
        to: clabel(String(result.proposal.cognitive_level ?? '')),
      });
    }
    if (!sameValue(result.proposal.exam_point_id, result.current.exam_point_id)) {
      attrRows.push({
        label: '考点',
        from: result.current.exam_point_title || result.current.exam_point_id || '—',
        to: result.proposal.exam_point_title || result.proposal.exam_point_id || '—',
      });
    }
    if (!sameValue(result.proposal.coverage_atom, result.current.coverage_atom)) {
      attrRows.push({
        label: '考查原子',
        from: String(result.current.coverage_atom || '—'),
        to: String(result.proposal.coverage_atom || '—'),
      });
    }
    if (!sameValue(result.proposal.answer_boundary, result.current.answer_boundary)) {
      attrRows.push({
        label: '答案域',
        from: String(result.current.answer_boundary || '—'),
        to: String(result.proposal.answer_boundary || '—'),
      });
    }
  }

  const running = busy || !!taskRunId;

  return (
    <div
      style={{
        padding: '16px 24px 16px 22px',
        display: 'flex',
        flexDirection: 'column',
        gap: '12px',
      }}
    >
      {/* 头部（标题/最小化/关闭）由 FloatingPanel 外壳统一提供 */}

      {result && (
        <>
          {/* 校验徽标 + AI 自述 */}
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
            {result.validation.passed
              ? <Badge variant="success">校验通过</Badge>
              : (
                  <span title={result.validation.code}>
                    <Badge variant="error">未通过校验</Badge>
                  </span>
                )}
            {!result.validation.passed && (
              <span style={{ fontSize: '0.78rem', color: 'var(--error)' }}>{result.validation.message}</span>
            )}
            {!hasChanges && <Badge variant="default">提案与原题无差异</Badge>}
          </div>
          {result.change_summary && (
            <p style={{ fontSize: '0.82rem', color: 'var(--text-secondary)', lineHeight: 1.7, margin: 0 }}>
              <span style={{ fontWeight: 600, color: 'var(--purple)' }}>AI 说明：</span>
              {result.change_summary}
            </p>
          )}

          {/* 属性调整 diff（目标属性由系统盖章，模型只写题面） */}
          {attrRows.length > 0 && (
            <div style={{
              padding: '8px 12px', borderRadius: 'var(--radius-sm)',
              background: 'var(--purple-subtle)', border: '1px solid var(--purple-soft)',
              display: 'flex', flexDirection: 'column', gap: 6,
            }}>
              <div style={{ fontSize: '0.75rem', fontWeight: 600, color: 'var(--purple)' }}>
                目标属性调整（应用后落库，可撤销）
              </div>
              {attrRows.map((row) => (
                <div key={row.label} style={{ display: 'flex', gap: 8, alignItems: 'baseline', fontSize: '0.8rem', flexWrap: 'wrap' }}>
                  <span style={{ fontWeight: 600, color: 'var(--text-tertiary)', minWidth: 48 }}>{row.label}</span>
                  <span style={{ color: 'var(--error-ink)', textDecoration: 'line-through' }}>{row.from}</span>
                  <span style={{ color: 'var(--text-tertiary)' }}>→</span>
                  <span style={{ color: 'var(--text)', wordBreak: 'break-word' }}>{row.to}</span>
                </div>
              ))}
              {result.proposal.backfilled_from && (
                <div style={{ fontSize: '0.75rem', color: 'var(--warning)' }}>
                  跨考点改考：本题按原考点计回合同配比（回补明细可在评审报告中查看）
                </div>
              )}
            </div>
          )}

          {/* 字段级 diff：只展示有变化的字段 */}
          <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
            {changedKeys.map((k) => (
              <div key={k}>
                <div style={{ fontSize: '0.75rem', fontWeight: 600, color: 'var(--text-tertiary)', marginBottom: 4 }}>
                  {FIELD_LABEL[k]}
                </div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
                  <div style={{
                    padding: '6px 10px', borderRadius: 'var(--radius-sm)', fontSize: '0.82rem', lineHeight: 1.65,
                    background: 'var(--error-subtle)',
                    color: 'var(--text-secondary)', whiteSpace: 'pre-wrap', wordBreak: 'break-word',
                    textDecoration: 'line-through', textDecorationColor: 'var(--error-ink)',
                  }}>
                    {toText(result.current[k])}
                  </div>
                  <div style={{
                    padding: '6px 10px', borderRadius: 'var(--radius-sm)', fontSize: '0.82rem', lineHeight: 1.65,
                    background: 'var(--success-subtle)',
                    color: 'var(--text)', whiteSpace: 'pre-wrap', wordBreak: 'break-word',
                  }}>
                    {toText(result.proposal[k])}
                  </div>
                </div>
              </div>
            ))}
            {changedKeys.length === 0 && (
              <p style={{ fontSize: '0.8rem', color: 'var(--text-tertiary)', margin: 0 }}>
                {hasChanges
                  ? '题面内容未改动，仅调整目标属性。'
                  : '提案没有改动任何字段，可换一种要求重新生成。'}
              </p>
            )}
          </div>

          {/* 确认 / 撤销 */}
          <div style={{ display: 'flex', gap: '8px', alignItems: 'center', flexWrap: 'wrap', paddingTop: '10px', borderTop: '1px solid var(--line-soft)' }}>
            {!snapshot ? (
              <>
                <Button
                  size="sm"
                  disabled={!canApply || applying}
                  loading={applying}
                  onClick={() => void apply()}
                  icon={<Check size={14} />}
                >
                  确认应用到本题
                </Button>
                {!result.validation.passed && (
                  <span style={{ fontSize: '0.75rem', color: 'var(--warning)' }}>
                    校验未通过，不能应用——可调整要求后重新生成
                  </span>
                )}
              </>
            ) : (
              <>
                <Badge variant="success">已应用</Badge>
                <Button size="sm" variant="secondary" disabled={applying} loading={applying} onClick={() => void undo()} icon={<RotateCcw size={14} />}>
                  撤销本次修改
                </Button>
              </>
            )}
          </div>
        </>
      )}

      {/* 指令输入（悬浮）：置于面板末尾并 sticky 钉住滚动容器底边，
          diff/提案再长也不会把输入条推出视野；不透明背景 + 阴影保证
          浮在结果内容之上时文字不透出，z-index 低于弹窗与 toast */}
      <div
        style={{
          display: 'flex', flexDirection: 'column', gap: '8px',
          position: 'sticky', bottom: 12, zIndex: 5,
          padding: '12px 14px', borderRadius: 12,
          background: 'var(--surface-elevated)',
          border: '1px solid var(--line-soft)',
          boxShadow: 'var(--shadow-3)',
        }}
      >
        {/* 目标属性（全部「不变」为默认；配对合法性与考点可用性由后端确定性预检） */}
        <div style={{ display: 'flex', gap: '10px', flexWrap: 'wrap' }}>
          <label style={targetLabelStyle}>
            难度目标
            <select value={difficultyTarget} disabled={running} onChange={(e) => setDifficultyTarget(e.target.value)} style={selectStyle}>
              <option value="">不变（{dlabel(String(item.difficulty ?? 'medium'))}）</option>
              {PLAN_DIFFICULTY_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>{o.label}</option>
              ))}
            </select>
          </label>
          <label style={targetLabelStyle}>
            认知层级
            <select value={cognitiveTarget} disabled={running} onChange={(e) => setCognitiveTarget(e.target.value)} style={selectStyle}>
              <option value="">不变（{clabel(String(item.cognitive_level ?? ''))}）</option>
              {Object.entries(COGNITIVE_LABELS).map(([v, l]) => (
                <option key={v} value={v}>{l}</option>
              ))}
            </select>
          </label>
          <label style={targetLabelStyle}>
            考点（换考点）
            <select
              value={epTarget}
              disabled={running || targetsLoading}
              onChange={(e) => { setEpTarget(e.target.value); setCardTarget(''); }}
              style={selectStyle}
            >
              <option value="">{targetsLoading ? '加载考点…' : '不变'}</option>
              {(targetsData?.exam_points ?? []).map((ep) => (
                <option key={ep.id} value={ep.id}>
                  {ep.code ? ep.code + ' ' : ''}{ep.title || ep.id}{ep.current ? '（当前）' : ''}
                </option>
              ))}
              {(targetsData?.unavailable_exam_points ?? []).map((ep) => (
                <option key={ep.id} value="" disabled>
                  {ep.title || ep.id}（{ep.reason}）
                </option>
              ))}
            </select>
          </label>
          {epTarget && (
            <label style={targetLabelStyle}>
              知识卡（原子）
              <select value={cardTarget} disabled={running} onChange={(e) => setCardTarget(e.target.value)} style={selectStyle}>
                <option value="">自动选择最优</option>
                {epCards.map((c) => (
                  <option key={c.card_id} value={c.card_id}>{c.card_name}：{c.atom}</option>
                ))}
              </select>
            </label>
          )}
        </div>
        {targetsData?.note && (
          <div style={{ fontSize: '0.72rem', color: 'var(--text-tertiary)' }}>{targetsData.note}</div>
        )}
        <textarea
          value={instruction}
          onChange={(e) => setInstruction(e.target.value)}
          placeholder="告诉 AI 怎么改这道题（题型/分值锁定；难度/认知/考点可在上方目标中调整，目标由系统确定性落库）"
          rows={2}
          disabled={running}
          style={{
            resize: 'vertical', padding: '8px 10px', borderRadius: 12, fontSize: '0.875rem',
            border: '1px solid var(--line)', background: 'var(--surface-sunken)',
            fontFamily: 'inherit', lineHeight: 1.6,
          }}
        />
        <div style={{ display: 'flex', gap: '6px', flexWrap: 'wrap' }}>
          {PRESETS.map((p) => (
            <button
              key={p}
              disabled={running}
              onClick={() => void submit(p)}
              style={{
                padding: '3px 10px', borderRadius: 999, fontSize: '0.75rem', fontWeight: 600,
                background: 'var(--purple-subtle)', color: 'var(--purple)',
                border: '1px solid var(--purple-soft)', cursor: running ? 'default' : 'pointer',
                opacity: running ? 0.5 : 1,
              }}
            >
              {p}
            </button>
          ))}
        </div>
        <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
          <Button size="sm" loading={running} onClick={() => void submit()} icon={<Sparkles size={14} />}>
            {running ? 'AI 改题中…（约几秒）' : result ? '按新要求再改一版' : '生成修改提案'}
          </Button>
          {result && !running && (
            <Button size="sm" variant="ghost" onClick={() => setResult(null)}>清空提案</Button>
          )}
        </div>
      </div>
    </div>
  );
}
