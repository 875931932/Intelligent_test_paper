// 补证据可选项（来自候选 payload 的 evidence_sources，仅 supporting/background）
export interface SupplementSource {
  evidence_chunk_id: string;
  exam_point_code: string;
  relevance_class: string;
  support_claim: string;
  evidence_role: string;
  confidence: number;
  locator: Record<string, unknown>;
  content?: string;
}

export type ViewMode = 'tree' | 'graph';
export type BuildState = 'idle' | 'building' | 'candidate' | 'published';

export function EvidenceRoleLabel(role: string): string {
  const labels: Record<string, string> = {
    direct: '直接证据', supporting: '支持证据',
    background: '背景证据', out_of_scope: '超出范围',
  };
  return labels[role] || role;
}

export function EvidenceRoleVariant(role: string): string {
  const variants: Record<string, string> = {
    direct: 'success', supporting: 'warning',
    background: 'default', out_of_scope: 'default',
  };
  return variants[role] || 'default';
}

export function formatLocator(loc: Record<string, unknown> | string | null | undefined): string {
  if (!loc) return '';
  if (typeof loc === 'string') return loc;
  const parts: string[] = [];
  const page = loc.page_index;
  if (typeof page === 'number' && page >= 0) parts.push(`第 ${page + 1} 页`);
  const path = loc.heading_path;
  if (typeof path === 'string' && path.trim()) parts.push(path);
  const block = loc.block_type;
  if (typeof block === 'string' && block.trim()) parts.push(block);
  return parts.join(' · ');
}

export interface MaterialVersionOption {
  id: string;
  name: string;
  type: string;
  version: string;
}

export function truncate(s: string | null | undefined, n: number): string {
  if (!s) return '';
  return s.length > n ? s.slice(0, n) + '…' : s;
}
