const SIZE_MAP: Record<string, number> = { sm: 16, md: 24, lg: 36 };

export function Spinner({ size = 'md' }: { size?: number | 'sm' | 'md' | 'lg' }) {
  const px = typeof size === 'number' ? size : (SIZE_MAP[size] ?? 24);
  return <div className="spinner" style={{ width: px, height: px }} />;
}
