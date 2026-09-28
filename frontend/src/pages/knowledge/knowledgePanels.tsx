import { Network, Plus } from 'lucide-react';
import { Button, ProgressPanel } from '@/components/ui';
import type { ViewMode } from './knowledgeShared';

// ─── Sub-components ───

export function StatsBadge({ icon, color, value, label }: { icon: React.ReactNode; color: string; value: number; label: string }) {
  return (
    <div className="glass-card" style={{ padding: '10px 16px', display: 'flex', alignItems: 'center', gap: '10px' }}>
      <span style={{ color, display: 'flex' }}>{icon}</span>
      <span style={{ fontSize: '1.125rem', fontWeight: 600, letterSpacing: '-0.01em', lineHeight: 1.1 }}>{value}</span>
      <span style={{ fontSize: '0.8125rem', color: 'var(--text-secondary)' }}>{label}</span>
    </div>
  );
}

export function FieldBlock({ label, content, muted }: { label: string; content: string; muted?: boolean }) {
  return (
    <div>
      <h4 style={{ fontSize: '0.75rem', fontWeight: 600, color: 'var(--text-tertiary)', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: '6px' }}>{label}</h4>
      <p style={{
        fontSize: '0.875rem', lineHeight: 1.6, whiteSpace: 'pre-wrap',
        color: muted ? 'var(--text-tertiary)' : 'var(--text)',
        fontStyle: muted ? 'italic' : undefined,
      }}>{content}</p>
    </div>
  );
}

export function ViewToggle({ mode, current, onChange, label, icon: Icon }: {
  mode: ViewMode; current: ViewMode; onChange: (m: ViewMode) => void;
  label: string; icon: React.ComponentType<{ size: number }>;
}) {
  const active = current === mode;
  return (
    <button
      onClick={() => onChange(mode)}
      style={{
        display: 'flex', alignItems: 'center', gap: '6px',
        padding: '6px 12px', borderRadius: 'var(--radius-sm)',
        fontSize: '0.875rem', fontWeight: 500,
        background: active ? 'var(--surface-solid)' : 'transparent',
        boxShadow: active ? '0 1px 4px var(--line)' : 'none',
        color: active ? 'var(--text)' : 'var(--text-tertiary)',
        border: 'none', cursor: 'pointer',
        transition: 'all 0.2s cubic-bezier(0, 0, 0.2, 1)',
      }}
    >
      <Icon size={14} /> {label}
    </button>
  );
}

export function BuildingPanel() {
  return (
    <ProgressPanel
      title="正在构建知识目录，请稍候…"
      messages={[
        '正在组织资料与考点…',
        '正在检索证据与落地关系…',
        '正在生成知识卡片…',
        '正在校验目录一致性…',
      ]}
    />
  );
}

export function IdlePanel({ onBuild }: { onBuild: () => void }) {
  return (
    <div className="glass-panel" style={{ padding: '64px 20px', display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '16px' }}>
      <div style={{ width: 56, height: 56, borderRadius: 'var(--radius-lg)', background: 'var(--accent-subtle)', color: 'var(--accent)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <Network size={28} />
      </div>
      <h3 style={{ fontSize: '1.125rem', fontWeight: 600 }}>尚未构建知识目录</h3>
      <p style={{ fontSize: '0.875rem', color: 'var(--text-secondary)', maxWidth: '420px', textAlign: 'center' }}>
        知识目录将课程资料组织为以考点为核心的知识卡片与证据链，是命题蓝图与合同的基础。
      </p>
      <Button icon={<Plus size={16} />} onClick={onBuild}>构建知识目录</Button>
    </div>
  );
}
