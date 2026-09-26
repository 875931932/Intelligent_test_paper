import type { PaperExportKind } from '@/api/domains/paperVersions';
import { DIFFICULTY_OPTIONS } from '@/lib/examDisplay';
import type { AiCreateProposal, PaperVersionItem } from '@/types/api';

// ─── 题型与选项工具 ───

/** 有选项需要编辑的题型。判断题答案是布尔值、没有 options 字段，不能混进来 */
export function hasOptions(t: string): boolean {
  return t === 'single_choice' || t === 'multiple_choice';
}

export type OptEntry = { key: string; text: string };

export function optionsToEntries(options?: Record<string, string> | string[]): OptEntry[] {
  if (Array.isArray(options)) {
    return options.map((v, i) => ({ key: String.fromCharCode(65 + i), text: String(v) }));
  }
  return Object.entries(options || {}).map(([k, v]) => ({ key: k, text: String(v) }));
}

export function entriesToOptions(entries: OptEntry[]): Record<string, string> {
  const o: Record<string, string> = {};
  entries.forEach((e, i) => {
    o[e.key || String.fromCharCode(65 + i)] = e.text;
  });
  return o;
}

/**
 * 判断题答案在后端是布尔值（true/false）且没有 options；展示层统一成字符串口径，
 * 否则对布尔值调字符串方法会直接抛 TypeError。
 */
export function normalizeAnswer(answer: unknown): string {
  if (typeof answer === 'boolean') return answer ? '正确' : '错误';
  if (answer == null) return '';
  return String(answer);
}

/**
 * 答案解析成选项字母，与后端 answer_option_keys 同口径：
 * 模型有时返回 'B' / 'ABD'，有时按 schema 约定返回选项原文，教师手写时还会写成
 * '甲、丙' 这种并列原文。只有整串都是选项字母时才按字母解析，否则按原文匹配，
 * 避免把 'LoRA' 里的 L/O/R/A 误当成选项字母。
 */
export function optionKeysOf(answer: unknown, optionTexts: string[]): Set<string> {
  const text = normalizeAnswer(answer);
  if (!text) return new Set();
  const keys = optionTexts.map((_, i) => String.fromCharCode(65 + i));

  const resolveOne = (part: string): Set<string> => {
    const upper = part.toUpperCase();
    if (upper && [...upper].every((c) => keys.includes(c))) return new Set(upper);
    const exact = new Set<string>();
    optionTexts.forEach((t, i) => {
      if (t === part) exact.add(keys[i]);
    });
    return exact;
  };

  const compact = text.toUpperCase().replace(/[,，、；;\s]+/g, '');
  if (compact && [...compact].every((c) => keys.includes(c))) return new Set(compact);
  const parts = text.split(/[,，、；;]+/).filter((p) => p.trim());
  if (parts.length > 1) {
    const resolved = new Set<string>();
    parts.forEach((p) => resolveOne(p.trim()).forEach((k) => resolved.add(k)));
    return resolved;
  }
  return resolveOne(text);
}

/** 在已解析的选项字母上切换（多选累加，单选替换） */
export function toggleAnswerKey(keys: Set<string>, key: string, multi: boolean): string {
  if (!multi) return key;
  const next = new Set(keys);
  if (next.has(key)) next.delete(key);
  else next.add(key);
  return [...next].sort().join('');
}

// ─── 编辑草稿 ───

/** rubric 两态统一成换行文本：生成侧是要点数组，表单按文本编辑 */
export function rubricText(v: string | string[] | null | undefined): string {
  return Array.isArray(v) ? v.join('\n') : (v ?? '');
}

export interface Draft {
  stem: string;
  question_type: string;
  difficulty: string;
  score: string;
  answer: string;
  explanation: string;
  /** 评分细则（主观题）：每行一个要点 */
  rubric: string;
  options: OptEntry[];
}

export interface EditorSubmit {
  stem: string;
  question_type: string;
  difficulty: string;
  score: number;
  /** 判断题提交布尔值（后端该题型强校验 bool），其余题型为字符串 */
  answer: string | boolean;
  explanation: string;
  /** 评分细则（主观题）；原文进 teacher_override / POST items，客观题为空串 */
  rubric: string;
  options: Record<string, string>;
  clear_needs_review?: boolean;
}

/** 编辑框按文本编辑，提交时判断题答案规范化回布尔值 */
export function answerForSubmit(questionType: string, answer: string): string | boolean {
  if (questionType !== 'true_false') return answer;
  const a = answer.trim();
  if (['true', '正确', '对', '是', 'T'].includes(a)) return true;
  if (['false', '错误', '错', '否', 'F'].includes(a)) return false;
  return a;
}

export function draftFromItem(item: PaperVersionItem): Draft {
  return {
    stem: item.stem ?? '',
    question_type: item.question_type || 'short_answer',
    difficulty: item.difficulty || 'medium',
    score: String(item.score ?? 0),
    answer: normalizeAnswer(item.answer),
    explanation: item.explanation ?? '',
    rubric: rubricText(item.rubric),
    options: optionsToEntries(item.options),
  };
}

export const emptyDraft = (): Draft => ({
  stem: '',
  question_type: 'short_answer',
  difficulty: 'medium',
  score: '5',
  answer: '',
  explanation: '',
  rubric: '',
  options: [{ key: 'A', text: '' }, { key: 'B', text: '' }],
});

/**
 * AI 整题提案 → 新增表单草稿：题面字段全量回填，教师微调后走既有「加入试卷」。
 * 分值不进提案（是教师的总分决策），保留表单默认值由教师自己定。
 */
export function draftFromProposal(p: AiCreateProposal): Draft {
  const choice = hasOptions(p.question_type);
  let options = choice ? optionsToEntries(p.options ?? undefined) : [];
  // 选择题提案缺选项时兜底给 A-D 空行，教师可直接补（正常不会发生：校验已拦）
  if (choice && options.length === 0) {
    options = [
      { key: 'A', text: '' }, { key: 'B', text: '' }, { key: 'C', text: '' }, { key: 'D', text: '' },
    ];
  }
  const difficulty = DIFFICULTY_OPTIONS.some((o) => o.value === p.difficulty)
    ? (p.difficulty as string)
    : 'medium';
  return {
    stem: p.stem ?? '',
    question_type: p.question_type || 'short_answer',
    difficulty,
    score: '5',
    answer: normalizeAnswer(p.answer),
    explanation: p.explanation ?? '',
    rubric: rubricText(p.rubric),
    options,
  };
}

/** 可导出的三份卷面 + 答案细则 JSON；前三种同时也是整体预览的页签 */
export type ExportKind = PaperExportKind;
export type PreviewKind = Exclude<ExportKind, 'json'>;

export const PREVIEW_TABS: Array<{ key: PreviewKind; label: string }> = [
  { key: 'student', label: '学生卷' },
  { key: 'card', label: '答题卡' },
  { key: 'answer', label: '答卷（含答案）' },
];