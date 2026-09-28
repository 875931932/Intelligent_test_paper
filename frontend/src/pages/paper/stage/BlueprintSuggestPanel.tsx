import { useEffect, useRef, useState } from 'react';
import { Check, ChevronUp, Sparkles, Wand2 } from 'lucide-react';
import { api } from '@/api/client';
import { getErrorMessage } from '@/api/errors';
import { useAuthStore } from '@/stores/auth';
import { Button } from '@/components/ui/Button';
import type { NameMaps } from '@/hooks/useNameMaps';
import { clabel, dlabel, qlabel } from '@/lib/examDisplay';
import { formatScore } from '@/lib/format';
import type {
  BlueprintSuggestResult,
  BlueprintSuggestion,
  PlanItem,
  PlanItemChanges,
  TaskRun,
} from '@/types/api';
import { cardLabel, examPointLabel, isTerminal, type ToastFn } from './stageShared';

const FIELD_LABELS: Record<keyof PlanItemChanges, string> = {
  question_type: '题型',
  difficulty: '难度',
  cognitive_level: '认知层级',
  score: '分值',
  exam_point_id: '考点',
  card_id: '知识卡',
};

/** 建议字段 → 可直接提交给既有 PATCH plan-items 的 changes 形状（后端已归一词表） */
function toChanges(s: BlueprintSuggestion): PlanItemChanges {
  switch (s.field) {
    case 'score': return { score: Number(s.value) };
    case 'difficulty': return { difficulty: String(s.value) };
    case 'cognitive_level': return { cognitive_level: String(s.value) };
    case 'question_type': return { question_type: String(s.value) };
    case 'exam_point_id': return { exam_point_id: String(s.value) };
    case 'card_id': return { card_id: String(s.value) };
    default: return {};
  }
}

/** 当前值文案（题位现状）与目标值文案（建议值） */
function currentLabel(item: PlanItem | undefined, field: keyof PlanItemChanges, maps: NameMaps): string {
  if (!item) return '—';
  switch (field) {
    case 'score': return `${formatScore(item.score)} 分`;
    case 'difficulty': return dlabel(item.difficulty);
    case 'cognitive_level': return clabel(item.cognitive_level);
    case 'question_type': return qlabel(item.question_type);
    case 'exam_point_id': return item.exam_point_id ? examPointLabel(maps, item.exam_point_id, item.exam_point_title) : '—';
    case 'card_id': return item.knowledge_card_id ? cardLabel(maps, item.knowledge_card_id, item.knowledge_card_name) : '—';
    default: return '—';
  }
}

function targetLabel(s: BlueprintSuggestion, maps: NameMaps): string {
  switch (s.field) {
    case 'score': return `${formatScore(s.value)} 分`;
    case 'difficulty': return dlabel(String(s.value));
    case 'cognitive_level': return clabel(String(s.value));
    case 'question_type': return qlabel(String(s.value));
    case 'exam_point_id': return examPointLabel(maps, String(s.value));
    case 'card_id': return cardLabel(maps, String(s.value));
    default: return String(s.value);
  }
}

const keyOf = (s: BlueprintSuggestion) => `${s.item_index}:${s.field}`;

/** 「已应用」判定里「没有额外 key」的占位集合（模块级常量，避免每次渲染新建） */
const EMPTY_KEYS = new Set<string>();

/** 提案时原值文案：优先用后端 from_value 快照（应用后不漂移），缺失时退回当前值 */
function fromLabel(s: BlueprintSuggestion, item: PlanItem | undefined, maps: NameMaps): string {
  if (s.from_value === undefined || s.from_value === null || s.from_value === '') {
    return currentLabel(item, s.field, maps);
  }
  const value = String(s.from_value);
  switch (s.field) {
    case 'score': return `${formatScore(Number(value))} 分`;
    case 'difficulty': return dlabel(value);
    case 'cognitive_level': return clabel(value);
    case 'question_type': return qlabel(value);
    case 'exam_point_id': return examPointLabel(maps, value);
    case 'card_id': return cardLabel(maps, value);
    default: return value;
  }
}

/** 建议值已等于题位当前值（应用过或教师已手动改到位）→ 视为已应用 */
function matchesCurrent(s: BlueprintSuggestion, item: PlanItem | undefined): boolean {
  if (!item) return false;
  if (s.field === 'score') return Math.abs(Number(item.score) - Number(s.value)) < 0.001;
  const current =
    s.field === 'card_id' ? item.knowledge_card_id
    : s.field === 'exam_point_id' ? item.exam_point_id
    : s.field === 'difficulty' ? item.difficulty
    : s.field === 'cognitive_level' ? item.cognitive_level
    : item.question_type;
  return String(current ?? '') === String(s.value);
}

/**
 * 蓝图题位 AI 调整建议面板：一句话（可空）→ 建议任务 → 轮询 → 建议清单。
 * 教师逐条/批量点「应用」走既有 PATCH plan-items（0.5 步进、总分合理性、
 * draft 冻结与课程隔离等服务端校验原样生效）——AI 只产提案不直接改题位。
 */
export function BlueprintSuggestPanel({
  courseId, projectId, planItems, reload, maps, addToast,
}: {
  courseId: string;
  projectId: string;
  planItems: PlanItem[];
  reload: () => void;
  maps: NameMaps;
  addToast: ToastFn;
}) {
  const token = useAuthStore((s) => s.token);
  const [instruction, setInstruction] = useState('');
  const [busy, setBusy] = useState(false);
  const [taskRunId, setTaskRunId] = useState<string | null>(null);
  const [result, setResult] = useState<BlueprintSuggestResult | null>(null);
  const [collapsed, setCollapsed] = useState(false);
  const [applied, setApplied] = useState<Set<string>>(new Set());
  const [applyingKey, setApplyingKey] = useState<string | null>(null);
  const [applyingAll, setApplyingAll] = useState(false);

  const running = busy || !!taskRunId;

  // 恢复用 refs：planItems 不进 effect 依赖（每次 apply/reload 重触发会清掉
  // 刚点上的「已应用」标记），改为每次同步到 ref 读最新值
  const planItemsRef = useRef(planItems);
  useEffect(() => { planItemsRef.current = planItems; }, [planItems]);
  /** 每个项目只恢复一次（token 刷新重跑 effect 时不再发请求） */
  const restoredForRef = useRef<string | null>(null);
  /** 教师已在本面板发起新建议——慢一步返回的恢复结果不得覆盖新任务 */
  const startedRef = useRef(false);

  // 轮询建议任务（与 PaperReviewPanel 同款：依赖只取 id，终态自停）
  useEffect(() => {
    if (!taskRunId) return;
    const timer = setInterval(async () => {
      try {
        const tr: TaskRun = await api.examProjects.getTaskRun(courseId, taskRunId, token ?? undefined);
        if (!isTerminal(tr.status)) return;
        clearInterval(timer);
        setTaskRunId(null);
        setBusy(false);
        if (tr.status === 'succeeded' && tr.result) {
          const res = tr.result as unknown as BlueprintSuggestResult;
          setResult(res);
          setApplied(new Set());
          setCollapsed(false);
          addToast(
            res.suggestions.length > 0
              ? `AI 给出 ${res.suggestions.length} 条调整建议，确认后点「应用」`
              : 'AI 检查完成：蓝图无需调整',
            'success',
          );
        } else if (tr.status === 'failed') {
          addToast('AI 调整建议失败: ' + (tr.error_message || '未知错误'), 'error');
        }
      } catch {
        /* 瞬时轮询错误忽略，下一轮重试 */
      }
    }, 1500);
    return () => clearInterval(timer);
  }, [taskRunId, courseId, token, addToast]);

  // 挂载时从 task_runs 恢复最近一次建议（权威在后端，只存组件内存会刷新即丢——
  // 教师误以为「应用了没保存」）。在途任务续上既有轮询跑完照常出清单；成功任务
  // 恢复清单与折叠态；失败/取消/无历史一律静默，面板保持新发起的空状态。
  useEffect(() => {
    if (!projectId || restoredForRef.current === projectId) return;
    restoredForRef.current = projectId;
    let cancelled = false;
    void (async () => {
      try {
        const res = await api.examProjects.getLatestSuggest(courseId, projectId, token ?? undefined);
        const tr = res.task_run;
        if (cancelled || startedRef.current || !tr) return;
        if (!isTerminal(tr.status)) {
          // 在途 → 交给上面的轮询，跑完照常出清单/报错
          setTaskRunId(tr.id);
          setBusy(true);
          setInstruction((prev) => prev || String(tr.payload?.instruction ?? ''));
          return;
        }
        if (tr.status !== 'succeeded' || !tr.result) return;
        const out = tr.result as unknown as BlueprintSuggestResult;
        setResult(out);
        setInstruction((prev) => prev || out.instruction);
        // 全部建议值都已等于题位当前值（上次已应用过）→ 恢复即折叠，不抢视线
        const items = planItemsRef.current;
        if (
          out.suggestions.length > 0
          && out.suggestions.every((s) =>
            matchesCurrent(s, items.find((p) => p.item_index === s.item_index)),
          )
        ) {
          setCollapsed(true);
        }
      } catch {
        /* 无历史/瞬时错误 → 静默跳过恢复，不打扰教师 */
      }
    })();
    return () => { cancelled = true; };
  }, [courseId, projectId, token]);

  const handleGenerate = async () => {
    setBusy(true);
    startedRef.current = true;
    try {
      const res = await api.examProjects.suggestBlueprintAdjustments(
        courseId, projectId, instruction.trim(), token ?? undefined,
      );
      setTaskRunId(res.task_run_id);
    } catch (err) {
      setBusy(false);
      addToast('发起 AI 建议失败: ' + getErrorMessage(err), 'error');
    }
  };

  const applyOne = async (s: BlueprintSuggestion): Promise<boolean> => {
    const item = planItems.find((p) => p.item_index === s.item_index);
    if (!item) {
      addToast(`第 ${s.item_index} 题不在当前蓝图里，请点「刷新」后重试`, 'error');
      return false;
    }
    try {
      await api.examProjects.updatePlanItem(courseId, item.id, toChanges(s));
      return true;
    } catch (err) {
      addToast(`第 ${s.item_index} 题${FIELD_LABELS[s.field]}应用失败: ${getErrorMessage(err)}`, 'error');
      return false;
    }
  };

  /**
   * 已应用的统一口径：本会话点过应用、或题位当前值已等于建议值。
   * 恢复历史建议时 `applied` 必然为空，全靠「值已相等」兜住——否则刷新后会把
   * 已落地条目误报成待办，点「全部应用」又对同一题位重复 PATCH。
   */
  const isAppliedTo = (s: BlueprintSuggestion, extra: Set<string> = EMPTY_KEYS): boolean =>
    extra.has(keyOf(s))
    || applied.has(keyOf(s))
    || matchesCurrent(s, planItems.find((p) => p.item_index === s.item_index));

  /** 待办清单：尚未落地的建议 */
  const pending = result ? result.suggestions.filter((s) => !isAppliedTo(s)) : [];

  /** 已落地条数 */
  const appliedCount = result ? result.suggestions.filter((s) => isAppliedTo(s)).length : 0;

  /** 全部建议是否都已落地——应用完自动折叠的判定（部分失败不收起） */
  const allSatisfied = (extra: Set<string>): boolean =>
    !!result && result.suggestions.every((s) => isAppliedTo(s, extra));

  const handleApplyOne = async (s: BlueprintSuggestion) => {
    const key = keyOf(s);
    setApplyingKey(key);
    try {
      const ok = await applyOne(s);
      if (ok) {
        setApplied((prev) => new Set(prev).add(key));
        addToast(`已应用第 ${s.item_index} 题的${FIELD_LABELS[s.field]}调整`, 'success');
        reload();
        // 最后一条也落地 → 建议清单自动折叠成一行状态
        if (allSatisfied(new Set([key]))) setCollapsed(true);
      }
    } finally {
      setApplyingKey(null);
    }
  };

  const handleApplyAll = async () => {
    if (pending.length === 0) return;
    setApplyingAll(true);
    let okCount = 0;
    const appliedNow = new Set(applied);
    try {
      for (const s of pending) {
        if (await applyOne(s)) {
          appliedNow.add(keyOf(s));
          setApplied((prev) => new Set(prev).add(keyOf(s)));
          okCount += 1;
        }
      }
      reload();
      const failed = pending.length - okCount;
      addToast(
        failed > 0 ? `已应用 ${okCount} 条建议，${failed} 条失败` : `已应用 ${okCount} 条建议`,
        failed > 0 ? 'error' : 'success',
      );
      // 全部落地才收起；有失败的留在眼前，教师能看见漏网条目
      if (allSatisfied(appliedNow)) setCollapsed(true);
    } finally {
      setApplyingAll(false);
    }
  };

  return (
    <div style={{
      borderRadius: 10, padding: '12px 14px',
      background: 'var(--accent-subtle)', border: '1px dashed rgba(0,113,227,0.35)',
      display: 'flex', flexDirection: 'column', gap: '10px',
    }}>
      <div style={{ display: 'flex', gap: '10px', alignItems: 'center', flexWrap: 'wrap' }}>
        <span style={{
          display: 'flex', alignItems: 'center', gap: '6px', whiteSpace: 'nowrap',
          fontSize: '0.8rem', fontWeight: 600, color: 'var(--accent)',
        }}>
          <Sparkles size={15} /> AI 调整建议
        </span>
        <input
          className="input-field" style={{ flex: 1, minWidth: 220 }}
          value={instruction}
          onChange={(e) => setInstruction(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') void handleGenerate(); }}
          placeholder="可留空做常规检查；也可写：难度按5简单3中等2难分、第3章分值调高"
          aria-label="调整要求（一句话，可空）"
          disabled={running}
        />
        <Button size="sm" variant="secondary" loading={running} onClick={() => void handleGenerate()} icon={<Sparkles size={14} />}>
          {running ? '生成中…' : '生成建议'}
        </Button>
        {pending.length > 0 && (
          <Button
            size="sm" loading={applyingAll}
            onClick={() => void handleApplyAll()} icon={<Wand2 size={14} />}
          >
            {applyingAll ? '应用中…' : `全部应用（${pending.length}）`}
          </Button>
        )}
      </div>

      {result && collapsed && (
        <button
          type="button"
          onClick={() => setCollapsed(false)}
          style={{
            display: 'flex', alignItems: 'center', gap: '8px', width: '100%',
            padding: '7px 10px', borderRadius: 8, cursor: 'pointer', textAlign: 'left',
            background: 'var(--surface, #fff)', border: '1px solid var(--border, #d2d2d7)',
            fontSize: '0.78rem', color: 'var(--text-secondary)',
          }}
        >
          <Check size={13} style={{ color: 'var(--accent)', flexShrink: 0 }} />
          <span>
            {result.suggestions.length > 0
              ? `已应用 ${appliedCount}/${result.suggestions.length} 条调整建议`
              : 'AI 检查完成：蓝图无需调整'}
          </span>
          <span style={{ marginLeft: 'auto', color: 'var(--accent)', fontWeight: 600 }}>
            展开查看
          </span>
        </button>
      )}
      {result && !collapsed && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
          <div style={{ display: 'flex', gap: '8px', alignItems: 'flex-start' }}>
            <p style={{ flex: 1, fontSize: '0.8rem', lineHeight: 1.7, color: 'var(--text-secondary)' }}>
              {result.summary}
            </p>
            <Button
              size="sm" variant="secondary"
              onClick={() => setCollapsed(true)} icon={<ChevronUp size={13} />}
            >
              收起
            </Button>
          </div>
          {result.suggestions.length === 0 && (
            <p style={{ fontSize: '0.78rem', color: 'var(--text-tertiary)' }}>
              没有需要调整的题位。
            </p>
          )}
          {result.suggestions.map((s) => {
            const item = planItems.find((p) => p.item_index === s.item_index);
            const isApplied = isAppliedTo(s);
            return (
              <div
                key={keyOf(s)}
                style={{
                  display: 'flex', gap: '10px', alignItems: 'center', flexWrap: 'wrap',
                  padding: '8px 10px', borderRadius: 8,
                  background: isApplied ? 'rgba(0,0,0,0.03)' : 'var(--surface, #fff)',
                  border: '1px solid var(--border, #d2d2d7)',
                  opacity: isApplied ? 0.65 : 1,
                }}
              >
                <span style={{ fontSize: '0.78rem', fontWeight: 700 }}>#{s.item_index}</span>
                <span style={{ fontSize: '0.78rem', color: 'var(--text-secondary)' }}>
                  {FIELD_LABELS[s.field]}：{fromLabel(s, item, maps)}
                  <span style={{ margin: '0 6px', color: 'var(--accent)' }}>→</span>
                  <strong>{targetLabel(s, maps)}</strong>
                </span>
                <span style={{ fontSize: '0.75rem', color: 'var(--text-tertiary)', flex: 1, minWidth: '160px' }}>
                  {s.reason}
                </span>
                <Button
                  size="sm" variant="secondary"
                  disabled={isApplied || applyingAll}
                  loading={applyingKey === keyOf(s)}
                  onClick={() => void handleApplyOne(s)}
                >
                  {isApplied ? '已应用' : '应用'}
                </Button>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
