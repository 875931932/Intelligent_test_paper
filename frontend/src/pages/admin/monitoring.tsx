import { useCallback, useEffect, useState } from 'react';
import {
  Bar,
  BarChart,
  CartesianGrid,
  ComposedChart,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { RefreshCw, ChevronLeft, ChevronRight, X } from 'lucide-react';
import { useAuthStore } from '@/stores/auth';
import { useToastStore } from '@/stores/toast';
import { api } from '@/api/client';
import type {
  MonitorCall,
  MonitorCallDetail,
  MonitorCallPage,
  MonitorGroup,
  MonitorGroupBy,
  MonitorOverview,
} from '@/api/domains/admin';
import { Button, Card, Input, Select, BadgeSuccess, BadgeWarning, BadgeInfo } from '@/components/ui';

type RangeKey = '24h' | '7d' | '30d' | 'custom';

const RANGE_LABEL: Record<RangeKey, string> = { '24h': '近 24 小时', '7d': '近 7 天', '30d': '近 30 天', custom: '自定义' };

const GROUP_LABEL: Record<MonitorGroupBy, string> = {
  model: '按模型',
  stage: '按阶段',
  status: '按状态',
  error_code: '按错误码',
};

function fmtTime(iso: string | null | undefined): string {
  if (!iso) return '—';
  return iso.slice(0, 19).replace('T', ' ');
}

function fmtBucket(bucket: string): string {
  // "2026-10-10T07:00:00" → "10-10 07:00"
  return bucket.slice(5, 16).replace('T', ' ');
}

function fmtDuration(ms: number | null): string {
  return ms == null ? '—' : ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`;
}

/** 时间窗纯函数：每次 load 调用现算（刷新即移动窗口），避免 memo 依赖间接层。 */
function buildRange(rangeKey: RangeKey, customFrom: string, customTo: string): { from: string; to: string } {
  const to = new Date();
  if (rangeKey === 'custom') {
    const from = customFrom ? new Date(customFrom) : new Date(to.getTime() - 7 * 86400_000);
    const end = customTo ? new Date(customTo) : to;
    return { from: from.toISOString(), to: end.toISOString() };
  }
  const hours = rangeKey === '24h' ? 24 : rangeKey === '7d' ? 24 * 7 : 24 * 30;
  return { from: new Date(to.getTime() - hours * 3600_000).toISOString(), to: to.toISOString() };
}

function KpiCard({ title, value, hint }: { title: string; value: string; hint?: string }) {
  return (
    <div
      style={{
        flex: '1 1 160px',
        background: 'var(--surface)',
        border: '1px solid var(--line)',
        borderRadius: 'var(--radius-md)',
        padding: '14px 16px',
      }}
    >
      <div style={{ fontSize: '0.75rem', color: 'var(--text-secondary)' }}>{title}</div>
      <div style={{ fontSize: '1.5rem', fontWeight: 650, marginTop: 4 }}>{value}</div>
      {hint && <div style={{ fontSize: '0.6875rem', color: 'var(--text-tertiary)', marginTop: 2 }}>{hint}</div>}
    </div>
  );
}

/** 模型调用监控看板（设计 §6.3）：指标卡/趋势/分组/失败列表/明细抽屉。 */
export default function AdminMonitoringPage() {
  const role = useAuthStore((s) => s.user?.role);
  const isPlatform = role === 'admin';
  const addToast = useToastStore((s) => s.addToast);

  const [rangeKey, setRangeKey] = useState<RangeKey>('7d');
  const [customFrom, setCustomFrom] = useState('');
  const [customTo, setCustomTo] = useState('');
  const [autoRefresh, setAutoRefresh] = useState(false);

  const [group, setGroup] = useState<MonitorGroupBy>('model');
  const [page, setPage] = useState(1);
  const [statusFilter, setStatusFilter] = useState('');
  const [modelInput, setModelInput] = useState('');
  const [modelFilter, setModelFilter] = useState('');
  const [stageInput, setStageInput] = useState('');
  const [stageFilter, setStageFilter] = useState('');

  const [overview, setOverview] = useState<MonitorOverview | null>(null);
  const [series, setSeries] = useState<{ bucket: string; calls: number; success_rate: number }[]>([]);
  const [groups, setGroups] = useState<MonitorGroup[]>([]);
  const [errorGroups, setErrorGroups] = useState<MonitorGroup[]>([]);
  const [recentFailures, setRecentFailures] = useState<MonitorCall[]>([]);
  const [calls, setCalls] = useState<MonitorCallPage | null>(null);
  const [detail, setDetail] = useState<MonitorCallDetail | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    const range = buildRange(rangeKey, customFrom, customTo);
    const bucket = rangeKey === '24h' ? 'hour' : 'day';
    try {
      const [ov, ts, gr, err, rf, cl] = await Promise.all([
        api.admin.monitorOverview(range),
        api.admin.monitorTimeseries({ ...range, bucket }),
        api.admin.monitorBreakdown({ ...range, group_by: group }),
        api.admin.monitorBreakdown({ ...range, group_by: 'error_code' }),
        api.admin.monitorCalls({ ...range, status: 'failed', page: 1, page_size: 5 }),
        api.admin.monitorCalls({
          ...range,
          page,
          page_size: 20,
          ...(statusFilter ? { status: statusFilter } : {}),
          ...(modelFilter ? { model: modelFilter } : {}),
          ...(stageFilter ? { stage: stageFilter } : {}),
        }),
      ]);
      setOverview(ov);
      setSeries(ts);
      setGroups(gr);
      setErrorGroups(err);
      setRecentFailures(rf.items);
      setCalls(cl);
    } catch {
      addToast('监控数据加载失败', 'error');
    } finally {
      setLoading(false);
    }
  }, [rangeKey, customFrom, customTo, group, page, statusFilter, modelFilter, stageFilter, addToast]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (!autoRefresh) return;
    const timer = setInterval(() => void load(), 30_000);
    return () => clearInterval(timer);
  }, [autoRefresh, load]);

  const openDetail = async (id: string) => {
    try {
      setDetail(await api.admin.monitorCall(id));
    } catch (e) {
      addToast((e as Error).message || '调用详情加载失败', 'error');
    }
  };

  if (loading) {
    return (
      <div style={{ display: 'flex', justifyContent: 'center', padding: 60 }}>
        <div className="spinner spinner-lg" />
      </div>
    );
  }

  const totalTokens = overview?.total_tokens ?? 0;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-lg)' }}>
      {/* 顶部：标题 + 时间范围 + 自动刷新 */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: 8 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <h1 style={{ fontSize: '1.5rem', fontWeight: 600, margin: 0 }}>模型调用监控</h1>
          {!isPlatform && <BadgeInfo>本单位视图</BadgeInfo>}
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <div style={{ display: 'flex', gap: 4 }}>
            {(['24h', '7d', '30d', 'custom'] as RangeKey[]).map((key) => (
              <Button
                key={key}
                size="sm"
                variant={rangeKey === key ? 'primary' : 'secondary'}
                onClick={() => setRangeKey(key)}
              >
                {RANGE_LABEL[key]}
              </Button>
            ))}
          </div>
          {rangeKey === 'custom' && (
            <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
              <input
                type="datetime-local"
                className="input-field"
                style={{ fontSize: 12, width: 190 }}
                value={customFrom}
                onChange={(e) => setCustomFrom(e.target.value)}
              />
              <span style={{ color: 'var(--text-tertiary)' }}>→</span>
              <input
                type="datetime-local"
                className="input-field"
                style={{ fontSize: 12, width: 190 }}
                value={customTo}
                onChange={(e) => setCustomTo(e.target.value)}
              />
            </div>
          )}
          <Button
            size="sm"
            variant={autoRefresh ? 'primary' : 'secondary'}
            onClick={() => setAutoRefresh((v) => !v)}
          >
            自动刷新{autoRefresh ? ' ·30s' : ''}
          </Button>
          <Button
            size="sm"
            variant="secondary"
            icon={<RefreshCw size={14} />}
            onClick={() => void load()}
          >
            刷新
          </Button>
        </div>
      </div>

      {/* 第一行：指标卡 ×6 */}
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 12 }}>
        <KpiCard title="调用量" value={(overview?.total_calls ?? 0).toLocaleString()} hint={RANGE_LABEL[rangeKey]} />
        <KpiCard
          title="成功率"
          value={`${((overview?.success_rate ?? 0) * 100).toFixed(1)}%`}
          hint={`成功 ${overview?.succeeded ?? 0} / 失败 ${overview?.failed ?? 0}`}
        />
        <KpiCard
          title="Tokens（入+出）"
          value={totalTokens.toLocaleString()}
          hint={`入 ${overview?.input_tokens ?? 0} / 出 ${overview?.output_tokens ?? 0}`}
        />
        <KpiCard
          title="成本估算"
          value={`¥${(overview?.cost_estimate ?? 0).toFixed(4)}`}
          hint="单价未配置的型号不计价"
        />
        <KpiCard title="平均耗时" value={fmtDuration(overview?.avg_duration_ms ?? null)} />
        <KpiCard
          title="P95 耗时"
          value={fmtDuration(overview?.p95_duration_ms ?? null)}
          hint={overview?.p95_duration_ms == null ? '分位数需 PostgreSQL' : undefined}
        />
      </div>

      {/* 第二行：趋势 */}
      <Card title="调用趋势" subtitle="柱=调用量（左轴），线=成功率（右轴）">
        {series.length === 0 ? (
          <p style={{ fontSize: 14, color: 'var(--text-secondary)', margin: 0 }}>该时间范围内暂无调用。</p>
        ) : (
          <ResponsiveContainer width="100%" height={260}>
            <ComposedChart data={series} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--line-soft)" />
              <XAxis dataKey="bucket" tickFormatter={fmtBucket} fontSize={11} />
              <YAxis yAxisId="left" fontSize={11} allowDecimals={false} />
              <YAxis yAxisId="right" orientation="right" domain={[0, 1]} tickFormatter={(v) => `${Math.round(v * 100)}%`} fontSize={11} />
              <Tooltip
                labelFormatter={(label) => fmtBucket(String(label))}
                formatter={(value, name) =>
                  name === 'success_rate' ? [`${(Number(value) * 100).toFixed(1)}%`, '成功率'] : [value, '调用量']
                }
              />
              <Bar yAxisId="left" dataKey="calls" fill="var(--accent, #4f7cff)" radius={[3, 3, 0, 0]} />
              <Line yAxisId="right" dataKey="success_rate" stroke="#10b981" dot={false} strokeWidth={2} />
            </ComposedChart>
          </ResponsiveContainer>
        )}
      </Card>

      {/* 第三行：分组统计 + 错误分布 */}
      <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 3fr) minmax(0, 2fr)', gap: 'var(--space-lg)' }}>
        <Card
          title="分组统计"
          actions={
            <div style={{ display: 'flex', gap: 4 }}>
              {(['model', 'stage', 'error_code'] as MonitorGroupBy[]).map((key) => (
                <Button key={key} size="sm" variant={group === key ? 'primary' : 'secondary'} onClick={() => setGroup(key)}>
                  {GROUP_LABEL[key]}
                </Button>
              ))}
            </div>
          }
        >
          <div style={{ overflowX: 'auto' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
              <thead>
                <tr style={{ borderBottom: '1px solid var(--line)' }}>
                  {['分组', '调用量', '成功率', 'Tokens', '成本', '均耗时'].map((h) => (
                    <th key={h} style={{ textAlign: 'left', padding: '8px 8px', fontSize: 12, fontWeight: 600, color: 'var(--text-secondary)', background: 'var(--surface-sunken)' }}>
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {groups.map((g) => (
                  <tr key={g.key || '(空)'} style={{ borderBottom: '1px solid var(--line-soft)' }}>
                    <td style={{ padding: '8px', fontFamily: 'monospace' }}>{g.key || '—'}</td>
                    <td style={{ padding: '8px' }}>{g.calls.toLocaleString()}</td>
                    <td style={{ padding: '8px' }}>{(g.success_rate * 100).toFixed(1)}%</td>
                    <td style={{ padding: '8px', color: 'var(--text-secondary)' }}>
                      {(g.input_tokens + g.output_tokens).toLocaleString()}
                    </td>
                    <td style={{ padding: '8px' }}>¥{g.cost_estimate.toFixed(4)}</td>
                    <td style={{ padding: '8px', color: 'var(--text-secondary)' }}>{fmtDuration(g.avg_duration_ms)}</td>
                  </tr>
                ))}
                {groups.length === 0 && (
                  <tr>
                    <td colSpan={6} style={{ padding: 16, color: 'var(--text-secondary)' }}>
                      该时间范围内暂无数据。
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </Card>

        <Card title="错误分布" subtitle="Top 错误码（按调用量）">
          {errorGroups.length === 0 ? (
            <p style={{ fontSize: 14, color: 'var(--text-secondary)', margin: 0 }}>该时间范围内没有失败调用。</p>
          ) : (
            <ResponsiveContainer width="100%" height={240}>
              <BarChart data={errorGroups.slice(0, 8).map((g) => ({ key: g.key || '—', calls: g.calls }))} layout="vertical" margin={{ left: 8, right: 16 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--line-soft)" />
                <XAxis type="number" fontSize={11} allowDecimals={false} />
                <YAxis type="category" dataKey="key" width={150} fontSize={10} />
                <Tooltip />
                <Bar dataKey="calls" fill="#ef4444" radius={[0, 3, 3, 0]} />
              </BarChart>
            </ResponsiveContainer>
          )}
        </Card>
      </div>

      {/* 第四行：最近失败 */}
      <Card title="最近失败" subtitle="点击行查看调用详情">
        {recentFailures.length === 0 ? (
          <p style={{ fontSize: 14, color: 'var(--text-secondary)', margin: 0 }}>暂无失败调用。</p>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column' }}>
            {recentFailures.map((c) => (
              <button
                key={c.id}
                type="button"
                onClick={() => void openDetail(c.id)}
                style={{
                  display: 'flex',
                  gap: 16,
                  alignItems: 'center',
                  padding: '8px 4px',
                  background: 'none',
                  border: 'none',
                  borderBottom: '1px solid var(--line-soft)',
                  cursor: 'pointer',
                  fontSize: 13,
                  textAlign: 'left',
                  color: 'var(--text)',
                }}
              >
                <span style={{ color: 'var(--text-secondary)', minWidth: 130 }}>{fmtTime(c.created_at)}</span>
                <span style={{ fontFamily: 'monospace', minWidth: 140 }}>{c.stage}</span>
                <span style={{ fontFamily: 'monospace', flex: 1 }}>{c.model}</span>
                <BadgeWarning>{c.error_code || 'failed'}</BadgeWarning>
              </button>
            ))}
          </div>
        )}
      </Card>

      {/* 明细表 */}
      <Card
        title="调用明细"
        subtitle={calls ? `共 ${calls.total} 条 · 第 ${calls.page} 页` : undefined}
        actions={
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            <Select
              value={statusFilter}
              onChange={(e) => {
                setStatusFilter(e.target.value);
                setPage(1);
              }}
              options={[
                { value: '', label: '全部状态' },
                { value: 'succeeded', label: '成功' },
                { value: 'failed', label: '失败' },
              ]}
            />
            <Input
              placeholder="型号（回车筛选）"
              value={modelInput}
              onChange={(e) => setModelInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  setModelFilter(modelInput.trim());
                  setPage(1);
                }
              }}
              style={{ width: 170 }}
            />
            <Input
              placeholder="阶段（回车筛选）"
              value={stageInput}
              onChange={(e) => setStageInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  setStageFilter(stageInput.trim());
                  setPage(1);
                }
              }}
              style={{ width: 170 }}
            />
          </div>
        }
      >
        <div style={{ overflowX: 'auto' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
            <thead>
              <tr style={{ borderBottom: '1px solid var(--line)' }}>
                {['时间', '阶段', '型号', '状态', 'Tokens(入/出)', '耗时', '错误码'].map((h) => (
                  <th key={h} style={{ textAlign: 'left', padding: '8px', fontSize: 12, fontWeight: 600, color: 'var(--text-secondary)', background: 'var(--surface-sunken)' }}>
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {(calls?.items ?? []).map((c) => (
                <tr
                  key={c.id}
                  onClick={() => void openDetail(c.id)}
                  style={{ borderBottom: '1px solid var(--line-soft)', cursor: 'pointer' }}
                >
                  <td style={{ padding: '8px', whiteSpace: 'nowrap' }}>{fmtTime(c.created_at)}</td>
                  <td style={{ padding: '8px', fontFamily: 'monospace' }}>{c.stage}</td>
                  <td style={{ padding: '8px', fontFamily: 'monospace' }}>{c.model}</td>
                  <td style={{ padding: '8px' }}>
                    {c.status === 'succeeded' ? <BadgeSuccess>成功</BadgeSuccess> : <BadgeWarning>失败</BadgeWarning>}
                  </td>
                  <td style={{ padding: '8px', color: 'var(--text-secondary)' }}>
                    {c.input_tokens ?? '—'} / {c.output_tokens ?? '—'}
                  </td>
                  <td style={{ padding: '8px', color: 'var(--text-secondary)' }}>{fmtDuration(c.duration_ms)}</td>
                  <td style={{ padding: '8px', fontFamily: 'monospace', fontSize: 12, color: 'var(--text-secondary)' }}>
                    {c.error_code || '—'}
                  </td>
                </tr>
              ))}
              {(calls?.items.length ?? 0) === 0 && (
                <tr>
                  <td colSpan={7} style={{ padding: 16, color: 'var(--text-secondary)' }}>
                    没有符合条件的调用记录。
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 12 }}>
          <Button
            size="sm"
            variant="secondary"
            icon={<ChevronLeft size={14} />}
            disabled={page <= 1}
            onClick={() => setPage((p) => Math.max(1, p - 1))}
          >
            上一页
          </Button>
          <Button
            size="sm"
            variant="secondary"
            icon={<ChevronRight size={14} />}
            disabled={(calls?.page ?? 1) * (calls?.page_size ?? 20) >= (calls?.total ?? 0)}
            onClick={() => setPage((p) => p + 1)}
          >
            下一页
          </Button>
        </div>
      </Card>

      {/* 明细抽屉 */}
      {detail && (
        <div
          onClick={() => setDetail(null)}
          style={{
            position: 'fixed',
            inset: 0,
            background: 'rgba(0,0,0,0.35)',
            zIndex: 60,
            display: 'flex',
            justifyContent: 'flex-end',
          }}
        >
          <aside
            onClick={(e) => e.stopPropagation()}
            style={{
              width: 'min(480px, 92vw)',
              height: '100%',
              background: 'var(--surface)',
              borderLeft: '1px solid var(--line)',
              padding: 20,
              overflowY: 'auto',
              display: 'flex',
              flexDirection: 'column',
              gap: 14,
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <h2 style={{ fontSize: '1.05rem', fontWeight: 600, margin: 0 }}>调用详情</h2>
              <Button size="sm" variant="ghost" icon={<X size={16} />} onClick={() => setDetail(null)} />
            </div>

            <div style={{ display: 'grid', gridTemplateColumns: '96px 1fr', gap: '6px 12px', fontSize: 13 }}>
              <span style={{ color: 'var(--text-secondary)' }}>时间</span>
              <span>{fmtTime(detail.created_at)}</span>
              <span style={{ color: 'var(--text-secondary)' }}>阶段</span>
              <span style={{ fontFamily: 'monospace' }}>{detail.stage}</span>
              <span style={{ color: 'var(--text-secondary)' }}>供应商</span>
              <span>{detail.provider}</span>
              <span style={{ color: 'var(--text-secondary)' }}>型号</span>
              <span style={{ fontFamily: 'monospace' }}>{detail.model}</span>
              <span style={{ color: 'var(--text-secondary)' }}>状态</span>
              <span>
                {detail.status === 'succeeded' ? <BadgeSuccess>成功</BadgeSuccess> : <BadgeWarning>失败</BadgeWarning>}
              </span>
              <span style={{ color: 'var(--text-secondary)' }}>耗时</span>
              <span>{fmtDuration(detail.duration_ms)}</span>
              <span style={{ color: 'var(--text-secondary)' }}>Tokens</span>
              <span>
                {detail.input_tokens ?? '—'} / {detail.output_tokens ?? '—'}
              </span>
              <span style={{ color: 'var(--text-secondary)' }}>课程</span>
              <span style={{ fontFamily: 'monospace', fontSize: 12 }}>{detail.course_id}</span>
              <span style={{ color: 'var(--text-secondary)' }}>request_id</span>
              <span style={{ fontFamily: 'monospace', fontSize: 12, wordBreak: 'break-all' }}>{detail.request_id || '—'}</span>
              <span style={{ color: 'var(--text-secondary)' }}>prompt_hash</span>
              <span style={{ fontFamily: 'monospace', fontSize: 12 }}>{detail.prompt_hash || '—'}</span>
            </div>

            {detail.error_code && (
              <div
                style={{
                  padding: '10px 12px',
                  borderRadius: 'var(--radius-sm)',
                  background: 'rgba(239,68,68,0.08)',
                  border: '1px solid rgba(239,68,68,0.25)',
                  fontSize: 13,
                }}
              >
                <div style={{ fontWeight: 600, fontFamily: 'monospace' }}>{detail.error_code}</div>
                <div style={{ color: 'var(--text-secondary)', marginTop: 4, wordBreak: 'break-word' }}>
                  {detail.error_message || '—'}
                </div>
              </div>
            )}

            <div>
              <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-secondary)', marginBottom: 6 }}>
                调用参数（已脱敏，不含模型响应原文）
              </div>
              <pre
                style={{
                  margin: 0,
                  padding: 12,
                  background: 'var(--surface-sunken)',
                  borderRadius: 'var(--radius-sm)',
                  fontSize: 12,
                  whiteSpace: 'pre-wrap',
                  wordBreak: 'break-word',
                }}
              >
                {Object.keys(detail.details).length ? JSON.stringify(detail.details, null, 2) : '（无）'}
              </pre>
            </div>
          </aside>
        </div>
      )}
    </div>
  );
}
