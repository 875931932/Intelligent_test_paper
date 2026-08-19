import type { ReactNode } from 'react'
import { COURSE_SECTIONS, SECTION_LABELS, type CourseSection, type Route } from '../nav'

export function Layout({ route, onNavigateSection, onBackToCourses, onOpenDemo, children }: {
  route: Route
  onNavigateSection: (section: CourseSection) => void
  onBackToCourses: () => void
  onOpenDemo: () => void
  children: ReactNode
}) {
  const inWorkspace = route.page === 'course-space' || route.page === 'exam-project'
  const course = inWorkspace ? route.course : null
  const section: CourseSection = route.page === 'course-space' ? route.section : 'home'

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="sidebar-brand">
          <div className="brand-mark">卷</div>
          <div className="brand-copy">
            <b>砚卷工作台</b>
            <span>教师控制台</span>
          </div>
        </div>

        {inWorkspace && course ? (
          <>
            <div className="sidebar-course">
              <b>{course.name}</b>
              <span>{course.slug}</span>
            </div>
            <div className="sidebar-section">课程资产</div>
            <button
              className={`nav-item${section === 'home' ? ' active' : ''}`}
              onClick={() => onNavigateSection('home')}
            >
              <span className="dot" />
              {SECTION_LABELS.home}
            </button>
            {COURSE_SECTIONS.map((s) => (
              <button
                key={s.key}
                className={`nav-item${section === s.key ? ' active' : ''}`}
                onClick={() => onNavigateSection(s.key)}
              >
                <span className="dot" />
                {s.label}
              </button>
            ))}
          </>
        ) : (
          <>
            <div className="sidebar-section">导航</div>
            <button className="nav-item active">
              <span className="dot" />
              课程列表
            </button>
          </>
        )}

        <div className="sidebar-foot">
          <button className="nav-item" onClick={inWorkspace ? onBackToCourses : onOpenDemo}>
            <span className="dot" />
            {inWorkspace ? '返回课程列表' : '旧版演示流程'}
          </button>
        </div>
      </aside>

      <div className="main">
        <header className="topbar">
          {inWorkspace && course ? (
            <>
              <span className="crumb-link" onClick={onBackToCourses}>课程</span>
              <span className="crumb-sep">/</span>
              <h1>{course.name}</h1>
              <span className="muted small">{SECTION_LABELS[section]}</span>
            </>
          ) : (
            <h1>课程列表</h1>
          )}
          <div className="topbar-actions">
            {inWorkspace && course ? <span className="muted small mono">{course.slug}</span> : null}
          </div>
        </header>

        <main className="content">{children}</main>
      </div>
    </div>
  )
}
