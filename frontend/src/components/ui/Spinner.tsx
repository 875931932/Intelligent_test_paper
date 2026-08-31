const SIZE_MAP: Record<string, number> = { sm: 16, md: 24, lg: 36 };

export function Spinner({ size = 'md' }: { size?: number | 'sm' | 'md' | 'lg' }) {
  const px = typeof size === 'number' ? size : (SIZE_MAP[size] ?? 24);
  return <div className="spinner" style={{ width: px, height: px }} />;
}

export function SpinnerOverlay({ label = '加载中...' }: { label?: string }) {
  return (
    <div style={{
      display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center',
      gap: '16px', padding: '80px 20px', color: 'var(--text-secondary)',
    }}>
      <div className="spinner spinner-lg" />
      <span style={{ fontSize: '0.875rem' }}>{label}</span>
    </div>
  );
}
