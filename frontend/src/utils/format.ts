export function formatDate(dateStr: string): string {
  const d = new Date(dateStr);
  const year = d.getFullYear();
  const month = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

export function formatDateTime(dateStr: string): string {
  const d = new Date(dateStr);
  const year = d.getFullYear();
  const month = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  const hours = String(d.getHours()).padStart(2, '0');
  const minutes = String(d.getMinutes()).padStart(2, '0');
  return `${year}-${month}-${day} ${hours}:${minutes}`;
}

export const MATERIAL_TYPE_LABELS: Record<string, string> = {
  teaching_syllabus: '教学大纲',
  assessment_syllabus: '考核大纲',
  teaching_material: '教学材料',
  exercise: '习题',
};

export const MATERIAL_TYPE_COLORS: Record<string, string> = {
  teaching_syllabus: 'badge-info',
  assessment_syllabus: 'badge-success',
  teaching_material: 'badge-purple',
  exercise: 'badge-warning',
};

/** document_parse_runs 状态 → 中文文案（资料库列表与 AI 助手结果卡共用） */
export const PARSE_STATUS_LABELS: Record<string, string> = {
  queued: '排队中',
  submitted: '解析中',
  waiting_file: '解析中',
  pending: '等待中',
  running: '解析中',
  converting: '转换中',
  ready: '已完成',
  failed: '失败',
};
