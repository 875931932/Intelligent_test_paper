import { type ReactNode } from 'react';

// 蓝图 / 合同 / 生成三段共用的阶段标题行：左标题 + 右侧动作位。
// 单独成文件：组件与纯工具函数不同文件导出，保住 Fast Refresh 的整文件热替换。
export function StageHeading({ title, right }: { title: string; right?: ReactNode }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '4px' }}>
      <h3 style={{ fontWeight: 600, fontSize: '1rem', letterSpacing: '-0.01em' }}>{title}</h3>
      {right}
    </div>
  );
}
