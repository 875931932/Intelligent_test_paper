import { useCallback, useEffect, useState } from 'react';
import { History } from 'lucide-react';
import { api } from '@/api/client';
import { getErrorMessage } from '@/api/errors';
import { useAuthStore } from '@/stores/auth';
import { useToastStore } from '@/stores/toast';
import { Badge } from '@/components/ui/Badge';
import { PAPER_STATUS_META } from '@/lib/examDisplay';
import type { PaperVersionSummary } from '@/types/api';

/**
 * 试卷历史条：列出项目最近 3 份试卷，点一下即把当前卷切过去。
 *
 * 切换只改后端 exam_projects 指针，不碰版本本身：切到已定稿的旧卷仍是
 * 定稿态，要改得先在档案卡上点「撤销定稿」（冻结即不可变的纪律不受影响）。
 * 更早的版本在生成新卷时已被物理删除，所以这里恒为最多 3 枚，无需下拉。
 */
export function PaperHistoryBar({
  courseId, projectId, currentPvId, onChanged,
}: {
  courseId: string;
  projectId: string;
  /** 当前展示的试卷版本 id；切换成功后父级换卷，本条随之高亮 */
  currentPvId: string;
  /** 切换成功：通知父级重新拉取当前试卷 */
  onChanged: () => void;
}) {
  const token = useAuthStore((s) => s.token);
  const addToast = useToastStore((s) => s.addToast);
  const [versions, setVersions] = useState<PaperVersionSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  // 首次加载与「切卷后刷新高亮」都走这条：状态变更全部落在 await 之后，
  // 不在 effect 里同步 setState（loading 初值即 true，无需在 effect 里开）
  const load = useCallback(async () => {
    try {
      const list = await api.paperVersions.listVersions(courseId, projectId, token ?? undefined);
      setVersions(list);
      setError(null);
    } catch (e) {
      setError(getErrorMessage(e));
    } finally {
      setLoading(false);
    }
  }, [courseId, projectId, token]);

  // currentPvId 也要进依赖：切卷后父级换新 pv，「当前」高亮得跟着刷新
  useEffect(() => { void load(); }, [load, currentPvId]);

  const retry = () => {
    setLoading(true);
    setError(null);
    void load();
  };

  const handleActivate = async (v: PaperVersionSummary) => {
    if (v.is_current || busyId) return;
    setBusyId(v.id);
    try {
      await api.paperVersions.activate(courseId, projectId, v.id, token ?? undefined);
      addToast(`已切换到试卷 v${v.version_no}`, 'success');
      onChanged();
    } catch (e) {
      addToast(getErrorMessage(e), 'error');
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="glass-card" style={{ padding: '12px 16px' }}>
      <div style={{ display: 'flex', gap: '10px', alignItems: 'center', flexWrap: 'wrap' }}>
        <span style={{
          display: 'inline-flex', alignItems: 'center', gap: '6px',
          fontSize: '0.78rem', fontWeight: 600, color: 'var(--text-secondary)',
        }}>
          <History size={14} /> 试卷历史
        </span>

        {loading && <span style={{ fontSize: '0.78rem', color: 'var(--text-tertiary)' }}>加载中…</span>}
        {!loading && error && (
          <span style={{ fontSize: '0.78rem', color: 'var(--error)' }}>
            历史加载失败：{error}
            <button
              onClick={retry}
              style={{ marginLeft: 8, background: 'none', border: 'none', color: 'var(--accent)', cursor: 'pointer', fontSize: '0.78rem' }}
            >
              重试
            </button>
          </span>
        )}
        {!loading && !error && versions.length === 0 && (
          <span style={{ fontSize: '0.78rem', color: 'var(--text-tertiary)' }}>暂无历史试卷</span>
        )}

        {!loading && !error && versions.map((v) => {
          const isCurrent = v.id === currentPvId;
          const psm = PAPER_STATUS_META[v.status] ?? { label: v.status, variant: 'default' as const };
          return (
            <button
              key={v.id}
              onClick={() => void handleActivate(v)}
              disabled={isCurrent || !!busyId}
              title={isCurrent ? '当前试卷' : `切换到试卷 v${v.version_no}（${psm.label}）`}
              style={{
                display: 'inline-flex', alignItems: 'center', gap: '6px',
                padding: '5px 12px', borderRadius: 999, cursor: isCurrent ? 'default' : 'pointer',
                fontSize: '0.78rem', fontWeight: 600,
                border: isCurrent ? '1px solid var(--accent)' : '1px solid var(--border)',
                background: isCurrent ? 'var(--accent-subtle)' : 'transparent',
                color: isCurrent ? 'var(--accent)' : 'var(--text-secondary)',
                opacity: busyId && !isCurrent ? 0.5 : 1,
                transition: 'all .15s ease',
              }}
            >
              v{v.version_no}
              <span style={{ fontWeight: 400, color: 'var(--text-tertiary)' }}>{v.item_count} 题</span>
              <Badge variant={psm.variant}>{psm.label}</Badge>
            </button>
          );
        })}

        <span style={{
          marginLeft: 'auto', fontSize: '0.72rem', color: 'var(--text-tertiary)',
          maxWidth: '100%',
        }}>
          仅保留最近 3 份：生成新卷时更早的版本连同题面一并从库中删除
        </span>
      </div>
    </div>
  );
}
