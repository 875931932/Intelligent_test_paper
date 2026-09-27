import { useEffect, useState } from 'react';
import { Sparkles, Wand2 } from 'lucide-react';
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
  const [applied, setApplied] = useState<Set<string>>(new Set());
  const [applyingKey, setApplyingKey] = useState<string | null>(null);
  const [applyingAll, setApplyingAll] = useState(false);

  const running = busy || !!taskRunId;

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

  const handleGenerate = async () => {
    setBusy(true);
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

  const pending = result ? result.suggestions.filter((s) => !applied.has(keyOf(s))) : [];

  const handleApplyOne = async (s: BlueprintSuggestion) => {
    const key = keyOf(s);
    setApplyingKey(key);
    try {
      const ok = await applyOne(s);
      if (ok) {
        setApplied((prev) => new Set(prev).add(key));
        addToast(`已应用第 ${s.item_index} 题的${FIELD_LABELS[s.field]}调整`, 'success');
        reload();
      }
    } finally {
      setApplyingKey(null);
    }
  };

  const handleApplyAll = async () => {
    if (pending.length === 0) return;
    setApplyingAll(true);
    let okCount = 0;
    try {
      for (const s of pending) {
        if (await applyOne(s)) {
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
          placeholder="可留空做常规检查；也可写：难题调多一点、第3章分值调高"
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

      {result && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
          <p style={{ fontSize: '0.8rem', lineHeight: 1.7, color: 'var(--text-secondary)' }}>
            {result.summary}
          </p>
          {result.suggestions.length === 0 && (
            <p style={{ fontSize: '0.78rem', color: 'var(--text-tertiary)' }}>
              没有需要调整的题位。
            </p>
          )}
          {result.suggestions.map((s) => {
            const item = planItems.find((p) => p.item_index === s.item_index);
            const isApplied = applied.has(keyOf(s));
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
                  {FIELD_LABELS[s.field]}：{currentLabel(item, s.field, maps)}
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
