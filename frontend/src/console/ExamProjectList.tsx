import { Card, EmptyState, Notice } from './ui'

export function ExamProjectList() {
  return (
    <div className="content-inner">
      <div className="page-head">
        <h2>试卷项目</h2>
        <div className="desc">按学期归档的单次命题对象。进入后是 5 阶段生产线。</div>
      </div>
      <Notice kind="info">
        试卷项目服务（蓝图 → 合同 → 生成 → 审核 → 导出 5 阶段生产线）将在 S2 交付。当前为入口骨架。
      </Notice>
      <Card title="项目列表">
        <EmptyState>暂无试卷项目（后端 PaperVersion 内核待落地）</EmptyState>
      </Card>
    </div>
  )
}
