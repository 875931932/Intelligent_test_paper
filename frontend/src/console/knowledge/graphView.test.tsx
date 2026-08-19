import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { GraphView } from './graphView'
import type { GraphLayout, GraphNode } from './graphLayout'

const node = (id: string, type: GraphNode['type'], overrides: Partial<GraphNode> = {}): GraphNode => ({
  id, label: id, type, x: 0, y: 0, r: 10, clusterIndex: 0, grounded: true, importance: 3, conceptCluster: 'c1', ...overrides,
})

const layout: GraphLayout = {
  nodes: [
    node('ep1', 'domain', { x: 100, y: 0, r: 32 }),
    node('u1', 'unit', { x: 100, y: 200, r: 22 }),
    node('c1', 'card', { x: 50, y: 400, r: 16, label: '量化微调' }),
    node('c2', 'card', { x: 150, y: 400, r: 10, grounded: false, label: '未落地卡' }),
  ],
  edges: [
    { source: 'ep1', target: 'u1', kind: 'hierarchical', dashed: false, thick: false },
    { source: 'u1', target: 'c1', kind: 'hierarchical', dashed: false, thick: false },
    { source: 'c1', target: 'c2', kind: 'equivalent_to', dashed: false, thick: true },
  ],
  clusters: ['c1'],
}

describe('GraphView', () => {
  it('renders SVG with nodes and edges', () => {
    render(<GraphView layout={layout} onSelectNode={vi.fn()} selectedId={null} />)
    const svg = document.querySelector('svg')
    expect(svg).toBeInTheDocument()
    const circles = document.querySelectorAll('svg circle')
    expect(circles).toHaveLength(4)
  })

  it('renders node labels', () => {
    render(<GraphView layout={layout} onSelectNode={vi.fn()} selectedId={null} />)
    expect(screen.getByText('量化微调')).toBeInTheDocument()
    expect(screen.getByText('未落地卡')).toBeInTheDocument()
  })

  it('marks ungrounded nodes with dashed red border', () => {
    render(<GraphView layout={layout} onSelectNode={vi.fn()} selectedId={null} />)
    const circles = document.querySelectorAll('svg circle')
    const ungrounded = circles[3] // c2 is 4th
    expect(ungrounded.getAttribute('class')).toContain('ungrounded')
  })

  it('calls onSelectNode when a node is clicked', async () => {
    const onSelect = vi.fn()
    render(<GraphView layout={layout} onSelectNode={onSelect} selectedId={null} />)
    const circles = document.querySelectorAll('svg circle')
    await userEvent.click(circles[2]) // c1
    expect(onSelect).toHaveBeenCalledWith('c1')
  })

  it('highlights selected node', () => {
    render(<GraphView layout={layout} onSelectNode={vi.fn()} selectedId="c1" />)
    const circles = document.querySelectorAll('svg circle')
    expect(circles[2].getAttribute('class')).toContain('selected')
  })
})
