import { useEffect, useState } from 'react';
import { useToastStore } from '@/stores/toast';
import { api } from '@/api/client';

/** 后台概览：账号/组织/近 24h 调用的核心统计（数据随阶段推进逐步接入）。 */
interface OverviewStats {
  users: { total: number; active: number; by_role: Record<string, number> };
  organizations: { total: number };
  model_calls_24h: { total: number; failed: number };
}

export default function AdminOverviewPage() {
  const addToast = useToastStore((s) => s.addToast);
  const [stats, setStats] = useState<OverviewStats | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const data = await api.admin.overview();
        if (!cancelled) setStats(data);
      } catch {
        if (!cancelled) addToast('概览数据加载失败', 'error');
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [addToast]);

  if (loading) {
    return (
      <div style={{ display: 'flex', justifyContent: 'center', padding: 60 }}>
        <div className="spinner spinner-lg" />
      </div>
    );
  }

  const roleLabel: Record<string, string> = {
    admin: '平台管理员',
    org_admin: '单位管理员',
    teacher: '教师',
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-lg)' }}>
      <h1 style={{ fontSize: '1.5rem', fontWeight: 600, color: 'var(--text)', margin: 0 }}>
        概览
      </h1>

      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fill, minmax(200px, 1fr))',
          gap: 'var(--bento-gap)',
        }}
      >
        {[
          { label: '账号总数', value: stats?.users.total ?? 0 },
          { label: '启用中账号', value: stats?.users.active ?? 0 },
          { label: '单位数', value: stats?.organizations.total ?? 0 },
          { label: '近 24h 模型调用', value: stats?.model_calls_24h.total ?? 0 },
          { label: '近 24h 失败调用', value: stats?.model_calls_24h.failed ?? 0 },
        ].map((card) => (
          <div key={card.label} className="glass-card" style={{ padding: 'var(--space-md)' }}>
            <div style={{ fontSize: 13, color: 'var(--text-secondary)' }}>{card.label}</div>
            <div style={{ fontSize: 28, fontWeight: 600, color: 'var(--text)', marginTop: 6 }}>
              {card.value}
            </div>
          </div>
        ))}
      </div>

      <div className="glass-card" style={{ padding: 'var(--space-md)' }}>
        <div style={{ fontSize: 14, fontWeight: 600, color: 'var(--text)', marginBottom: 10 }}>
          账号构成
        </div>
        <div style={{ display: 'flex', gap: 'var(--space-md)', flexWrap: 'wrap' }}>
          {Object.entries(stats?.users.by_role ?? {}).map(([role, count]) => (
            <span
              key={role}
              className="badge"
              style={{ fontSize: 13, color: 'var(--text-secondary)' }}
            >
              {roleLabel[role] ?? role}：{count}
            </span>
          ))}
          {Object.keys(stats?.users.by_role ?? {}).length === 0 && (
            <span style={{ fontSize: 13, color: 'var(--text-tertiary)' }}>暂无账号</span>
          )}
        </div>
      </div>
    </div>
  );
}
