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
      <div className="pipeline-track">
        {PIPELINE_STAGES.map((s, i) => {
          const isCompleted = completed.includes(s.stage)
          const isActive = current === s.stage
          const isFuture = s.index > currentIndex
          const isClickable = isCompleted || isActive
          return (
            <div
              className={`pipeline-step${isActive ? ' active' : ''}${isCompleted ? ' done' : ''}${isFuture ? ' future' : ''}`}
              key={s.stage}
              onClick={() => isClickable && onJump(s.stage)}
              role="button"
              tabIndex={isClickable ? 0 : -1}
            >
              <div className="pipeline-circle">{isCompleted ? '✓' : s.index}</div>
              <div className="pipeline-label">{s.label}</div>
              {i < PIPELINE_STAGES.length - 1 && <div className="pipeline-connector" />}
            </div>
          )
        })}
      </div>
    </div>
  )
}
