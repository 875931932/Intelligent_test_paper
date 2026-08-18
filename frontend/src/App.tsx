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

/** 合同题位：考哪个原子、答案域、禁用上下文（对应后端 ContractSlot）。 */
type ContractSlot = {
  item_index: number
  question_type: string
  score: number
  difficulty: string
  cognitive_level: string
  assessment_mode: string
  exam_point_id: string
  anchor_key: string
  unit_id: string
  card_id: string
  coverage_atom: string
  answer_boundary: string
  performance_statement: string
  prompt_material: string[]
  scope_boundary: Record<string, unknown>
  preferred_terms: string[]
  forbidden_context: { atoms: string[]; answer_cores: string[] }
  comprehensive_archetype?: string | null
  material_form?: string | null
  cognitive_sequence: string[]
  subquestion_count_range?: number[] | null
  subquestion_actions: string[]
  answer_boundaries: string[]
}
type ContractConflict = {
  code: string
  exam_point_id: string
  message: string
  detail: Record<string, unknown>
}
type ExamPointProportion = {
  exam_point_id: string
  weight: number
  question_count: number
  proportion: number
}
type ContractAuditSummary = {
  exam_points: ExamPointProportion[]
  type_counts: Record<string, number>
  difficulty_counts: Record<string, number>
}
type PaperContract = {
  total_score: number
  slots: ContractSlot[]
  conflicts: ContractConflict[]
  audit_summary: ContractAuditSummary
}
type FinalCheck = {
  passed?: boolean
  checks?: Array<{ code: string; passed: boolean; detail?: Record<string, unknown> }>
}
type GenerationRunResult = {
  status: string
  questions: Question[]
  final_check: FinalCheck
  model_call_count: number
  model: string
}
type Subquestion =
  | string
  | {
      id?: string
      question?: string
      action?: string
      prompt?: string
      answer_boundary?: string
      answer?: string
      rubric?: string[]
      score?: number
    }

type Question = {
  item_index?: number
  question_type: string
  score: number
  stem?: string
  options?: string[] | Record<string, string>
  answer?: unknown
  explanation?: string
  rubric?: Array<{ point?: string; score?: number }>
  subquestions?: Subquestion[]
  comprehensive_archetype?: string | null
  material_form?: string | null
  subquestion_actions?: string[]
  quality?: { status?: string; code?: string; message?: string; issues?: string[] }
  needs_review?: boolean
  exam_point_id?: string
  unit_id?: string
  card_id?: string
  coverage_atom?: string
  answer_boundary?: string
}

const ARCHETYPE_LABELS: Record<string, { title: string; tag: string }> = {
  fault_diagnosis: { title: '故障诊断', tag: '症状→原因→修复' },
  code_completion_scenario: { title: '代码补全', tag: '工程场景·代码填空' },
  comparative_decision: { title: '对比决策', tag: '多方案权衡选择' },
  experiment_analysis: { title: '实验分析', tag: '现象·数据·结论' },
  scenario_design: { title: '方案设计', tag: '从需求到设计' },
}

function formatMaterialStem(stem: string) {
  if (!stem) return null
  // 识别代码块 ```…```
  if (/```/.test(stem)) {
    const parts: Array<{ kind: 'text' | 'code'; content: string; lang?: string }> = []
    let rest = stem
    const re = /```([\w+-]*)\n([\s\S]*?)```/g
    let last = 0
    let m: RegExpExecArray | null
    while ((m = re.exec(rest))) {
      if (m.index > last) parts.push({ kind: 'text', content: rest.slice(last, m.index) })
      parts.push({ kind: 'code', content: m[2], lang: m[1] || 'text' })
      last = m.index + m[0].length
    }
    if (last < rest.length) parts.push({ kind: 'text', content: rest.slice(last) })
    return parts.map((p, i) =>
      p.kind === 'code' ? (
        <pre key={i} className="material-code"><code className={p.lang}>{p.content}</code></pre>
      ) : (
        <div key={i} className="material-prose">{formatTableOrBullets(p.content)}</div>
      ),
    )
  }
  return <div className="material-prose">{formatTableOrBullets(stem)}</div>
}

function formatTableOrBullets(text: string) {
  // 先尝试 Markdown 表格：|---|
  const tableMatch = text.match(/(\|[^\n]+\|\n\|[-\s:]+\|[^\n]*\n(?:\|[^\n]+\|\n?)+)/)
  if (tableMatch) {
    const prefix = text.slice(0, tableMatch.index!)
    const raw = tableMatch[0]
    const suffix = text.slice((tableMatch.index || 0) + tableMatch[0].length)
    const rows = raw.split(/\n/).map((r) => r.trim()).filter(Boolean)
    const header = rows[0].split('|').map((c) => c.trim()).filter((c) => c.length > 0)
    const body = rows.slice(2).map((r) => r.split('|').map((c) => c.trim()).filter((c) => c.length > 0))
    return (
      <>
        <MaterialText text={prefix} />
        <table className="material-table">
          <thead>
            <tr>{header.map((h, i) => <th key={i}>{h}</th>)}</tr>
          </thead>
          <tbody>
            {body.map((row, r) => <tr key={r}>{row.map((cell, c) => <td key={c}>{cell}</td>)}</tr>)}
          </tbody>
        </table>
        <MaterialText text={suffix} />
      </>
    )
  }
  return <MaterialText text={text} />
}

function MaterialText({ text }: { text: string }) {
  if (!text) return null
  const lines = text.split(/\n/)
  // 检测 (1)(2)(3) 或 1. 2. 3. 等编号列表
  const enumRe = /^\s*\(?(\d+)\)|^\s*[-*·]|^\s*\d+[.、\)]/
  if (lines.some((l) => enumRe.test(l))) {
    return (
      <ul className="material-enum">
        {lines.map((line, i) => {
          const cleaned = line.replace(/^\s*\(?\d+\)|^\s*[-*·]|^\s*\d+[.、\)]/, '').trim()
          if (!cleaned) return null
          const m = line.match(/^\s*\(?(\d+)\)|^\s*(\d+)[.、\)]/)
          const num = m ? (m[1] || m[2]) : null
          return <li key={i}>{num ? <b className="enum-no">{num}.</b> : null}{cleaned}</li>
        })}
      </ul>
    )
  }
  return <>{lines.filter(Boolean).map((line, i) => <p key={i}>{line}</p>)}</>
}

function SubquestionItem({ index, item }: { index: number; item: Subquestion }) {
  const obj: Record<string, string | number | string[] | undefined> =
    typeof item === 'string' ? { prompt: item } : (item as Record<string, string | number | string[] | undefined>)
  const rubricArr = Array.isArray(obj.rubric) ? obj.rubric : typeof obj.rubric === 'string' ? [obj.rubric] : []
  const score = typeof obj.score === 'number' ? obj.score : null
  const action = typeof obj.action === 'string' ? obj.action : null
  const boundary = typeof obj.answer_boundary === 'string' ? obj.answer_boundary : null
  const prompt = typeof obj.prompt === 'string' ? obj.prompt : typeof obj.question === 'string' ? obj.question : String(item)
  const answer = typeof obj.answer === 'string' ? obj.answer : null
  const actionLabel: Record<string, string> = {
    fill_blank: '填空',
    short_answer: '简答',
    analysis: '分析',
    judgement: '判断',
    calculation: '计算',
  }
  return (
    <li className="sub-item">
      <div className="sub-item-head">
        <span className="sub-index">（{toChinese(index + 1)}）</span>
        {action && <span className="sub-action">{actionLabel[action] || action}</span>}
        {score != null && <span className="sub-score">{score} 分</span>}
      </div>
      <div className="sub-item-prompt">{formatTableOrBullets(prompt)}</div>
      {answer && (
        <details className="sub-answer">
          <summary>本问答案与评分点</summary>
          <div>
            {answer.split(/\n/).filter(Boolean).map((line, i) => <p key={i}>{line}</p>)}
            {boundary && <p className="muted"><strong>答题域：</strong>{boundary}</p>}
            {rubricArr.length > 0 && (
              <ol>
                {rubricArr.map((r, i) => {
                  const text = typeof r === 'string' ? r : (r as { point?: string }).point ?? JSON.stringify(r)
                  const sc = typeof r === 'object' && r != null ? (r as { score?: number }).score : null
                  return <li key={i}>{text}{sc != null ? <span className="rubric-score">（{sc} 分）</span> : ''}</li>
                })}
              </ol>
            )}
          </div>
        </details>
      )}
    </li>
  )
}

function formatAnswerBlock(answer: unknown) {
  if (answer == null) return <span>—</span>
  if (typeof answer === 'boolean') return <span>{answer ? '正确' : '错误'}</span>
  const s = String(answer)
  if (s.includes('\n')) {
    return (
      <div className="answer-block">
        {s.split(/\n/).filter(Boolean).map((line, i) => {
          const m = line.match(/^分问\((\d+)\)(.+?[:：])?(.*)$/) || line.match(/^(\d+)[.、)](.+?[:：])?(.*)$/)
          if (m) {
            const num = m[1]
            const head = m[2] || '：'
            const rest = m[3]
            return (
              <div key={i} className="answer-sub-item">
                <b>（{toChinese(Number(num))}）</b>
                {rest || head}
              </div>
            )
          }
          return <p key={i}>{line}</p>
        })}
      </div>
    )
  }
  return <span>{s}</span>
}
type WeightAuditRow = {
  exam_point_id: string
  syllabus_weight_percent: number
  planned_score: number
  actual_score: number
  question_count: number
}
type DemoPipeline = {
  status: string
  files_total?: number
  source_directory?: string
  model?: string
  extraction?: Array<{ filename: string; material_type: string; block_count: number; content_preview?: Array<{ page?: number; type?: string; text: string }> }>
  framework?: { teaching_topics?: Array<{ title: string; depth: string }>; anchors?: Array<{ title: string; exam_weight: number }>; final_exam_rules?: Record<string, unknown>; exam_points?: Array<{ code: string; title: string; weight_value: number }> }
  knowledge_tree?: { topics?: Array<{ name: string; framework_anchor_key: string; units?: Array<{ title: string; cards?: Array<{ name: string; assessable_content?: string[] }> }> }>; excluded_summary?: string[] }
  blueprint?: { plan?: Plan; allocation_basis?: string }
  paper?: { questions?: Question[]; total_score?: number; question_count?: number; weight_audit?: { rows?: WeightAuditRow[]; total_actual_score?: number } }
  error?: string
}

const QUESTION_LABELS: Record<string, string> = {
  single_choice: '单项选择题',
  true_false: '判断题',
  fill_blank: '填空题',
  short_answer: '简答题',
  comprehensive: '综合题',
}

const DIFFICULTY_LABELS: Record<string, string> = { low: '低', medium: '中', high: '高' }

const FINAL_CHECK_LABELS: Record<string, string> = {
  quota_match: '配额一致',
  atom_uniqueness: '原子唯一',
  answer_mutex: '答案域互斥',
  traceability: '溯源完整',
  needs_review: '复核清零',
}

const CONFLICT_LABELS: Record<string, string> = {
  atom_pool_insufficient: '原子池不足',
  cluster_exhausted: '原子簇耗尽',
  missing_exam_point: '未关联考点',
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
    { unit_id: 'unit-foundation', exam_point_id: 'EP-rag', anchor_key: 'foundation', card_ids: ['card-rag'] },
    { unit_id: 'unit-application', exam_point_id: 'EP-lora', anchor_key: 'application', card_ids: ['card-lora'] },
  ],
}

const initialCards = {
  'card-rag': {
    is_core: true,
    name: '检索增强生成基本原理',
    performance_statement: '必须掌握检索增强生成的完整流程，并分析检索结果质量对生成答案的影响。',
    assessable_content: [
      '文本切分的基本作用与常见粒度权衡',
      '向量化嵌入表示文本语义的原理',
      '相似度检索匹配查询与文档的方法',
      '上下文生成阶段对检索结果的依赖关系',
      '检索召回质量对最终答案可靠性的影响',
      '混合检索结合关键词与语义匹配的优势',
      '重排序模型精排候选文档的作用',
      '检索增强生成整体流程的先后顺序',
      '上下文窗口长度对注入文档数量的限制',
      '查询改写提升检索命中率的手段',
      '分块重叠设计对语义完整性的意义',
      '嵌入模型选型对检索效果的影响',
      '检索失败时的兜底生成策略',
      '评估检索增强系统质量的常用指标',
    ],
    preferred_terms: ['检索增强'],
    scope_boundary: { exclude: ['软件安装命令', '具体文件名和实验编号'] },
    cognitive_targets: ['understand', 'apply'],
    allowed_question_types: ['single_choice', 'true_false', 'short_answer'],
  },
  'card-lora': {
    is_core: true,
    name: '参数高效微调原理与应用',
    performance_statement: '必须掌握LoRA的核心思想，并根据任务约束分析参数高效微调方案。',
    assessable_content: [
      'LoRA低秩矩阵分解的核心思想',
      '秩的大小对可训练参数量的影响',
      '缩放系数调节增量权重的作用',
      '目标模块选择的基本原则',
      '参数高效微调减少显存占用的机制',
      'QLoRA量化与低秩适配的结合方式',
      '训练资源与模型效果之间的权衡',
      '冻结基座参数防止灾难性遗忘',
      '适配器权重合并回基座的部署优势',
      '全参微调与高效微调的成本对比',
      'NF4量化格式压缩权重的原理',
      '批大小与学习率对微调稳定性的影响',
      '过拟合在小数据微调中的表现',
      '训练数据质量对微调效果的决定作用',
      '领域语料继续预训练的适用场景',
      '指令微调对齐模型行为的流程',
      '验证集损失监控微调停止时机',
      '梯度检查点换取显存节约的代价',
      '多适配器切换服务不同任务的架构',
      '微调评估基准选择的方法',
    ],
    preferred_terms: ['LoRA'],
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
  const [contract, setContract] = useState<PaperContract | null>(null)
  const [confirmed, setConfirmed] = useState<PaperContract | null>(null)
  const [questions, setQuestions] = useState<Question[]>([])
  const [finalCheck, setFinalCheck] = useState<FinalCheck | null>(null)
  const [modelCallCount, setModelCallCount] = useState(0)
  const [model, setModel] = useState('')
  const [busy, setBusy] = useState<'course' | 'allocate' | 'confirm' | 'paper' | null>(null)
  const [notice, setNotice] = useState('')
  const [demo, setDemo] = useState<DemoPipeline | null>(null)

  const selectedCourse = courses.find((course) => course.id === courseId)
  const hasConflicts = (contract?.conflicts.length ?? 0) > 0
  const reviewCount = useMemo(() => questions.filter((q) => q.needs_review).length, [questions])
  const qualityPassed = useMemo(() => questions.filter((q) => q.quality?.status === 'pass' && !q.needs_review).length, [questions])

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
      setNotice('课程空间已建立，可以开始分配命题合同。')
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '创建失败')
    } finally {
      setBusy(null)
    }
  }

  async function allocateContract() {
    if (!courseId) return setNotice('请先选择或创建课程。')
    setBusy('allocate')
    setNotice('')
    setConfirmed(null)
    setQuestions([])
    setFinalCheck(null)
    try {
      const blueprint = JSON.parse(blueprintText)
      const knowledgeCards = JSON.parse(cardsText)
      if (!knowledgeCards || Object.keys(knowledgeCards).length === 0) {
        setNotice('知识目录为空：请先发布知识卡（knowledge_cards），再分配命题合同。')
        return
      }
      const data = await api<PaperContract>(`/api/v1/courses/${courseId}/blueprints/allocate`, {
        method: 'POST',
        body: JSON.stringify({ blueprint, knowledge_cards: knowledgeCards }),
      })
      setContract(data)
      if (data.conflicts.length > 0) {
        setNotice(`命题合同存在 ${data.conflicts.length} 处冲突，请先调整知识卡或蓝图再重新分配。`)
      } else {
        setNotice(`命题合同已分配：${data.slots.length} 个题位、总分 ${data.total_score} 分，等待人工确认。`)
      }
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '合同分配失败')
    } finally {
      setBusy(null)
    }
  }

  async function confirmContract() {
    if (!courseId || !contract) return setNotice('请先分配命题合同。')
    if (contract.conflicts.length > 0) return setNotice('合同存在冲突，处理冲突前无法确认。')
    setBusy('confirm')
    setNotice('')
    try {
      const knowledgeCards = JSON.parse(cardsText)
      const units = JSON.parse(blueprintText).units ?? []
      const data = await api<PaperContract>(`/api/v1/courses/${courseId}/blueprints/confirm`, {
        method: 'POST',
        body: JSON.stringify({ contract, slot_revisions: [], units, knowledge_cards: knowledgeCards }),
      })
      setContract(data)
      setConfirmed(data)
      setNotice('合同已确认（无修订）。合同锁定后可开始逐题命题。')
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '合同确认失败')
    } finally {
      setBusy(null)
    }
  }

  async function generatePaper() {
    if (!courseId || !confirmed) return setNotice('请先确认命题合同，再开始命题。')
    setBusy('paper')
    setNotice('模型正在按合同逐题命题并执行合同终检，请稍候……')
    try {
      const knowledgeCards = JSON.parse(cardsText)
      const data = await api<GenerationRunResult>(`/api/v1/courses/${courseId}/generation-runs`, {
        method: 'POST',
        body: JSON.stringify({ contract: confirmed.slots, knowledge_cards: knowledgeCards }),
      })
      setQuestions(data.questions)
      setFinalCheck(data.final_check ?? null)
      setModelCallCount(data.model_call_count ?? 0)
      setModel(data.model ?? '')
      setNotice(`候选试卷生成完成（${data.status}），使用模型 ${data.model}，共 ${data.model_call_count} 次模型调用。`)
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
          <h2>先算死题位，再锁定合同，<br />最后让模型逐题落笔。</h2>
        </div>
        <p className="lede">当前为合同链路验证版。分配阶段构造性保证不重复、不抄袭、比例对；合同经人工确认锁定后，才进入逐题生成与合同终检。</p>
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
          <PanelHead number="02" title="分配命题合同" icon={<ScrollText size={20} />} />
          <p className="panel-copy">每种题型分别设置低/中/高难度占比，三者合计 100%。系统按考点原子池分配题位：原子唯一、答案域互斥、考点比例对齐，冲突会显式报告。</p>
          <label>蓝图参数</label>
          <textarea className="code-editor blueprint-editor" value={blueprintText} onChange={(event) => setBlueprintText(event.target.value)} spellCheck={false} />
          <button onClick={allocateContract} disabled={busy !== null || !courseId}>
            {busy === 'allocate' ? <LoaderCircle className="spin" size={17} /> : <Sparkles size={17} />} 分配命题合同
          </button>
          {contract && (
            <div className={hasConflicts ? 'plan-summary contract-warn' : 'plan-summary'}>
              <div><b>{contract.total_score}</b><span>总分</span></div>
              <div><b>{contract.slots.length}</b><span>题位</span></div>
              <div><b>{hasConflicts ? contract.conflicts.length : contract.audit_summary.exam_points.length}</b><span>{hasConflicts ? '冲突' : '考点'}</span></div>
              {hasConflicts ? <CircleAlert size={23} /> : <CheckCircle2 size={23} />}
            </div>
          )}
        </article>

        <article className="panel generate-panel">
          <PanelHead number="03" title="确认合同并命题" icon={<Sparkles size={20} />} />
          <p className="panel-copy">这里只放入纯净知识卡，不传递文件名、页码、章节编号和证据 ID。合同确认锁定后，模型按合同逐题落笔并自动终检。</p>
          <label>纯净知识卡</label>
          <textarea className="code-editor card-editor" value={cardsText} onChange={(event) => setCardsText(event.target.value)} spellCheck={false} />
          <button className="secondary" onClick={confirmContract} disabled={busy !== null || !contract || hasConflicts}>
            {busy === 'confirm' ? <LoaderCircle className="spin" size={17} /> : <CheckCircle2 size={17} />} 确认合同（无修订）
          </button>
          <button className="primary-dark" onClick={generatePaper} disabled={busy !== null || !confirmed || hasConflicts}>
            {busy === 'paper' ? <LoaderCircle className="spin" size={17} /> : <ChevronRight size={17} />} 按合同逐题命题
          </button>
          <p className="fineprint">逐题生成 · 合同终检 · 最多局部重试 2 次</p>
        </article>
      </section>

      {demo?.blueprint?.plan
        ? <PlanTable plan={demo.blueprint.plan} />
        : contract && <ContractSection contract={contract} confirmedContract={confirmed} onConfirm={confirmContract} busy={busy} />}
      {demo?.paper?.questions?.length
        ? <Paper questions={demo.paper.questions} qualityPassed={demo.paper.questions.filter((q) => q.quality?.status === 'pass').length} />
        : questions.length > 0 && <Paper questions={questions} qualityPassed={qualityPassed} finalCheck={finalCheck} modelCallCount={modelCallCount} model={model} reviewCount={reviewCount} />}

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
    {demo.paper?.weight_audit?.rows?.length ? <WeightAuditTable rows={demo.paper.weight_audit.rows} pointTitles={pointTitles(demo)} /> : null}
    {demo.knowledge_tree?.topics && demo.knowledge_tree.topics.length > 0 && <div className="tree-strip"><strong>归并后的纯净知识卡</strong>{demo.knowledge_tree.topics.slice(0, 12).map((topic) => <div key={topic.name}><span>{topic.name}</span><small>{topic.units?.flatMap((unit) => unit.cards ?? []).slice(0, 3).map((card) => card.name).join(' · ')}</small></div>)}</div>}
  </section>
}

function EvidenceColumn({ title, items }: { title: string; items: Array<{ filename: string; block_count: number; content_preview?: Array<{ page?: number; text: string }> }> }) {
  return <div className="evidence-column"><strong>{title}</strong>{items.map((item) => <details key={item.filename}><summary>{item.filename} <small>{item.block_count} blocks</small></summary><div>{(item.content_preview ?? []).slice(0, 4).map((block, index) => <p key={index}><i>p.{(block.page ?? 0) + 1}</i>{block.text}</p>)}</div></details>)}</div>
}

function pointTitles(demo: DemoPipeline): Record<string, string> {
  const titles: Record<string, string> = {}
  for (const point of demo.framework?.exam_points ?? []) {
    titles[point.code] = point.title.replace(/^第\d+章\s*/, '')
  }
  return titles
}

function WeightAuditTable({ rows, pointTitles }: { rows: WeightAuditRow[]; pointTitles: Record<string, string> }) {
  const matched = rows.every((row) => Math.abs(row.actual_score - row.syllabus_weight_percent) < 0.01)
  return <div className="weight-audit">
    <div className="weight-audit-head">
      <strong>考纲占比审计</strong>
      <span className={matched ? 'audit-ok' : 'audit-warn'}>{matched ? '全部考点分值与考纲占比一致' : '存在偏差，需教师复核'}</span>
    </div>
    <table>
      <thead><tr><th>考点</th><th>考纲占比</th><th>计划分值</th><th>实际分值</th><th>题数</th><th>状态</th></tr></thead>
      <tbody>{rows.map((row) => {
        const ok = Math.abs(row.actual_score - row.syllabus_weight_percent) < 0.01
        return <tr key={row.exam_point_id}>
          <td title={pointTitles[row.exam_point_id] ?? ''}>{row.exam_point_id}</td>
          <td>{row.syllabus_weight_percent}%</td>
          <td>{row.planned_score}</td>
          <td><b>{row.actual_score}</b></td>
          <td>{row.question_count}</td>
          <td className={ok ? 'audit-ok' : 'audit-warn'}>{ok ? '一致' : '偏差'}</td>
        </tr>
      })}</tbody>
    </table>
  </div>
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

function ContractSection({ contract, confirmedContract, onConfirm, busy }: {
  contract: PaperContract
  confirmedContract: PaperContract | null
  onConfirm: () => void
  busy: 'course' | 'allocate' | 'confirm' | 'paper' | null
}) {
  const hasConflicts = contract.conflicts.length > 0
  const summary = contract.audit_summary
  const confirmedNow = confirmedContract !== null
  return (
    <section className="result-section">
      <div className="result-head">
        <div><p className="section-index">人工确认点 A</p><h2>命题合同</h2></div>
        <p>{hasConflicts ? '合同存在冲突：请调整知识卡或蓝图参数后重新分配，冲突清零前无法确认与生成。' : '每个题位已锁定考点、知识原子与答案域。确认合同后题位锁定，方可逐题命题。'}</p>
      </div>

      {hasConflicts && (
        <div className="notice conflict-strip">
          <CircleAlert size={17} />
          <div>
            <strong>合同冲突 {contract.conflicts.length} 处 — 需教师处理</strong>
            {contract.conflicts.map((conflict, index) => (
              <p key={`${conflict.code}-${conflict.exam_point_id}-${index}`}>
                [{conflict.exam_point_id || '全卷'}] {conflict.message}（{CONFLICT_LABELS[conflict.code] ?? conflict.code}）
              </p>
            ))}
          </div>
        </div>
      )}

      <div className="difficulty-strip">
        {Object.entries(summary.type_counts).map(([type, count]) => <span key={type}><b>{QUESTION_LABELS[type] ?? type}</b>{count} 题</span>)}
        {Object.entries(summary.difficulty_counts).map(([level, count]) => <span key={level}><b>{DIFFICULTY_LABELS[level] ?? level}难度</b>{count} 题</span>)}
      </div>

      <div className="table-wrap audit-table">
        <table>
          <thead><tr><th>考点</th><th>章节权重</th><th>题数</th><th>题位占比</th></tr></thead>
          <tbody>{summary.exam_points.map((point) => (
            <tr key={point.exam_point_id}>
              <td>{point.exam_point_id}</td>
              <td>{point.weight}%</td>
              <td><b>{point.question_count}</b></td>
              <td>{Math.round(point.proportion * 100)}%</td>
            </tr>
          ))}</tbody>
        </table>
      </div>

      <div className="table-wrap">
        <table>
          <thead><tr><th>题号</th><th>题型</th><th>分值</th><th>考点</th><th>知识原子</th><th>答案域</th></tr></thead>
          <tbody>{contract.slots.map((slot) => (
            <tr key={slot.item_index}>
              <td>{String(slot.item_index).padStart(2, '0')}</td>
              <td>{QUESTION_LABELS[slot.question_type] ?? slot.question_type}</td>
              <td>{slot.score}</td>
              <td>{slot.exam_point_id}</td>
              <td title={slot.coverage_atom}>{slot.coverage_atom}</td>
              <td className="mono-cell">{slot.answer_boundary || '—'}</td>
            </tr>
          ))}</tbody>
        </table>
      </div>

      <button className="secondary confirm-row" onClick={onConfirm} disabled={busy !== null || hasConflicts || confirmedNow}>
        {busy === 'confirm' ? <LoaderCircle className="spin" size={17} /> : <CheckCircle2 size={17} />}
        {confirmedNow ? '合同已确认锁定' : '确认合同（无修订）'}
      </button>
    </section>
  )
}

function Paper({ questions, qualityPassed, finalCheck, modelCallCount, model, reviewCount }: {
  questions: Question[]
  qualityPassed: number
  finalCheck?: FinalCheck | null
  modelCallCount?: number
  model?: string
  reviewCount?: number
}) {
  let number = 0
  const groups = Object.entries(QUESTION_LABELS).map(([type, label]) => ({ type, label, questions: questions.filter((q) => q.question_type === type) })).filter((group) => group.questions.length)
  return (
    <section className="paper-shell">
      <div className="paper-toolbar">
        <div><p className="section-index">人工确认点 B</p><h2>候选试卷</h2></div>
        <div className="toolbar-badges">
          {(modelCallCount ?? 0) > 0 && <span className="quality-badge"><Sparkles size={15} /> {model || 'model'} · {modelCallCount} 次调用</span>}
          {finalCheck && <span className={`quality-badge ${finalCheck.passed ? '' : 'badge-fail'}`}><CheckCircle2 size={17} /> 合同终检{finalCheck.passed ? '通过' : '未通过'}</span>}
          <span className={`quality-badge ${(reviewCount ?? 0) > 0 ? 'badge-fail' : ''}`}><CheckCircle2 size={17} /> 质检通过 {qualityPassed}/{questions.length}{(reviewCount ?? 0) > 0 ? ` · ${reviewCount} 题待复核` : ''}</span>
        </div>
      </div>
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
      {finalCheck && <FinalCheckTable finalCheck={finalCheck} />}
    </section>
  )
}

function FinalCheckTable({ finalCheck }: { finalCheck: FinalCheck }) {
  const checks = finalCheck.checks ?? []
  return (
    <div className="final-check">
      <div className="final-check-head">
        <strong>合同终检（final_check）</strong>
        <span className={finalCheck.passed ? 'status-ok' : 'status-fail'}>{finalCheck.passed ? '全部通过' : '存在未通过项'}</span>
      </div>
      <div className="table-wrap">
        <table>
          <thead><tr><th>检查项</th><th>结果</th><th>明细</th></tr></thead>
          <tbody>{checks.map((check) => (
            <tr key={check.code}>
              <td>{FINAL_CHECK_LABELS[check.code] ?? check.code}</td>
              <td className={check.passed ? 'status-ok' : 'status-fail'}>{check.passed ? '通过' : '未通过'}</td>
              <td className="mono-cell">{JSON.stringify(check.detail ?? {})}</td>
            </tr>
          ))}</tbody>
        </table>
      </div>
    </div>
  )
}

function QuestionView({ number, question }: { number: number; question: Question }) {
  const options = Array.isArray(question.options) ? question.options : question.options ? Object.entries(question.options).map(([key, value]) => `${key}. ${value}`) : []
  const qualityText = [question.quality?.status ? `状态 ${question.quality.status}` : '', question.quality?.message ?? '', ...(question.quality?.issues ?? [])].filter(Boolean).join(' · ')
  const isComp = question.question_type === 'comprehensive'
  const archetype = isComp && question.comprehensive_archetype ? ARCHETYPE_LABELS[question.comprehensive_archetype] : null
  return (
    <div className={`question ${isComp ? 'question-comprehensive' : ''}`}>
      <div className="stem">
        <b>{number}.</b>
        <span className="stem-col">
          <span className="stem-main">
            {isComp ? (
              <div className="comp-head">
                {archetype && (
                  <span className="archetype-tag" title={archetype.tag}>
                    <i className="archetype-pip" />{archetype.title}
                    <em className="archetype-tag-hint">{archetype.tag}</em>
                  </span>
                )}
                {question.material_form && <span className="material-form-tag">材料：{materialFormLabel(question.material_form)}</span>}
              </div>
            ) : null}
            {isComp ? formatMaterialStem(question.stem || '') : (question.stem || '（模型未返回题干）')}
            {question.needs_review && <i className="review-chip">需人工复核</i>}
            {question.exam_point_id && <i className="exam-point-chip">{question.exam_point_id}</i>}
          </span>
        </span>
        <em>{question.score} 分</em>
      </div>
      {options.length > 0 && <div className="options">{options.map((option, index) => <span key={index}>{/^[A-D][.、]/.test(option) ? option : `${String.fromCharCode(65 + index)}. ${option}`}</span>)}</div>}
      {question.subquestions?.length ? (
        isComp ? (
          <ol className="subquestions subquestions-rich">
            {question.subquestions.map((item, index) => <SubquestionItem key={index} index={index} item={item} />)}
          </ol>
        ) : (
          <ol className="subquestions">{question.subquestions.map((item, index) => <li key={index}>{typeof item === 'string' ? item : item.question ?? JSON.stringify(item)}</li>)}</ol>
        )
      ) : null}
      <details>
        <summary>查看答案、解析与评分细则</summary>
        <div className="answer">
          <p><strong>参考答案：</strong>{formatAnswerBlock(question.answer)}</p>
          {question.explanation && <p><strong>解析：</strong>{question.explanation}</p>}
          {question.rubric && question.rubric.length > 0 && (
            <div className="rubric-section">
              <strong>评分细则：</strong>
              <ul>{question.rubric.map((row, index) => <li key={index}>{row.point ?? JSON.stringify(row)}{row.score != null ? <span className="rubric-score">（{row.score} 分）</span> : ''}</li>)}</ul>
            </div>
          )}
          {question.coverage_atom && <p><strong>合同原子：</strong>{question.coverage_atom}</p>}
          {qualityText && <p className={`quality ${question.quality?.status === 'pass' && !question.needs_review ? 'pass' : 'fail'}`}>质量检查：{qualityText}</p>}
        </div>
      </details>
    </div>
  )
}

function materialFormLabel(form: string) {
  const map: Record<string, string> = {
    code_skeleton: '代码骨架',
    symptom_list: '症状列表',
    constraint_table: '约束对比表',
    case_story: '案例描述',
    data_panel: '数据面板',
    scenario_text: '场景文字',
  }
  return map[form] || form
}

function toChinese(value: number) {
  return ['零', '一', '二', '三', '四', '五', '六'][value] ?? String(value)
}

export default App
