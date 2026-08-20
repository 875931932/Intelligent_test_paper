import { describe, expect, it } from 'vitest'
import { screen } from '@testing-library/react'
import { renderWithProviders } from '../test/render'
import { ProgressFeedback } from './ProgressFeedback'

describe('ProgressFeedback', () => {
  it('renders real percentages and status messages', () => {
    renderWithProviders(
      <ProgressFeedback
        materialProgress={{ percent: 60, status: 'running', message: '3/5 份资料解析中' }}
        blueprintProgress={{ percent: 100, status: 'success', message: '蓝图已确认' }}
        paperProgress={{ percent: 42, status: 'running', message: '正在生成题目' }}
      />,
    )
    expect(screen.getByText('60%')).toBeInTheDocument()
    expect(screen.getByText('蓝图已确认')).toBeInTheDocument()
    expect(screen.getByText('正在生成题目')).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveAttribute('aria-live', 'polite')
  })

  it('exposes failures as an actionable status', () => {
    renderWithProviders(
      <ProgressFeedback
        materialProgress={{ percent: 40, status: 'warning', message: '2/5 份资料已解析', detail: '3 份资料解析失败，请重试' }}
        blueprintProgress={{ percent: 0, status: 'idle', message: '等待生成蓝图' }}
        paperProgress={{ percent: 0, status: 'idle', message: '蓝图确认后生成试卷' }}
      />,
    )
    expect(screen.getByText('需处理')).toBeInTheDocument()
    expect(screen.getByText('2/5 份资料已解析')).toBeInTheDocument()
  })
})
