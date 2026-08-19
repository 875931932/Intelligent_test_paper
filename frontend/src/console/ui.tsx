/** 共享 UI 原语：按钮 / 卡片 / 徽章 / 表单 / 提示 / 加载，全部由 styles.css 令牌驱动。 */

import type { ButtonHTMLAttributes, ReactNode } from 'react'

export function Button({
  variant = 'secondary',
  size,
  loading,
  children,
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: 'primary' | 'secondary' | 'ghost' | 'danger' | 'danger-ghost'
  size?: 'sm'
  loading?: boolean
}) {
  return (
    <button className={`btn ${variant}${size ? ` ${size}` : ''}`} {...rest} disabled={rest.disabled || loading}>
      {loading ? <span className="spinner" style={{ width: 13, height: 13, borderWidth: 2 }} /> : null}
      {children}
    </button>
  )
}

export function Card({ title, sub, actions, children, tight }: {
  title?: ReactNode
  sub?: ReactNode
  actions?: ReactNode
  children: ReactNode
  tight?: boolean
}) {
  return (
    <section className="card">
      {title ? (
        <header className="card-head">
          <div>
            <h3>{title}</h3>
            {sub ? <div className="sub">{sub}</div> : null}
          </div>
          <span className="spacer" />
          {actions}
        </header>
      ) : null}
      <div className={`card-body${tight ? ' tight' : ''}`}>{children}</div>
    </section>
  )
}

export type PillKind = 'neutral' | 'info' | 'success' | 'danger' | 'warning'

export function Pill({ kind = 'neutral', dot, children }: { kind?: PillKind; dot?: boolean; children: ReactNode }) {
  return (
    <span className={`pill ${kind}`}>
      {dot ? <i className="dot" /> : null}
      {children}
    </span>
  )
}

export function Field({ label, hint, children }: { label: ReactNode; hint?: ReactNode; children: ReactNode }) {
  return (
    <label className="field">
      <span>{label}</span>
      {children}
      {hint ? <span className="hint">{hint}</span> : null}
    </label>
  )
}

export type NoticeKind = 'info' | 'success' | 'error' | 'warning'

type NoticeProps = React.HTMLAttributes<HTMLDivElement> & {
  kind: NoticeKind
  children: React.ReactNode
}

export function Notice({ kind, children, className, ...rest }: NoticeProps) {
  if (!children) return null
  return (
    <div {...rest} className={`notice ${kind}${className ? ` ${className}` : ''}`}>
      {children}
    </div>
  )
}

export function LoadingLine({ children }: { children: ReactNode }) {
  return (
    <div className="loading-line">
      <span className="spinner" />
      {children}
    </div>
  )
}

export function EmptyState({ children }: { children: ReactNode }) {
  return <div className="table-empty">{children}</div>
}

/** 步骤导航条：四步流程的当前定位。 */
export function StepNav({ steps, current, done, onSelect }: {
  steps: { key: string; label: string }[]
  current: string
  done: Record<string, boolean>
  onSelect: (key: string) => void
}) {
  return (
    <nav className="step-nav" aria-label="命题流程">
      {steps.map((step, index) => (
        <button
          key={step.key}
          className={`step-tab${step.key === current ? ' active' : ''}${done[step.key] ? ' done' : ''}`}
          onClick={() => onSelect(step.key)}
        >
          <span className="step-index">{done[step.key] ? '✓' : index + 1}</span>
          {step.label}
        </button>
      ))}
    </nav>
  )
}
