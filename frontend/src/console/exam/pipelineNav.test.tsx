import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { PipelineNav } from './pipelineNav'
import type { PipelineStage } from '../types'

describe('PipelineNav', () => {
  it('renders 5 stage dots with labels', () => {
    render(<PipelineNav current="blueprint" completed={[]} onJump={vi.fn()} />)
    expect(screen.getByText('蓝图')).toBeInTheDocument()
    expect(screen.getByText('合同')).toBeInTheDocument()
    expect(screen.getByText('生成')).toBeInTheDocument()
    expect(screen.getByText('审核')).toBeInTheDocument()
    expect(screen.getByText('导出')).toBeInTheDocument()
  })

  it('marks current stage as active', () => {
    render(<PipelineNav current="contract" completed={['blueprint']} onJump={vi.fn()} />)
    const contractDot = screen.getByText('合同').closest('.stage-dot')
    expect(contractDot?.classList.contains('active')).toBe(true)
  })

  it('marks completed stages with done class', () => {
    render(<PipelineNav current="contract" completed={['blueprint']} onJump={vi.fn()} />)
    const blueprintDot = screen.getByText('蓝图').closest('.stage-dot')
    expect(blueprintDot?.classList.contains('done')).toBe(true)
  })

  it('calls onJump when a completed stage is clicked', async () => {
    const onJump = vi.fn()
    const user = userEvent.setup()
    render(<PipelineNav current="contract" completed={['blueprint']} onJump={onJump} />)
    await user.click(screen.getByText('蓝图'))
    expect(onJump).toHaveBeenCalledWith('blueprint')
  })

  it('does not call onJump for future stages', async () => {
    const onJump = vi.fn()
    const user = userEvent.setup()
    render(<PipelineNav current="blueprint" completed={[]} onJump={onJump} />)
    await user.click(screen.getByText('导出'))
    expect(onJump).not.toHaveBeenCalled()
  })
})
