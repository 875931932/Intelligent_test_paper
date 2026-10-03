import { api } from '@/api/client';
import { isTerminal } from '@/pages/paper/stage/stageShared';
import type {
  BlueprintSuggestResult,
  BlueprintSuggestion,
  PlanItem,
  PlanItemChanges,
  TaskRun,
} from '@/types/api';

/**
 * 蓝图题位调整建议的共享实现：试卷页建议面板（教师逐条/批量应用）与助手自动
 * 应用共用同一套「已应用判定 / 逐条 PATCH / 等任务完成」，不开第二份。
 */

/** 建议字段 → 可直接提交给既有 PATCH plan-items 的 changes 形状（后端已归一词表） */
export function toChanges(s: BlueprintSuggestion): PlanItemChanges {
  switch (s.field) {
    case 'score': return { score: Number(s.value) };
    case 'difficulty': return { difficulty: String(s.value) };
    case 'cognitive_level': return { cognitive_level: String(s.value) };
    case 'question_type': return { question_type: String(s.value) };
    case 'exam_point_id': return { exam_point_id: String(s.value) };
    case 'card_id': return { card_id: String(s.value) };
    case 'assessment_mode': return { assessment_mode: String(s.value) };
    default: return {};
  }
}

/** 建议值的唯一键（题位 + 字段） */
export const suggestKey = (s: BlueprintSuggestion): string => `${s.item_index}:${s.field}`;

/** 建议值已等于题位当前值（应用过，或教师已手动改到位）→ 视为已应用 */
export function matchesCurrent(s: BlueprintSuggestion, item: PlanItem | undefined): boolean {
  if (!item) return false;
  if (s.field === 'score') return Math.abs(Number(item.score) - Number(s.value)) < 0.001;
  const current =
    s.field === 'card_id' ? item.knowledge_card_id
    : s.field === 'exam_point_id' ? item.exam_point_id
    : s.field === 'difficulty' ? item.difficulty
    : s.field === 'cognitive_level' ? item.cognitive_level
    : s.field === 'assessment_mode' ? item.assessment_mode
    : item.question_type;
  return String(current ?? '') === String(s.value);
}

/** 「没有本会话已点过的键」的占位集合（模块级常量，避免每次渲染新建） */
export const NO_APPLIED_KEYS: ReadonlySet<string> = new Set<string>();

/**
 * 已应用的统一口径：本会话点过应用、或题位当前值已等于建议值。
 * 恢复历史建议时 applied 必然为空，全靠「值已相等」兜住——否则刷新后会把
 * 已落地条目误报成待办，点「全部应用」又对同一题位重复 PATCH。
 */
export function isSuggestionApplied(
  s: BlueprintSuggestion,
  planItems: PlanItem[],
  applied: ReadonlySet<string> = NO_APPLIED_KEYS,
): boolean {
  return (
    applied.has(suggestKey(s))
    || matchesCurrent(s, planItems.find((p) => p.item_index === s.item_index))
  );
}

/** 尚未落地的建议（教师待办 / 助手待自动应用的都是这一份） */
export function pendingSuggestions(
  result: BlueprintSuggestResult,
  planItems: PlanItem[],
  applied: ReadonlySet<string> = NO_APPLIED_KEYS,
): BlueprintSuggestion[] {
  return result.suggestions.filter((s) => !isSuggestionApplied(s, planItems, applied));
}

/**
 * 逐条应用建议（走既有 PATCH plan-items：0.5 步进、总分合理性、draft 冻结与
 * 课程隔离等服务端校验原样生效，不绕过）。
 *
 * 分值类建议失败即中止：整套 score 建议满足 sum(delta)==0，逐条 PATCH 没有
 * 回滚，继续应用会把总分改偏；其余字段失败则跳过继续。
 */
export async function applySuggestions(
  courseId: string,
  planItems: PlanItem[],
  suggestions: BlueprintSuggestion[],
  token?: string,
): Promise<{ appliedKeys: string[]; failed: number }> {
  const appliedKeys: string[] = [];
  let failed = 0;
  for (const s of suggestions) {
    const item = planItems.find((p) => p.item_index === s.item_index);
    if (!item) {
      failed += 1;
      continue;
    }
    try {
      await api.examProjects.updatePlanItem(courseId, item.id, toChanges(s), token);
      appliedKeys.push(suggestKey(s));
    } catch {
      failed += 1;
      if (s.field === 'score') break; // 分值成对改动：偏斜不可回滚，立即止损
    }
  }
  return { appliedKeys, failed };
}

/** 建议任务的轮询参数（与试卷页面板同款节奏） */
const SUGGEST_POLL_INTERVAL_MS = 1500;
const SUGGEST_POLL_TIMEOUT_MS = 180_000;

/**
 * 等建议任务跑到终态并取回结果；失败/取消/超时返回 null。
 * 轮询瞬时错误忽略、下一轮重试（与试卷页面板同语义）。
 */
export async function waitSuggestTask(
  courseId: string,
  taskRunId: string,
  token?: string,
): Promise<BlueprintSuggestResult | null> {
  const deadline = Date.now() + SUGGEST_POLL_TIMEOUT_MS;
  while (Date.now() < deadline) {
    await new Promise((resolve) => window.setTimeout(resolve, SUGGEST_POLL_INTERVAL_MS));
    try {
      const tr: TaskRun = await api.examProjects.getTaskRun(courseId, taskRunId, token);
      if (!isTerminal(tr.status)) continue;
      if (tr.status !== 'succeeded' || !tr.result) return null;
      return tr.result as unknown as BlueprintSuggestResult;
    } catch {
      /* 瞬时轮询错误忽略，下一轮重试 */
    }
  }
  return null;
}

/**
 * 发起建议任务 → 等完成 → 应用全部待办建议（助手自动应用走这条）。
 * 返回 total = 待办条数（0 = 无需调整），failed > 0 表示有建议没落地，
 * 调用方应停止自动接力、交教师到试卷页处理。
 */
export async function runSuggestAndApplyAll(
  courseId: string,
  projectId: string,
  instruction: string,
  token?: string,
  onProgress?: (text: string) => void,
): Promise<{ applied: number; failed: number; total: number; error?: string }> {
  const { task_run_id } = await api.examProjects.suggestBlueprintAdjustments(
    courseId, projectId, instruction, token,
  );
  onProgress?.('正在生成蓝图调整建议…');
  const result = await waitSuggestTask(courseId, task_run_id, token);
  if (!result) return { applied: 0, failed: 0, total: 0, error: 'AI 调整建议未完成' };
  const planItems = await api.examProjects.getPlanItems(courseId, projectId, token);
  const pending = pendingSuggestions(result, planItems);
  if (pending.length === 0) return { applied: 0, failed: 0, total: 0 };
  onProgress?.(`正在应用 ${pending.length} 条调整建议…`);
  const { appliedKeys, failed } = await applySuggestions(courseId, planItems, pending, token);
  return { applied: appliedKeys.length, failed, total: pending.length };
}