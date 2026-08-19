import { PIPELINE_STAGES, type PipelineStage } from '../types'

interface Props {
  current: PipelineStage
  completed: PipelineStage[]
  onJump: (stage: PipelineStage) => void
}

export function PipelineNav({ current, completed, onJump }: Props) {
  const currentIndex = PIPELINE_STAGES.find((s) => s.stage === current)?.index ?? 1

  return (
    <div className="pipeline-nav">
      <div className="pipeline-line">
        {PIPELINE_STAGES.map((s, i) => {
          const isCompleted = completed.includes(s.stage)
          const isActive = current === s.stage
          const isFuture = s.index > currentIndex
          const isClickable = isCompleted || isActive
          return (
            <div
              className={`stage-dot ${isActive ? 'active' : ''} ${isCompleted ? 'done' : ''} ${isFuture ? 'future' : ''}`}
              key={s.stage}
              onClick={() => isClickable && onJump(s.stage)}
              role="button"
            >
              <div className="stage-circle">{isCompleted ? '✓' : s.index}</div>
              <div className="stage-label">{s.label}</div>
              {i < PIPELINE_STAGES.length - 1 && <div className="stage-connector" />}
            </div>
          )
        })}
      </div>
    </div>
  )
}
