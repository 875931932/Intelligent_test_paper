/** 课程列表页：教师控制台入口，新建课程 / 进入工作台。 */

import { useCallback, useEffect, useState } from 'react'
import { Button, Card, EmptyState, Field, Notice } from './ui'
import { coursesApi } from './client'
import type { Course } from './types'
import { ArrowUpRight, BookOpen, Plus } from 'lucide-react'

export function CoursesPage({ onOpen }: { onOpen: (course: Course) => void }) {
  const [courses, setCourses] = useState<Course[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [creating, setCreating] = useState(false)
  const [showForm, setShowForm] = useState(false)
  const [form, setForm] = useState({ name: '', slug: '', description: '' })

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      setCourses(await coursesApi.list())
    } catch (err) {
      setError(err instanceof Error ? err.message : '课程加载失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  async function create() {
    if (!form.name.trim() || !form.slug.trim()) {
      setError('课程名称与标识不能为空')
      return
    }
    setCreating(true)
    setError('')
    try {
      await coursesApi.create({
        name: form.name.trim(),
        slug: form.slug.trim(),
        description: form.description.trim() || undefined,
      })
      setForm({ name: '', slug: '', description: '' })
      setShowForm(false)
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : '课程创建失败')
    } finally {
      setCreating(false)
    }
  }

  return (
    <div className="content-inner">
      <div className="page-head">
        <div>
          <h2>课程</h2>
          <p className="desc">按课程空间管理资料、考纲框架、知识目录与命题流程。</p>
        </div>
        <span className="spacer" />
        <Button variant="primary" onClick={() => setShowForm((v) => !v)}>
          <Plus size={16} aria-hidden="true" />
          {showForm ? '收起' : '新建课程'}
        </Button>
      </div>

      {error ? <Notice kind="error">{error}</Notice> : null}

      {showForm ? (
        <Card title="新建课程" sub="课程是资料与命题的安全边界，标识（slug）创建后不可重复。">
          <div className="form-grid" style={{ marginBottom: 14 }}>
            <Field label="课程名称">
              <input
                className="input"
                placeholder="如：大模型提示词工程"
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
              />
            </Field>
            <Field label="课程标识" hint="仅小写字母、数字与连字符">
              <input
                className="input"
                placeholder="如：prompt-engineering"
                value={form.slug}
                onChange={(e) => setForm({ ...form, slug: e.target.value })}
              />
            </Field>
            <Field label="课程描述">
              <input
                className="input"
                placeholder="可选"
                value={form.description}
                onChange={(e) => setForm({ ...form, description: e.target.value })}
              />
            </Field>
          </div>
          <div className="form-row">
            <Button variant="primary" loading={creating} onClick={() => void create()}>
              创建课程
            </Button>
          </div>
        </Card>
      ) : null}

      <section className="course-list-section">
        <div className="section-bar">
          <div>
            <h3>我的课程</h3>
            <span>{loading ? '正在同步课程空间' : `${courses.length} 门课程`}</span>
          </div>
          <span className="section-bar-note">课程资料会在各自空间内隔离</span>
        </div>
        {loading ? (
          <EmptyState>正在加载课程…</EmptyState>
        ) : courses.length === 0 ? (
          <EmptyState>还没有课程，先新建一门课程开始命题准备。</EmptyState>
        ) : (
          <div className="course-grid">
            {courses.map((course) => (
              <article className="course-card" key={course.id}>
                <div className="course-card-icon"><BookOpen size={19} strokeWidth={1.8} aria-hidden="true" /></div>
                <div className="course-card-body">
                  <div className="course-card-heading">
                    <h3>{course.name}</h3>
                    <span className="course-card-slug">{course.slug}</span>
                  </div>
                  <p>{course.description ?? '管理课程资料、命题框架与试卷项目。'}</p>
                </div>
                <Button variant="secondary" size="sm" onClick={() => onOpen(course)}>
                  进入工作台 <ArrowUpRight size={15} aria-hidden="true" />
                </Button>
              </article>
            ))}
          </div>
        )}
      </section>
    </div>
  )
}
