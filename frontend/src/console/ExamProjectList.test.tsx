import { describe, it, expect } from 'vitest'
import { screen } from '@testing-library/react'
import { renderWithProviders } from '../test/render'
import { ExamProjectList } from './ExamProjectList'

describe('ExamProjectList', () => {
  it('renders the S2 skeleton notice and empty list', () => {
    renderWithProviders(<ExamProjectList />)
    expect(screen.getByText('试卷项目')).toBeInTheDocument()
    expect(screen.getByText(/S2/)).toBeInTheDocument()
    expect(screen.getByText(/暂无试卷项目/)).toBeInTheDocument()
  })
})
