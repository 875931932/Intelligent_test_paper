import type { ReactNode } from 'react'
import { ArrowLeft, BookOpen, Boxes, ClipboardList, FileText, Network, Settings2 } from 'lucide-react'
import { COURSE_SECTIONS, SECTION_LABELS, type CourseSection, type Route } from '../nav'

const SECTION_ICONS = {
  home: BookOpen,
  materials: FileText,
  framework: ClipboardList,
  knowledge: Network,
  projects: Boxes,
} as const

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
      <aside className="sidebar" aria-label="课程导航">
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
            {(['home', ...COURSE_SECTIONS.map((s) => s.key)] as CourseSection[]).map((key) => {
              const Icon = SECTION_ICONS[key]
              return (
                <button
                  key={key}
                  className={`nav-item${section === key ? ' active' : ''}`}
                  onClick={() => onNavigateSection(key)}
                  aria-current={section === key ? 'page' : undefined}
                >
                  <Icon size={16} strokeWidth={1.8} aria-hidden="true" />
                  {SECTION_LABELS[key]}
                </button>
              )
            })}
          </>
        ) : (
          <>
            <div className="sidebar-section">导航</div>
            <button className="nav-item active">
              <BookOpen size={16} strokeWidth={1.8} aria-hidden="true" />
              课程列表
            </button>
          </>
        )}

        <div className="sidebar-foot">
          <button className="nav-item" onClick={inWorkspace ? onBackToCourses : onOpenDemo}>
            {inWorkspace ? <ArrowLeft size={16} strokeWidth={1.8} aria-hidden="true" /> : <Settings2 size={16} strokeWidth={1.8} aria-hidden="true" />}
            {inWorkspace ? '返回课程列表' : '旧版演示流程'}
          </button>
        </div>
      </aside>

      <div className="main">
        <header className="topbar">
          <div className="topbar-main">
            {inWorkspace && course ? (
              <>
                <button className="crumb-link" onClick={onBackToCourses}>课程</button>
                <span className="crumb-sep">/</span>
                <h1>{course.name}</h1>
                <span className="topbar-section">{SECTION_LABELS[section]}</span>
              </>
            ) : (
              <h1>课程列表</h1>
            )}
            <div className="topbar-actions">
              {inWorkspace && course ? <span className="course-slug mono">{course.slug}</span> : null}
            </div>
          </div>
          {inWorkspace && course ? (
            <nav className="mobile-section-nav" aria-label="课程导航">
              {(['home', ...COURSE_SECTIONS.map((s) => s.key)] as CourseSection[]).map((key) => (
                <button key={key} className={section === key ? 'active' : ''} onClick={() => onNavigateSection(key)}>
                  {SECTION_LABELS[key]}
                </button>
              ))}
            </nav>
          ) : null}
        </header>

        <main className="content" tabIndex={-1}>
          <div className="content-inner">
            {children}
          </div>
        </main>
      </div>
    </div>
  )
}
