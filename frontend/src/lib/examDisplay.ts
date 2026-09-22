/**
 * 试卷项目 / 试卷中心共用的展示常量与映射。
 * 两个页面同属「试卷」模块，题位与状态的呈现口径必须一致，
 * 否则同一份合同在流水线页和试卷页会长得不一样。
 */

export const QUESTION_TYPE_LABELS: Record<string, string> = {
  single_choice: '单选',
  multiple_choice: '多选',
  true_false: '判断',
  fill_blank: '填空',
  short_answer: '简答',
  comprehensive: '综合',
  essay: '论述',
};

/** 卷面分组顺序：未列出的题型排在最后 */
export const QUESTION_TYPE_ORDER = [
  'single_choice',
  'multiple_choice',
  'true_false',
  'fill_blank',
  'short_answer',
  'comprehensive',
  'essay',
];

export const QUESTION_TYPE_OPTIONS = Object.entries(QUESTION_TYPE_LABELS).map(([value, label]) => ({ value, label }));

/** 卷面分组标题：短名是给徽标用的，大题栏要读起来像试卷 */
export const QUESTION_TYPE_SECTION_LABELS: Record<string, string> = {
  single_choice: '单项选择题',
  multiple_choice: '多项选择题',
  true_false: '判断题',
  fill_blank: '填空题',
  short_answer: '简答题',
  comprehensive: '综合题',
  essay: '论述题',
};

export const DIFFICULTY_LABELS: Record<string, string> = {
  easy: '易',
  medium: '中',
  hard: '难',
};

export const DIFFICULTY_OPTIONS = Object.entries(DIFFICULTY_LABELS).map(([value, label]) => ({ value, label }));

export const COGNITIVE_LABELS: Record<string, string> = {
  remember: '记忆',
  understand: '理解',
  apply: '应用',
  analyze: '分析',
  evaluate: '评价',
  create: '创造',
};

/** 试卷项目状态（后端 exam_projects.status） */
export const EXAM_PROJECT_STATUS_META: Record<string, { label: string; variant: 'default' | 'success' | 'warning' | 'error' | 'info' | 'purple' }> = {
  draft: { label: '草稿', variant: 'default' },
  blueprint: { label: '蓝图阶段', variant: 'info' },
  contract: { label: '合同阶段', variant: 'purple' },
  generating: { label: '生成中', variant: 'warning' },
  review: { label: '待审核', variant: 'warning' },
  exported: { label: '已导出', variant: 'success' },
};

/** 试卷版本状态（后端 paper_versions.status） */
export const PAPER_STATUS_META: Record<string, { label: string; variant: 'default' | 'success' | 'warning' | 'error' | 'info' | 'purple' }> = {
  draft: { label: '草稿', variant: 'info' },
  candidate: { label: '待审核', variant: 'warning' },
  finalized: { label: '已定稿', variant: 'success' },
};

export function qlabel(t: string): string {
  return QUESTION_TYPE_LABELS[t] ?? t;
}

export function sectionLabel(t: string): string {
  return QUESTION_TYPE_SECTION_LABELS[t] ?? qlabel(t);
}

export function dlabel(d: string): string {
  return DIFFICULTY_LABELS[d] ?? d;
}

export function clabel(c: string): string {
  return COGNITIVE_LABELS[c] ?? c;
}
