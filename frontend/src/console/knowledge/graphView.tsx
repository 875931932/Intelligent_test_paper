import { useState, useCallback } from 'react'
import type { GraphLayout, GraphNode } from './graphLayout'

interface Props {
  layout: GraphLayout
  onSelectNode: (id: string) => void
  selectedId: string | null
}

const CLUSTER_COLORS = [
  '#0a84ff', '#30d158', '#ff9f0a', '#bf5af2',
  '#ff453a', '#64d2ff', '#ffd60a', '#5e5ce6',
]

export function GraphView({ layout, onSelectNode, selectedId }: Props) {
  const [zoom, setZoom] = useState(1)

  const minX = Math.min(...layout.nodes.map((n) => n.x)) - 60
  const minY = Math.min(...layout.nodes.map((n) => n.y)) - 60
  const maxX = Math.max(...layout.nodes.map((n) => n.x)) + 60
  const maxY = Math.max(...layout.nodes.map((n) => n.y)) + 60
  const width = maxX - minX
  const height = maxY - minY

  const handleWheel = useCallback((e: React.WheelEvent) => {
    e.preventDefault()
    setZoom((z) => Math.max(0.3, Math.min(3, z - e.deltaY * 0.001)))
  }, [])

  return (
    <div className="graph-canvas" onWheel={handleWheel}>
      <div className="graph-controls">
        <button className="btn ghost" onClick={() => setZoom((z) => Math.min(3, z + 0.2))}>+</button>
        <button className="btn ghost" onClick={() => setZoom((z) => Math.max(0.3, z - 0.2))}>−</button>
        <button className="btn ghost" onClick={() => setZoom(1)}>复位</button>
      </div>
      <svg
        width="100%"
        height="100%"
        viewBox={`${minX} ${minY} ${width} ${height}`}
        style={{ transform: `scale(${zoom})`, transformOrigin: 'center' }}
      >
        <defs>
          <marker id="arrow" markerWidth="8" markerHeight="8" refX="8" refY="4" orient="auto">
            <path d="M0,0 L8,4 L0,8 z" fill="var(--text-muted)" />
          </marker>
          <marker id="arrow-thick" markerWidth="8" markerHeight="8" refX="8" refY="4" orient="auto">
            <path d="M0,0 L8,4 L0,8 z" fill="var(--accent-500)" />
          </marker>
        </defs>
        {layout.edges.map((edge, i) => {
          const source = layout.nodes.find((n) => n.id === edge.source)
          const target = layout.nodes.find((n) => n.id === edge.target)
          if (!source || !target) return null
          const strokeClass = edge.thick ? 'edge-thick' : edge.dashed ? 'edge-dashed' : 'edge-solid'
          return (
            <line
              key={`edge-${i}`}
              x1={source.x}
              y1={source.y}
              x2={target.x}
              y2={target.y}
              className={`graph-edge ${strokeClass}`}
              markerEnd={edge.kind === 'hierarchical' || edge.kind === 'specializes' || edge.kind === 'requires' || edge.kind === 'component_of' ? 'url(#arrow)' : undefined}
            />
          )
        })}
        {layout.nodes.map((node) => {
          const color = node.type === 'card' && node.clusterIndex >= 0
            ? CLUSTER_COLORS[node.clusterIndex % CLUSTER_COLORS.length]
            : 'var(--surface)'
          const classes = [
            'graph-node',
            `node-${node.type}`,
            node.grounded ? '' : 'ungrounded',
            selectedId === node.id ? 'selected' : '',
          ].filter(Boolean).join(' ')
          return (
            <g key={node.id} onClick={() => onSelectNode(node.id)} className="node-group">
              {node.type === 'card' && node.clusterIndex >= 0 && (
                <ellipse cx={node.x} cy={node.y} rx={node.r + 8} ry={node.r + 8} fill={color} opacity={0.12} className="cluster-halo" />
              )}
              <circle
                cx={node.x}
                cy={node.y}
                r={node.r}
                className={classes}
                fill={node.type === 'card' ? color : 'var(--surface)'}
                stroke={node.type === 'domain' ? 'var(--accent-500)' : 'var(--border)'}
                strokeWidth={2}
              />
              <text
                x={node.x}
                y={node.y + node.r + 14}
                textAnchor="middle"
                className="node-label"
              >
                {node.label}
              </text>
            </g>
          )
        })}
      </svg>
    </div>
  )
}
