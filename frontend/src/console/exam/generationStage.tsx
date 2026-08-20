import { useCallback, useEffect, useRef, useState } from 'react'
import { examPipelineApi } from '../client'
import type { ExamProjectDetail } from '../types'
import { Button, EmptyState, Notice } from '../ui'

/** Polling interval between getTaskRun calls. Exported so tests can override (mock) it. */
export const POLL_INTERVAL_MS = 1500
/** Max time to poll before giving up. Exported for tests. */
export const POLL_TIMEOUT_MS = 60_000

type PaperItem = {
  id?: string
  item_index?: number
  question_type?: string
  stem?: string
  options?: unknown
  answer?: unknown
  score?: number
  exam_point_id?: string | null
  card_id?: string | null
  quality?: Record<string, unknown> | null
  teacher_override?: Record<string, unknown> | null
  finalized_text?: string | null
  needs_review?: boolean
  needs_review_reason?: string | null
}

type TaskRunResult = { paper_version_id?: string }

interface Props {
  courseId: string
  project: ExamProjectDetail
  onProceed: (projectId: string) => void
  /** Optional poll interval override (for tests). Defaults to 1500ms. */
  pollIntervalMs?: number
  /** Optional timeout override (for tests). Defaults to 60_000ms. */
  pollTimeoutMs?: number
}

export function GenerationStage({
  courseId,
  project,
  onProceed,
  pollIntervalMs = POLL_INTERVAL_MS,
  pollTimeoutMs = POLL_TIMEOUT_MS,
}: Props) {
  const [taskRunId, setTaskRunId] = useState<string | null>(null)
  const [status, setStatus] = useState<'idle' | 'running' | 'succeeded' | 'failed'>('idle')
  const [progress, setProgress] = useState(0)
  const [errorMsg, setErrorMsg] = useState('')
  const [items, setItems] = useState<PaperItem[]>([])
  const [starting, setStarting] = useState(false)
  const pollTimer = useRef<number | null>(null)
  const timeoutTimer = useRef<number | null>(null)
  const pollCountRef = useRef(0)
  // Keep latest poll / timeout durations in refs so closures always read current value
  // without requiring useCallback dependency churn.
  const intervalRef = useRef<number>(pollIntervalMs)
  const timeoutRef = useRef<number>(pollTimeoutMs)
  useEffect(() => {
    intervalRef.current = pollIntervalMs
    timeoutRef.current = pollTimeoutMs
  }, [pollIntervalMs, pollTimeoutMs])

  const clearTimers = useCallback(() => {
    if (pollTimer.current) {
      window.clearTimeout(pollTimer.current)
      pollTimer.current = null
    }
    if (timeoutTimer.current) {
      window.clearTimeout(timeoutTimer.current)
      timeoutTimer.current = null
    }
  }, [])

  useEffect(() => () => clearTimers(), [clearTimers])

  const loadPaperVersion = useCallback(
    async (pvId: string) => {
      try {
        const result = (await examPipelineApi.getCurrentPaperVersion(courseId, project.id)) as {
          id?: string
          items?: PaperItem[]
        }
        if (Array.isArray(result?.items)) {
          setItems(result.items)
        } else {
          // Fallback using pvId scoped endpoint not defined; leave empty
          void pvId
        }
      } catch {
        /* swallow */
      }
    },
    [courseId, project.id],
  )

  const pollTask = useCallback(
    async (runId: string) => {
      try {
        const run = await examPipelineApi.getTaskRun(courseId, runId)
        if (typeof run.progress === 'number') setProgress(Math.max(0, Math.min(100, run.progress)))
        if (run.status === 'succeeded') {
          clearTimers()
          setStatus('succeeded')
          const result = (run.result as TaskRunResult | undefined) ?? {}
          if (result.paper_version_id) await loadPaperVersion(result.paper_version_id)
          return
        }
        if (run.status === 'failed') {
          clearTimers()
          setStatus('failed')
          setErrorMsg(run.error_message ?? '生成任务失败')
          return
        }
        // Continue polling
        pollCountRef.current += 1
        pollTimer.current = window.setTimeout(() => {
          void pollTask(runId)
        }, intervalRef.current)
      } catch (err) {
        clearTimers()
        setStatus('failed')
        setErrorMsg(err instanceof Error ? err.message : '轮询生成状态失败')
      }
    },
    [clearTimers, courseId, loadPaperVersion],
  )

  const handleStart = useCallback(async () => {
    setStarting(true)
    setErrorMsg('')
    setProgress(0)
    setItems([])
    pollCountRef.current = 0
    try {
      const { task_run_id } = await examPipelineApi.startGeneration(courseId, project.id)
      setTaskRunId(task_run_id)
      setStatus('running')
      timeoutTimer.current = window.setTimeout(() => {
        if (status !== 'succeeded') {
          clearTimers()
          setStatus('failed')
          setErrorMsg('生成超时（60s）')
        }
      }, timeoutRef.current)
      // Initial poll
      pollTimer.current = window.setTimeout(() => {
        void pollTask(task_run_id)
      }, intervalRef.current)
    } catch (err) {
      setStatus('failed')
      setErrorMsg(err instanceof Error ? err.message : '启动生成失败')
    } finally {
      setStarting(false)
    }
  }, [clearTimers, courseId, project.id, pollTask, status])

  const pending = items.filter((it) => it.needs_review).length

  return (
    <div className="stage-panel">
      <div className="stage-panel-head">
        <h3>生成阶段</h3>
        <span className="muted small">
          {items.length > 0 ? `${items.length} 题 · ${pending} 待审` : '尚未生成'}
        </span>
      </div>

      {errorMsg && status === 'failed' && (
        <Notice kind="error" role="alert" data-testid="gen-error">
          {errorMsg}
        </Notice>
      )}

      {status === 'idle' && <EmptyState>尚未生成，点击下方按钮开始出题。</EmptyState>}

      {status === 'running' && (
        <div className="gen-progress" style={{ marginTop: 16 }}>
          <div className="gen-bar" role="progressbar" aria-label="试卷生成进度" aria-valuemin={0} aria-valuemax={100} aria-valuenow={progress}>
            <div className="gen-bar-fill" style={{ width: `${progress}%` }} />
          </div>
          <span className="muted small" data-testid="progress-text">
            进度：{progress}%{taskRunId ? ` · 任务 ${taskRunId.slice(0, 8)}` : ''}
          </span>
        </div>
      )}

      {items.length > 0 && (
        <div className="question-list" data-testid="question-cards" style={{ marginTop: 20 }}>
          {items.map((q, i) => (
            <div className="q-preview" key={`${q.item_index ?? i}`} data-testid={`q-card-${q.item_index ?? i}`}>
              <div className="q-stem">
                <span>{q.item_index ?? i + 1}.</span> <span>{q.stem ?? '(题干未生成)'}</span>
              </div>
              <div className="q-meta">
                <span className="q-tag">{q.question_type ?? 'unknown'}</span>
                <span className="q-tag">{q.score ?? 0} 分</span>
                {q.needs_review && <span className="q-tag warn" data-testid={`needs-review-${q.item_index ?? i}`}>待审</span>}
              </div>
            </div>
          ))}
        </div>
      )}

      {status === 'succeeded' && (
        <Notice kind="success" data-testid="gen-success">
          生成成功，共 {items.length} 题。
        </Notice>
      )}

      <div className="stage-actions">
        <Button
          variant="primary"
          onClick={handleStart}
          loading={starting || status === 'running'}
          disabled={status === 'running'}
          data-testid="start-gen-btn"
        >
          {status === 'idle' ? '开始生成' : status === 'failed' ? '重新生成' : '生成中…'}
        </Button>
        {status === 'succeeded' && (
          <Button variant="primary" onClick={() => onProceed(project.id)} data-testid="proceed-review-btn">
            进入审核
          </Button>
        )}
      </div>
    </div>
  )
}
