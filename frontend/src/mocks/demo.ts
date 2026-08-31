/**
 * 演示模式静态数据（后端未连接时使用）。
 *
 * 所有读取接口返回逼真的静态数据，供前端页面完整渲染预览。
 * 通过 config.enableMock 开关控制，后端恢复后关闭即可。
 */
import type {
  CourseResponse,
  MaterialResponse,
  CurrentFrameworkResponse,
  FrameworkCandidate,
  PublishedKnowledgeResponse,
  ExamProject,
  EvidenceChunk,
} from '@/types/api';

// ───────────────────────────────────────────────
// 课程
// ───────────────────────────────────────────────
export const demoCourses: CourseResponse[] = [
  {
    id: 'c1',
    owner_id: 'u1',
    name: '高等数学（上）',
    slug: 'advanced-math-1',
    description: '面向大一理工科的高等数学上册课程',
  },
  {
    id: 'c2',
    owner_id: 'u1',
    name: '大学物理',
    slug: 'college-physics',
    description: '大学物理基础课程，涵盖力学、电磁学',
  },
  {
    id: 'c3',
    owner_id: 'u1',
    name: '数据结构与算法',
    slug: 'data-structures',
    description: '计算机专业核心课程',
  },
];

// ───────────────────────────────────────────────
// 资料库
// ───────────────────────────────────────────────
export const demoMaterials: MaterialResponse[] = [
  {
    id: 'm1',
    course_id: 'c1',
    logical_name: '高等数学教学大纲.pdf',
    material_type: 'teaching_syllabus',
    status: 'ready',
    latest_version: { id: 'v1', material_id: 'm1', status: 'active', version_no: 1, sha256: 'a'.repeat(64), mime_type: 'application/pdf', size_bytes: 486000 },
    parse_status: { id: 'ps1', status: 'completed' },
    created_at: '2026-08-20T09:00:00Z',
  },
  {
    id: 'm2',
    course_id: 'c1',
    logical_name: '高等数学考核大纲.docx',
    material_type: 'assessment_syllabus',
    status: 'ready',
    latest_version: { id: 'v2', material_id: 'm2', status: 'active', version_no: 1, sha256: 'b'.repeat(64), mime_type: 'application/pdf', size_bytes: 312000 },
    parse_status: { id: 'ps2', status: 'completed' },
    created_at: '2026-08-20T09:05:00Z',
  },
  {
    id: 'm3',
    course_id: 'c1',
    logical_name: '高等数学教材（上册）.pdf',
    material_type: 'teaching_material',
    status: 'ready',
    latest_version: { id: 'v3', material_id: 'm3', status: 'active', version_no: 1, sha256: 'c'.repeat(64), mime_type: 'application/pdf', size_bytes: 8420000 },
    parse_status: { id: 'ps3', status: 'completed' },
    created_at: '2026-08-21T10:00:00Z',
  },
  {
    id: 'm4',
    course_id: 'c1',
    logical_name: '习题集·极限与导数.pdf',
    material_type: 'exercise',
    status: 'ready',
    latest_version: { id: 'v4', material_id: 'm4', status: 'active', version_no: 1, sha256: 'd'.repeat(64), mime_type: 'application/pdf', size_bytes: 1520000 },
    parse_status: { id: 'ps4', status: 'completed' },
    created_at: '2026-08-21T10:30:00Z',
  },
  {
    id: 'm5',
    course_id: 'c1',
    logical_name: '期中试卷·定积分专题.docx',
    material_type: 'exercise',
    status: 'ready',
    latest_version: { id: 'v5', material_id: 'm5', status: 'active', version_no: 1, sha256: 'e'.repeat(64), mime_type: 'application/pdf', size_bytes: 640000 },
    parse_status: { id: 'ps5', status: 'parsing' },
    created_at: '2026-08-28T14:00:00Z',
  },
];

// ───────────────────────────────────────────────
// 命题框架（已发布）
// ───────────────────────────────────────────────
export const demoFrameworkCandidate: FrameworkCandidate = {
  anchors: [
    {
      key: 'a1',
      title: '函数、极限与连续',
      exam_weight: 40,
      ability_requirements: ['理解极限的概念', '掌握极限运算法则', '判断函数的连续性'],
      allowed_question_types: ['choice', 'fill', 'solution'],
      excluded_content: ['高阶无穷小的复杂比较'],
      alignment_keys: ['ep1', 'ep2'],
    },
    {
      key: 'a2',
      title: '一元函数微分学',
      exam_weight: 35,
      ability_requirements: ['掌握导数与微分', '应用中值定理', '分析函数性态'],
      allowed_question_types: ['choice', 'fill', 'solution', 'proof'],
      excluded_content: [],
      alignment_keys: ['ep3', 'ep4'],
    },
    {
      key: 'a3',
      title: '一元函数积分学',
      exam_weight: 25,
      ability_requirements: ['掌握不定积分方法', '计算定积分', '定积分几何应用'],
      allowed_question_types: ['choice', 'fill', 'solution'],
      excluded_content: ['广义积分'],
      alignment_keys: ['ep5', 'ep6'],
    },
  ],
  exam_points: [
    { id: 'ep1', code: 'EP-1', anchor_key: 'a1', title: '极限的概念与运算', assessment_requirement: '能熟练运用极限运算法则求解各类极限', weight_value: 15, weight_source: 'syllabus', cognitive_targets: ['理解', '应用'], allowed_question_types: ['choice', 'fill', 'solution'], operational_detail_policy: '考查基本极限与两个重要极限' },
    { id: 'ep2', code: 'EP-2', anchor_key: 'a1', title: '连续性与间断点', assessment_requirement: '能判断函数连续性并分类间断点', weight_value: 10, weight_source: 'syllabus', cognitive_targets: ['理解', '分析'], allowed_question_types: ['choice', 'fill'], operational_detail_policy: '结合闭区间连续函数性质考查' },
    { id: 'ep3', code: 'EP-3', anchor_key: 'a2', title: '导数的定义与几何意义', assessment_requirement: '掌握导数定义，会求切线方程', weight_value: 12, weight_source: 'syllabus', cognitive_targets: ['理解', '应用'], allowed_question_types: ['choice', 'fill', 'solution'], operational_detail_policy: '考查导数的几何与物理意义' },
    { id: 'ep4', code: 'EP-4', anchor_key: 'a2', title: '微分中值定理', assessment_requirement: '理解并能证明中值定理相关命题', weight_value: 10, weight_source: 'syllabus', cognitive_targets: ['分析', '评价'], allowed_question_types: ['proof', 'choice'], operational_detail_policy: '罗尔、拉格朗日中值定理应用' },
    { id: 'ep5', code: 'EP-5', anchor_key: 'a3', title: '不定积分', assessment_requirement: '掌握换元积分与分部积分法', weight_value: 13, weight_source: 'syllabus', cognitive_targets: ['应用'], allowed_question_types: ['choice', 'fill', 'solution'], operational_detail_policy: '第一、第二换元与分部积分' },
    { id: 'ep6', code: 'EP-6', anchor_key: 'a3', title: '定积分及其应用', assessment_requirement: '能计算定积分并应用于几何问题', weight_value: 15, weight_source: 'syllabus', cognitive_targets: ['应用', '分析'], allowed_question_types: ['choice', 'solution'], operational_detail_policy: '定积分计算与面积/体积应用' },
  ],
  teaching_topics: [],
  conflicts: [],
  final_exam_rules: {
    total_score: 100,
    duration_minutes: 120,
    difficulty_mix: { easy: 0.3, medium: 0.5, hard: 0.2 },
  },
};

export const demoFramework: CurrentFrameworkResponse = {
  published: true,
  id: 'fv1',
  candidate_id: 'fv1',
  payload: demoFrameworkCandidate as unknown as Record<string, unknown>,
};

// ───────────────────────────────────────────────
// 知识目录（已发布）
// ───────────────────────────────────────────────
export const demoKnowledge: PublishedKnowledgeResponse = {
  published: true,
  catalog_version_id: 'kv1',
  framework_version_id: 'fv1',
  exam_points: demoFrameworkCandidate.exam_points,
  units: [
    { unit_id: 'u1', code: 'U-1', title: '极限运算', performance_statement: '掌握极限的四则运算法则与两个重要极限', exam_point_id: 'ep1', exam_point_code: 'EP-1', anchor_key: 'a1', card_ids: ['k1', 'k2'] },
    { unit_id: 'u2', code: 'U-2', title: '连续性', performance_statement: '理解函数连续性概念与间断点分类', exam_point_id: 'ep2', exam_point_code: 'EP-2', anchor_key: 'a1', card_ids: ['k3'] },
    { unit_id: 'u3', code: 'U-3', title: '导数概念', performance_statement: '掌握导数定义与几何意义', exam_point_id: 'ep3', exam_point_code: 'EP-3', anchor_key: 'a2', card_ids: ['k4'] },
    { unit_id: 'u4', code: 'U-4', title: '中值定理', performance_statement: '理解微分中值定理及其证明', exam_point_id: 'ep4', exam_point_code: 'EP-4', anchor_key: 'a2', card_ids: ['k5'] },
    { unit_id: 'u5', code: 'U-5', title: '积分方法', performance_statement: '掌握换元积分与分部积分法', exam_point_id: 'ep5', exam_point_code: 'EP-5', anchor_key: 'a3', card_ids: ['k6', 'k7'] },
    { unit_id: 'u6', code: 'U-6', title: '定积分应用', performance_statement: '能运用定积分求解几何问题', exam_point_id: 'ep6', exam_point_code: 'EP-6', anchor_key: 'a3', card_ids: ['k8'] },
  ],
  knowledge_cards: {
    k1: {
      id: 'k1', name: '极限的四则运算法则',
      performance_statement: '能运用极限的四则运算法则求解函数极限',
      assessable_content: ['极限的加法、乘法、除法法则', '复合函数极限'],
      scope_boundary: { includes: ['有限个函数的极限运算'], excludes: ['无穷大参与的未定式'] },
      cognitive_targets: ['应用'],
      allowed_question_types: ['choice', 'fill', 'solution'],
      importance: 4, concept_cluster: '极限理论',
      answer_proposition: '先分解再求极限，注意未定式需变形',
      answer_boundary: '答案需给出极限值或说明不存在',
      prompt_material: ['基本初等函数的极限公式'],
      relation_edges: [{ source: 'k1', target: 'k2', relation: 'precedes', confidence: 0.9 }],
      grounded: true,
    },
    k2: {
      id: 'k2', name: '两个重要极限',
      performance_statement: '能应用两个重要极限求解含三角、指数的极限',
      assessable_content: ['sin x / x 型极限', '(1+1/x)^x 型极限'],
      scope_boundary: { includes: ['重要极限的直接与变形应用'], excludes: [] },
      cognitive_targets: ['应用'],
      allowed_question_types: ['choice', 'fill'],
      importance: 5, concept_cluster: '极限理论',
      answer_proposition: '识别重要极限形式并作等价变形',
      answer_boundary: '结果需化简为最简形式',
      prompt_material: ['重要极限公式'],
      relation_edges: [],
      grounded: true,
    },
    k3: {
      id: 'k3', name: '间断点的分类',
      performance_statement: '能判断函数的连续性并对间断点分类',
      assessable_content: ['第一类间断点（可去、跳跃）', '第二类间断点'],
      scope_boundary: { includes: ['初等函数的连续性'], excludes: [] },
      cognitive_targets: ['理解', '分析'],
      allowed_question_types: ['choice', 'fill'],
      importance: 3, concept_cluster: '连续性',
      answer_proposition: '分别计算左右极限判断间断类型',
      answer_boundary: '需明确指出间断点类型',
      prompt_material: [],
      relation_edges: [],
      grounded: false,
    },
    k4: {
      id: 'k4', name: '导数的几何意义与切线方程',
      performance_statement: '能利用导数求曲线切线方程',
      assessable_content: ['导数的几何意义', '切线方程、法线方程'],
      scope_boundary: { includes: ['显式函数的切线'], excludes: ['参数方程求导'] },
      cognitive_targets: ['应用'],
      allowed_question_types: ['fill', 'solution'],
      importance: 4, concept_cluster: '微分学',
      answer_proposition: '求导得斜率，点斜式写切线方程',
      answer_boundary: '切线方程需化为一般式或斜截式',
      prompt_material: ['导数公式表'],
      relation_edges: [{ source: 'k4', target: 'k5', relation: 'prerequisite', confidence: 0.85 }],
      grounded: true,
    },
    k5: {
      id: 'k5', name: '罗尔定理与拉格朗日中值定理',
      performance_statement: '能运用中值定理证明相关命题',
      assessable_content: ['罗尔定理', '拉格朗日中值定理'],
      scope_boundary: { includes: ['中值定理的证明应用'], excludes: ['柯西中值定理'] },
      cognitive_targets: ['分析', '评价'],
      allowed_question_types: ['proof'],
      importance: 3, concept_cluster: '微分学',
      answer_proposition: '构造辅助函数，验证定理条件',
      answer_boundary: '证明过程需逻辑完整',
      prompt_material: [],
      relation_edges: [],
      grounded: true,
    },
    k6: {
      id: 'k6', name: '换元积分法',
      performance_statement: '能运用第一、第二换元法求不定积分',
      assessable_content: ['第一类换元（凑微分）', '第二类换元（三角代换等）'],
      scope_boundary: { includes: ['常见换元技巧'], excludes: [] },
      cognitive_targets: ['应用'],
      allowed_question_types: ['choice', 'fill', 'solution'],
      importance: 4, concept_cluster: '积分学',
      answer_proposition: '识别被积函数结构，选择合适代换',
      answer_boundary: '结果需加积分常数 C',
      prompt_material: ['基本积分公式'],
      relation_edges: [{ source: 'k6', target: 'k7', relation: 'related', confidence: 0.7 }],
      grounded: true,
    },
    k7: {
      id: 'k7', name: '分部积分法',
      performance_statement: '能运用分部积分法求解积分',
      assessable_content: ['分部积分公式', 'u、dv 的选取策略'],
      scope_boundary: { includes: ['反三角、对数函数的积分'], excludes: [] },
      cognitive_targets: ['应用'],
      allowed_question_types: ['solution'],
      importance: 4, concept_cluster: '积分学',
      answer_proposition: '按「反、对、幂、指、三」顺序选取 u',
      answer_boundary: '结果需加积分常数 C',
      prompt_material: [],
      relation_edges: [],
      grounded: true,
    },
    k8: {
      id: 'k8', name: '定积分的几何应用',
      performance_statement: '能运用定积分求解平面图形面积与旋转体体积',
      assessable_content: ['平面图形面积', '旋转体体积'],
      scope_boundary: { includes: ['直角坐标下的面积与体积'], excludes: ['参数方程、极坐标'] },
      cognitive_targets: ['应用', '分析'],
      allowed_question_types: ['solution'],
      importance: 5, concept_cluster: '积分学',
      answer_proposition: '确定积分区间与被积函数，列式求解',
      answer_boundary: '结果需给出精确值或近似值',
      prompt_material: [],
      relation_edges: [],
      grounded: false,
    },
  },
};

// ───────────────────────────────────────────────
// 证据链
// ───────────────────────────────────────────────
export const demoEvidence: EvidenceChunk[] = [
  {
    evidence_role: 'direct',
    confidence: 0.95,
    content: '教材定义：设函数 f(x) 在点 x0 的某去心邻域内有定义，若存在常数 A……',
    locator: '高等数学教材 §1.4 极限的运算法则',
    material_version_id: 'v3',
  },
  {
    evidence_role: 'supporting',
    confidence: 0.82,
    content: '教学大纲要求「掌握极限的四则运算法则」，属基本运算能力考核点。',
    locator: '高等数学教学大纲 第 2 章',
    material_version_id: 'v1',
  },
  {
    evidence_role: 'background',
    confidence: 0.6,
    content: '习题集收录 12 道极限运算练习题，覆盖加减乘除与复合函数极限。',
    locator: '习题集·极限与导数 第 1 节',
    material_version_id: 'v4',
  },
];

// ───────────────────────────────────────────────
// 试卷项目
// ───────────────────────────────────────────────
export const demoProjects: ExamProject[] = [
  {
    id: 'p1', course_id: 'c1', name: '期中测验·极限与导数', status: 'review',
    total_score: 100, item_count: 18, model: 'deepseek-v3', created_at: '2026-08-25T10:00:00Z', updated_at: '2026-08-28T09:00:00Z',
  },
  {
    id: 'p2', course_id: 'c1', name: '期末模拟卷一', status: 'contract',
    total_score: 100, item_count: 24, model: 'deepseek-v3', created_at: '2026-08-26T14:00:00Z', updated_at: '2026-08-29T11:00:00Z',
  },
  {
    id: 'p3', course_id: 'c1', name: '章节练习·定积分', status: 'exported',
    total_score: 50, item_count: 10, model: 'deepseek-v3', created_at: '2026-08-20T09:00:00Z', updated_at: '2026-08-22T16:00:00Z',
  },
];

// ───────────────────────────────────────────────
// Mock 路由
// ───────────────────────────────────────────────
export function matchMock(path: string, method: string, body?: string): unknown | undefined {
  const m = (method || 'GET').toUpperCase();
  let parsedBody: Record<string, unknown> | undefined;
  if (body) {
    try { parsedBody = JSON.parse(body); } catch { /* ignore */ }
  }

  // 课程列表 / 创建
  if (path === '/courses' && m === 'GET') return demoCourses;
  if (path === '/courses' && m === 'POST') {
    const name = (parsedBody?.name as string) || '新课程';
    return { ...demoCourses[0], id: 'c' + Date.now(), name, slug: 'course-' + Date.now() };
  }

  // 资料列表
  if (/^\/courses\/[^/]+\/materials$/.test(path) && m === 'GET') return demoMaterials;

  // 上传会话 / 完成上传（演示模式直接返回成功，upload_url 留空跳过真实 PUT）
  if (/^\/courses\/[^/]+\/upload-sessions$/.test(path) && m === 'POST') {
    const filename = (parsedBody?.filename as string) || 'file';
    return {
      session_id: 's' + Date.now(),
      object_key: 'mock/' + filename,
      upload_url: '',
      expires_at: new Date().toISOString(),
      headers: {},
    };
  }
  if (/^\/courses\/[^/]+\/upload-sessions\/[^/]+\/complete$/.test(path) && m === 'POST') {
    return { id: 'v' + Date.now(), material_id: 'm' + Date.now(), status: 'active', version_no: 1, sha256: 'e'.repeat(64), mime_type: 'application/pdf', size_bytes: 1024 };
  }

  // 解析 / 轮询 / 删除
  if (/^\/courses\/[^/]+\/materials\/[^/]+\/parse\/poll$/.test(path) && m === 'POST') {
    return { parse_status: { id: 'ps', status: 'completed' } };
  }
  if (/^\/courses\/[^/]+\/materials\/[^/]+\/parse$/.test(path) && m === 'POST') {
    return { status: 'completed' };
  }
  if (/^\/courses\/[^/]+\/materials\/[^/]+$/.test(path) && m === 'DELETE') return undefined;

  // 命题框架当前版本
  if (/\/framework-versions\/current$/.test(path)) return demoFramework;

  // 知识目录
  if (/\/published-knowledge$/.test(path)) return demoKnowledge;
  if (/\/published-knowledge\/cards\/[^/]+\/evidence$/.test(path)) return demoEvidence;

  // 试卷项目列表
  if (/^\/courses\/[^/]+\/exam-projects$/.test(path) && m === 'GET') return demoProjects;
  if (/^\/courses\/[^/]+\/exam-projects$/.test(path) && m === 'POST') {
    const name = (parsedBody?.name as string) || '新试卷项目';
    return { ...demoProjects[0], id: 'p' + Date.now(), name, status: 'blueprint' };
  }

  return undefined;
}
