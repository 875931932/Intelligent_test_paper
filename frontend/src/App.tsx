import { useEffect, useMemo, useState } from 'react'
import {
  BookOpen,
  CheckCircle2,
  ChevronRight,
  CircleAlert,
  FileCheck2,
  LoaderCircle,
  Plus,
  ScrollText,
  Sparkles,
} from 'lucide-react'
import { api } from './api'

type Course = { id: string; name: string; slug: string; description?: string | null }
type Health = Record<string, string>
type PlanItem = {
  item_index: number
  question_type: string
  score: number
  anchor_key: string
  unit_id: string
  card_id: string
  difficulty: string
  cognitive_level: string
}
type Plan = { total_score: number; items: PlanItem[]; type_counts: Record<string, number>; difficulty_counts: Record<string, Record<string, number>>; anchor_counts: Record<string, number> }
type Question = {
  question_type: string
  score: number
  stem?: string
  options?: string[] | Record<string, string>
  answer?: unknown
  explanation?: string
  rubric?: Array<{ point?: string; score?: number }>
  subquestions?: Array<string | { id?: string; question?: string }>
  quality?: { status?: string; issues?: string[] }
}
type DemoPipeline = {
  status: string
  files_total?: number
  source_directory?: string
  model?: string
  extraction?: Array<{ filename: string; material_type: string; block_count: number; content_preview?: Array<{ page?: number; type?: string; text: string }> }>
  framework?: { teaching_topics?: Array<{ title: string; depth: string }>; anchors?: Array<{ title: string; exam_weight: number }>; final_exam_rules?: Record<string, unknown> }
  knowledge_tree?: { topics?: Array<{ name: string; framework_anchor_key: string; units?: Array<{ title: string; cards?: Array<{ name: string; assessable_content?: string[] }> }> }>; excluded_summary?: string[] }
  blueprint?: { plan?: Plan; allocation_basis?: string }
  paper?: { questions?: Question[]; total_score?: number; question_count?: number }
  error?: string
}

const QUESTION_LABELS: Record<string, string> = {
  single_choice: '单项选择题',
  true_false: '判断题',
  fill_blank: '填空题',
  short_answer: '简答题',
  comprehensive: '综合题',
}

const initialBlueprint = {
  total_score: 100,
  type_rules: {
    single_choice: { count: 15, score: 2, difficulty_distribution: { low: 40, medium: 40, high: 20 } },
    true_false: { count: 10, score: 2, difficulty_distribution: { low: 50, medium: 30, high: 20 } },
    short_answer: { count: 5, score: 10, difficulty_distribution: { low: 20, medium: 50, high: 30 } },
  },
  chapter_weights: { foundation: 40, application: 60 },
  units: [
    { unit_id: 'unit-foundation', anchor_key: 'foundation', card_ids: ['card-rag'] },
    { unit_id: 'unit-application', anchor_key: 'application', card_ids: ['card-lora'] },
  ],
}

const initialCards = {
  'card-rag': {
    name: '检索增强生成基本原理',
    performance_statement: '能够解释检索增强生成的完整流程，并分析检索结果质量对生成答案的影响。',
    assessable_content: ['文本切分、向量化、相似度检索与上下文生成的作用及先后关系', '召回质量、上下文相关性与最终答案可靠性的关系'],
    scope_boundary: { exclude: ['软件安装命令', '具体文件名和实验编号'] },
    cognitive_targets: ['understand', 'apply'],
    allowed_question_types: ['single_choice', 'true_false', 'short_answer'],
  },
  'card-lora': {
    name: '参数高效微调原理与应用',
    performance_statement: '能够说明LoRA的核心思想，并根据任务约束分析参数高效微调方案。',
    assessable_content: ['低秩矩阵分解对可训练参数量和显存占用的影响', '秩、缩放系数与目标模块选择的基本原则', '训练资源与模型效果之间的权衡'],
    scope_boundary: { exclude: ['固定版本号', '安装命令', '照抄某次实验参数'] },
    cognitive_targets: ['understand', 'apply', 'analyze'],
    allowed_question_types: ['single_choice', 'true_false', 'short_answer'],
  },
}

function App() {
  const [health, setHealth] = useState<Health | null>(null)
  const [courses, setCourses] = useState<Course[]>([])
  const [courseId, setCourseId] = useState('')
  const [courseName, setCourseName] = useState('大模型调优与部署技术')
  const [courseSlug, setCourseSlug] = useState(`llm-exam-${Date.now().toString().slice(-6)}`)
  const [blueprintText, setBlueprintText] = useState(JSON.stringify(initialBlueprint, null, 2))
  const [cardsText, setCardsText] = useState(JSON.stringify(initialCards, null, 2))
  const [plan, setPlan] = useState<Plan | null>(null)
  const [questions, setQuestions] = useState<Question[]>([])
  const [busy, setBusy] = useState<'course' | 'plan' | 'paper' | null>(null)
  const [notice, setNotice] = useState('')
  const [demo, setDemo] = useState<DemoPipeline | null>(null)

  const selectedCourse = courses.find((course) => course.id === courseId)
  const totalItems = plan?.items.length ?? 0
  const qualityPassed = useMemo(() => questions.filter((q) => q.quality?.status === 'pass').length, [questions])

  useEffect(() => {
    Promise.all([api<Health>('/api/v1/health'), api<Course[]>('/api/v1/courses')])
      .then(([healthData, courseData]) => {
        setHealth(healthData)
        setCourses(courseData)
        if (courseData[0]) setCourseId(courseData[0].id)
      })
      .catch((error) => setNotice(error.message))
  }, [])

  useEffect(() => {
    let active = true
    const refresh = () => fetch(`/demo/pipeline.json?ts=${Date.now()}`).then((response) => response.ok ? response.json() : null).then((data) => { if (active && data) setDemo(data) }).catch(() => undefined)
    refresh()
    const timer = window.setInterval(refresh, 5000)
    return () => { active = false; window.clearInterval(timer) }
  }, [])

  async function createCourse() {
    setBusy('course')
    setNotice('')
    try {
      const course = await api<Course>('/api/v1/courses', {
        method: 'POST',
        body: JSON.stringify({ name: courseName, slug: courseSlug, description: '用于验证AI命题质量的课程空间' }),
      })
      setCourses((current) => [course, ...current])
      setCourseId(course.id)
      setNotice('课程空间已建立，可以开始分配蓝图。')
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '创建失败')
    } finally {
      setBusy(null)
    }
  }

  async function allocateBlueprint() {
    if (!courseId) return setNotice('请先选择或创建课程。')
    setBusy('plan')
    setNotice('')
    setQuestions([])
    try {
      const blueprint = JSON.parse(blueprintText)
      const data = await api<Plan>(`/api/v1/courses/${courseId}/blueprints/allocate`, {
        method: 'POST',
        body: JSON.stringify(blueprint),
      })
      setPlan(data)
      setNotice(`蓝图已构建，共 ${data.items.length} 道题，等待人工确认。`)
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '蓝图构建失败')
    } finally {
      setBusy(null)
    }
  }

  async function generatePaper() {
    if (!courseId || !plan) return setNotice('请先构建并确认蓝图。')
    setBusy('paper')
    setNotice('模型正在逐题命题并进行质量检查，请稍候……')
    try {
      const knowledgeCards = JSON.parse(cardsText)
      const data = await api<{ status: string; questions: Question[]; model: string }>(`/api/v1/courses/${courseId}/generation-runs`, {
        method: 'POST',
        body: JSON.stringify({ plan_items: plan.items, knowledge_cards: knowledgeCards }),
      })
      setQuestions(data.questions)
      setNotice(`候选试卷生成完成，使用模型 ${data.model}。`)
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '试卷生成失败')
    } finally {
      setBusy(null)
    }
  }

  return (
    <main>
      <header className="masthead">
        <div className="brand-mark">砚</div>
        <div>
          <p className="eyebrow">高校期末考试 · 智能命题系统</p>
          <h1>砚卷命题工作台</h1>
        </div>
        <div className="health-strip">
          {health ? Object.entries(health).map(([key, value]) => (
            <span key={key} className={value === 'ok' || value === 'configured' ? 'healthy' : 'muted'}>
              <i />{key} {value}
            </span>
          )) : <span className="muted"><LoaderCircle className="spin" size={14} /> 检查服务</span>}
        </div>
      </header>

      <section className="intro-grid">
        <div>
          <p className="section-index">命题流程 / 01—03</p>
          <h2>先定范围，再定结构，<br />最后让模型逐题落笔。</h2>
        </div>
        <p className="lede">当前为核心链路验证版。它将知识卡与来源信息隔离，只向模型提供可考核内容；蓝图经人工确认后，才进入逐题生成与质量检查。</p>
      </section>

      {notice && <div className="notice"><CircleAlert size={17} /><span>{notice}</span></div>}

      {demo && <DemoProgress demo={demo} />}

      <section className="workflow">
        <article className="panel course-panel">
          <PanelHead number="01" title="选择课程空间" icon={<BookOpen size={20} />} />
          <label>已有课程</label>
          <select value={courseId} onChange={(event) => setCourseId(event.target.value)}>
            <option value="">请选择课程</option>
            {courses.map((course) => <option value={course.id} key={course.id}>{course.name}</option>)}
          </select>
          <div className="rule"><span>或新建一个</span></div>
          <label>课程名称</label>
          <input value={courseName} onChange={(event) => setCourseName(event.target.value)} />
          <label>课程标识</label>
          <input value={courseSlug} onChange={(event) => setCourseSlug(event.target.value.toLowerCase().replace(/[^a-z0-9-]/g, '-'))} />
          <button className="secondary" onClick={createCourse} disabled={busy !== null}>
            {busy === 'course' ? <LoaderCircle className="spin" size={17} /> : <Plus size={17} />} 建立课程空间
          </button>
          {selectedCourse && <div className="selection-card"><FileCheck2 size={18} /><div><strong>{selectedCourse.name}</strong><small>{selectedCourse.slug}</small></div></div>}
        </article>

        <article className="panel blueprint-panel">
          <PanelHead number="02" title="构建并确认蓝图" icon={<ScrollText size={20} />} />
          <p className="panel-copy">每种题型分别设置低/中/高难度占比，三者合计 100%。同一题型会按低 → 中 → 高排序生成；难度不再使用全卷统一值。</p>
          <label>蓝图参数</label>
          <textarea className="code-editor blueprint-editor" value={blueprintText} onChange={(event) => setBlueprintText(event.target.value)} spellCheck={false} />
          <button onClick={allocateBlueprint} disabled={busy !== null || !courseId}>
            {busy === 'plan' ? <LoaderCircle className="spin" size={17} /> : <Sparkles size={17} />} 构建命题蓝图
          </button>
          {plan && (
            <div className="plan-summary">
              <div><b>{plan.total_score}</b><span>总分</span></div>
              <div><b>{totalItems}</b><span>题目</span></div>
              <div><b>{Object.keys(plan.anchor_counts).length}</b><span>考核单元</span></div>
              <CheckCircle2 size={23} />
            </div>
          )}
        </article>

        <article className="panel generate-panel">
          <PanelHead number="03" title="生成候选试卷" icon={<Sparkles size={20} />} />
          <p className="panel-copy">这里只放入纯净知识卡，不传递文件名、页码、章节编号和证据 ID。</p>
          <label>纯净知识卡</label>
          <textarea className="code-editor card-editor" value={cardsText} onChange={(event) => setCardsText(event.target.value)} spellCheck={false} />
          <button className="primary-dark" onClick={generatePaper} disabled={busy !== null || !plan}>
            {busy === 'paper' ? <LoaderCircle className="spin" size={17} /> : <ChevronRight size={17} />} 确认蓝图并开始命题
          </button>
          <p className="fineprint">逐题生成 · 自动质检 · 最多局部重试 2 次</p>
        </article>
      </section>

      {demo?.blueprint?.plan ? <PlanTable plan={demo.blueprint.plan} /> : plan && <PlanTable plan={plan} />}
      {demo?.paper?.questions?.length ? <Paper questions={demo.paper.questions} qualityPassed={demo.paper.questions.filter((q) => q.quality?.status === 'pass').length} /> : questions.length > 0 && <Paper questions={questions} qualityPassed={qualityPassed} />}

      <footer>砚卷 · CORE PREVIEW / 2026</footer>
    </main>
  )
}

function DemoProgress({ demo }: { demo: DemoPipeline }) {
  const statusLabel: Record<string, string> = { parsing: 'MinerU 解析资料', framework: '整理教学大纲与考核大纲', knowledge_organization: '逐文件提取知识', knowledge_consolidation: '归并知识树', blueprint: '构建命题蓝图', generating: '逐题生成与质检', complete: '候选试卷已完成', failed: '流水线失败' }
  const extraction = demo.extraction ?? []
  const teaching = extraction.filter((item) => item.material_type === 'teaching_syllabus')
  const assessment = extraction.filter((item) => item.material_type === 'assessment_syllabus')
  const materials = extraction.filter((item) => item.material_type === 'teaching_material')
  return <section className="demo-status">
    <div className="demo-status-head"><div><p className="section-index">真实素材流水线</p><h2>{statusLabel[demo.status] ?? demo.status}</h2></div><span className={demo.status === 'complete' ? 'demo-complete' : demo.status === 'failed' ? 'demo-failed' : 'demo-running'}>{demo.status}</span></div>
    {demo.error && <div className="demo-error"><CircleAlert size={16} />{demo.error}</div>}
    <div className="demo-metrics"><div><b>{demo.files_total ?? extraction.length}</b><span>素材文件</span></div><div><b>{extraction.reduce((sum, item) => sum + (item.block_count ?? 0), 0)}</b><span>MinerU 内容块</span></div><div><b>{demo.knowledge_tree?.topics?.length ?? 0}</b><span>知识主题</span></div><div><b>{demo.blueprint?.plan?.items.length ?? 0}</b><span>蓝图题目</span></div></div>
    {extraction.length > 0 && <div className="evidence-grid"><EvidenceColumn title="考核输入" items={[...teaching, ...assessment]} /><EvidenceColumn title="教学材料" items={materials.slice(0, 8)} /></div>}
    {demo.framework && <div className="framework-strip"><div><strong>考核大纲权重</strong>{(demo.framework.anchors ?? []).map((anchor) => <span key={anchor.title}>{anchor.title.replace(/^第\d+章\s*/, '')} <b>{anchor.exam_weight}%</b></span>)}</div><div><strong>教学主题</strong>{(demo.framework.teaching_topics ?? []).slice(0, 9).map((topic) => <span key={topic.title}>{topic.title}</span>)}</div></div>}
    {demo.knowledge_tree?.topics && demo.knowledge_tree.topics.length > 0 && <div className="tree-strip"><strong>归并后的纯净知识卡</strong>{demo.knowledge_tree.topics.slice(0, 12).map((topic) => <div key={topic.name}><span>{topic.name}</span><small>{topic.units?.flatMap((unit) => unit.cards ?? []).slice(0, 3).map((card) => card.name).join(' · ')}</small></div>)}</div>}
  </section>
}

function EvidenceColumn({ title, items }: { title: string; items: Array<{ filename: string; block_count: number; content_preview?: Array<{ page?: number; text: string }> }> }) {
  return <div className="evidence-column"><strong>{title}</strong>{items.map((item) => <details key={item.filename}><summary>{item.filename} <small>{item.block_count} blocks</small></summary><div>{(item.content_preview ?? []).slice(0, 4).map((block, index) => <p key={index}><i>p.{(block.page ?? 0) + 1}</i>{block.text}</p>)}</div></details>)}</div>
}

function PanelHead({ number, title, icon }: { number: string; title: string; icon: React.ReactNode }) {
  return <div className="panel-head"><span>{number}</span><h3>{title}</h3>{icon}</div>
}

function PlanTable({ plan }: { plan: Plan }) {
  return (
    <section className="result-section">
      <div className="result-head"><div><p className="section-index">人工确认点 A</p><h2>命题蓝图预览</h2></div><p>确认题型结构、章节分值和各题型难度配额后，再启动模型生成。</p></div>
      <div className="difficulty-strip">{Object.entries(plan.difficulty_counts ?? {}).map(([type, counts]) => <span key={type}><b>{QUESTION_LABELS[type] ?? type}</b> 低 {counts.low ?? 0} · 中 {counts.medium ?? 0} · 高 {counts.high ?? 0}</span>)}</div>
      <div className="table-wrap">
        <table>
          <thead><tr><th>序号</th><th>题型</th><th>分值</th><th>考核锚点</th><th>知识卡</th><th>难度</th></tr></thead>
          <tbody>{plan.items.map((item) => <tr key={item.item_index}><td>{String(item.item_index).padStart(2, '0')}</td><td>{QUESTION_LABELS[item.question_type] ?? item.question_type}</td><td>{item.score}</td><td>{item.anchor_key}</td><td>{item.card_id}</td><td>{item.difficulty}</td></tr>)}</tbody>
        </table>
      </div>
    </section>
  )
}

function Paper({ questions, qualityPassed }: { questions: Question[]; qualityPassed: number }) {
  let number = 0
  const groups = Object.entries(QUESTION_LABELS).map(([type, label]) => ({ type, label, questions: questions.filter((q) => q.question_type === type) })).filter((group) => group.questions.length)
  return (
    <section className="paper-shell">
      <div className="paper-toolbar"><div><p className="section-index">人工确认点 B</p><h2>候选试卷</h2></div><div className="quality-badge"><CheckCircle2 size={17} /> 质检通过 {qualityPassed}/{questions.length}</div></div>
      <div className="paper">
        <header className="paper-header"><p>2025—2026 学年第二学期期末考试</p><h2>课程试卷（候选稿）</h2><div><span>考试形式：闭卷</span><span>满分：100 分</span><span>命题方式：AI 辅助</span></div></header>
        {groups.map((group, groupIndex) => (
          <section className="question-group" key={group.type}>
            <h3>{toChinese(groupIndex + 1)}、{group.label} <small>（共 {group.questions.length} 题）</small></h3>
            {group.questions.map((question) => {
              number += 1
              return <QuestionView key={number} number={number} question={question} />
            })}
          </section>
        ))}
      </div>
    </section>
  )
}

function QuestionView({ number, question }: { number: number; question: Question }) {
  const options = Array.isArray(question.options) ? question.options : question.options ? Object.entries(question.options).map(([key, value]) => `${key}. ${value}`) : []
  return (
    <div className="question">
      <div className="stem"><b>{number}.</b><span>{question.stem || '（模型未返回题干）'}</span><em>{question.score} 分</em></div>
      {options.length > 0 && <div className="options">{options.map((option, index) => <span key={index}>{/^[A-D][.、]/.test(option) ? option : `${String.fromCharCode(65 + index)}. ${option}`}</span>)}</div>}
      {question.subquestions?.length ? <ol className="subquestions">{question.subquestions.map((item, index) => <li key={index}>{typeof item === 'string' ? item : item.question ?? JSON.stringify(item)}</li>)}</ol> : null}
      <details><summary>查看答案、解析与评分细则</summary><div className="answer"><p><strong>参考答案：</strong>{typeof question.answer === 'boolean' ? (question.answer ? '正确' : '错误') : String(question.answer ?? '—')}</p>{question.explanation && <p><strong>解析：</strong>{question.explanation}</p>}{question.rubric && <div><strong>评分细则：</strong><ul>{question.rubric.map((row, index) => <li key={index}>{row.point ?? JSON.stringify(row)}{row.score != null ? `（${row.score} 分）` : ''}</li>)}</ul></div>}<p className={`quality ${question.quality?.status === 'pass' ? 'pass' : 'fail'}`}>质量检查：{question.quality?.status ?? 'unknown'}{question.quality?.issues?.length ? ` · ${question.quality.issues.join('；')}` : ''}</p></div></details>
    </div>
  )
}

function toChinese(value: number) {
  return ['零', '一', '二', '三', '四', '五', '六'][value] ?? String(value)
}

export default App
