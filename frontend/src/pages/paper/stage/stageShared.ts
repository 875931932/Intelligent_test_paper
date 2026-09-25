import type { NameMaps } from '@/hooks/useNameMaps';
import { friendlyId } from '@/lib/format';

export type StageKey = 'blueprint' | 'contract' | 'generate';
export type ToastType = 'success' | 'error' | 'info';
export type ToastFn = (message: string, type?: ToastType) => void;

// 任务状态口径与后端 ck_task_runs_status 对齐（schema.py）。轮询停不停、
// 能否再次发起，都只认这两组，避免各自硬编码出不同的终态集合。
export const IN_FLIGHT_STATUSES = new Set(['queued', 'running', 'waiting_external']);
export const TERMINAL_TASK_STATUSES = new Set(['succeeded', 'failed', 'cancelled']);
export const isInFlight = (s: string) => IN_FLIGHT_STATUSES.has(s);
export const isTerminal = (s: string) => TERMINAL_TASK_STATUSES.has(s);

// 名称降级链：映射名 → 业务 code 原文 → 友好占位（裸 ID 永不渲染，真值走单元格 title）
export function examPointLabel(maps: NameMaps, id: string): string {
  return maps.examPoints[id] || friendlyId(id, '未匹配考点');
}

export function anchorLabel(maps: NameMaps, key: string): string {
  return maps.anchors[key] || friendlyId(key, '未匹配范围');
}

export function cardLabel(maps: NameMaps, id: string): string {
  return maps.cards[id] || friendlyId(id, '未匹配知识卡');
}
