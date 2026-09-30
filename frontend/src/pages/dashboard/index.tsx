import { useEffect, useMemo, useState, type FC } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import {
  Plus, ChevronRight, FolderOpen, ClipboardList, Network, FileQuestion,
} from 'lucide-react';
import { useToastStore } from '@/stores/toast';
import { useAuthStore } from '@/stores/auth';
import { api } from '@/api/client';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { BadgeSuccess, BadgeWarning, BadgePurple, Badge } from '@/components/ui/Badge';
import { SkeletonCardGrid } from '@/components/ui/Skeleton';
import { EXAM_PROJECT_STATUS_META } from '@/lib/examDisplay';
import type { MaterialResponse, CurrentFrameworkResponse, PublishedKnowledgeResponse, ExamProject } from '@/types/api';

/** 资料类型中文名（与资料库页一致；hero 分布条展示用） */
const MATERIAL_TYPE_LABELS: Record<string, string> = {
  teaching_syllabus: '教学大纲',
  assessment_syllabus: '考核大纲',
  teaching_material: '教材',
  exercise: '习题',
};

const DashboardPage: FC = () => {
  const navigate = useNavigate();
  const { courseId: routeCourseId } = useParams<{ courseId: string }>();
  const activeCourseId = routeCourseId || '';
  const token = useAuthStore().token;
  const addToast = useToastStore((s) => s.addToast);

  const [loading, setLoading] = useState(true);
  const [materials, setMaterials] = useState<MaterialResponse[]>([]);
  const [framework, setFramework] = useState<CurrentFrameworkResponse | null>(null);
  const [knowledgeCatalog, setKnowledgeCatalog] = useState<PublishedKnowledgeResponse | null>(null);
  const [examProjects, setExamProjects] = useState<ExamProject[]>([]);

  useEffect(() => {
    if (!activeCourseId || !token) return;

    let cancelled = false;

    const loadData = async () => {
      setLoading(true);
      try {
        const [materialsData, frameworkData, knowledgeData, projectsData] = await Promise.allSettled([
          api.materials.list(activeCourseId, token),
          api.framework.getCurrent(activeCourseId, token),
          api.knowledge.getPublished(activeCourseId, token),
          api.examProjects.list(activeCourseId, token),
        ]);

        if (!cancelled) {
          if (materialsData.status === 'fulfilled') {
            setMaterials(materialsData.value);
          }
          if (frameworkData.status === 'fulfilled') {
            setFramework(frameworkData.value);
          }
          if (knowledgeData.status === 'fulfilled') {
            setKnowledgeCatalog(knowledgeData.value);
          }
          if (projectsData.status === 'fulfilled') {
            setExamProjects(projectsData.value);
          }
        }
      } catch {
        if (!cancelled) {
          addToast('加载课程数据失败', 'error');
        }
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    };

    loadData();

    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeCourseId, token]);

  // hero 卡「按类型分布」：只列有数的类型，条长 ∝ 占比（与顶部统计瓦片不重复）。
  // Hook 必须置于 loading 早返回之前——不允许条件调用。
  const typeCounts = useMemo(() => {
    const counts = new Map<string, number>();
    materials.forEach((m) => counts.set(m.material_type, (counts.get(m.material_type) ?? 0) + 1));
    return [...counts.entries()].sort((a, b) => b[1] - a[1]);
  }, [materials]);

  // ── 加载中 ──
  if (loading) {
    return (
      <div className="page-enter page-stack">
        <div>
          <div className="skeleton skeleton-title" style={{ width: '200px' }} />
          <div className="skeleton skeleton-text" style={{ width: '320px', marginTop: '8px' }} />
        </div>
        <SkeletonCardGrid count={4} />
      </div>
    );
  }

  // ── 统计数据 ──
  // 解析状态机：null(未开始) / queued(排队) / processing(处理中) / ready(已解析) / failed(失败)
  const isParsed = (m: MaterialResponse) =>
    ['ready'].includes(m.parse_status?.status as string);
  const materialStats = {
    categories: new Set(materials.map((m) => m.material_type)).size,
    parsed: materials.filter(isParsed).length,
    unparsed: materials.filter((m) => !isParsed(m)).length,
    total: materials.length,
  };

  const isFrameworkPublished = framework?.published ?? false;
  const examPointCount = framework?.payload
    ? Array.isArray((framework.payload as Record<string, unknown>).exam_points)
      ? ((framework.payload as Record<string, unknown>).exam_points as unknown[]).length
      : 0
    : 0;

  const knowledgeCards = knowledgeCatalog?.knowledge_cards ?? {};
  const knowledgeCardCount = Object.keys(knowledgeCards).length;
  const evidenceCount = knowledgeCatalog
    ? Object.values(knowledgeCards).reduce<number>((sum, card) => {
        const edges = card.relation_edges;
        return sum + (Array.isArray(edges) ? edges.length : 0);
      }, 0)
    : 0;

  const recentProjects = examProjects.slice(-3);

  // ── 导航 ──
  const handleCardNavigate = (path: string) => navigate(path);

  const handleButtonClick = (e: React.MouseEvent, path: string) => {
    e.stopPropagation();
    navigate(path);
  };

  // ── 渲染 ──
  return (
    <div className="page-enter page-stack">
      <div>
        <h1 className="page-title">课程概览</h1>
        <p className="page-subtitle">智能出卷系统 · 从课程资料到成品试卷的完整链路</p>
      </div>

      {/* 统计瓦片行：白底 zinc + 色板三原色实色（蓝/紫/粉）混排 */}
      <div className="bento">
        <div className="glass-card" style={{ padding: '20px 22px', display: 'flex', flexDirection: 'column', gap: '6px' }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
            <span style={{ fontSize: '0.75rem', color: 'var(--text-secondary)' }}>课程资料</span>
            <FolderOpen size={16} style={{ color: 'var(--text-tertiary)' }} />
          </div>
          <div style={{ fontSize: '1.75rem', fontWeight: 600, letterSpacing: '-0.02em', lineHeight: 1.1 }}>
            {materialStats.total} <span style={{ fontSize: '0.875rem', fontWeight: 400, color: 'var(--text-tertiary)' }}>份</span>
          </div>
          <span className="tile-cap">已解析 {materialStats.parsed} · 未解析 {materialStats.unparsed}</span>
        </div>
        <div className="glass-card tile-brand" style={{ padding: '20px 22px', display: 'flex', flexDirection: 'column', gap: '6px' }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
            <span className="tile-cap">考核点</span>
            <ClipboardList size={16} style={{ opacity: 0.85 }} />
          </div>
          <div style={{ fontSize: '1.75rem', fontWeight: 600, letterSpacing: '-0.02em', lineHeight: 1.1 }}>{examPointCount}</div>
          <span className="tile-cap">{isFrameworkPublished ? '框架已发布' : '框架待构建'}</span>
        </div>
        <div className="glass-card tile-purple" style={{ padding: '20px 22px', display: 'flex', flexDirection: 'column', gap: '6px' }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
            <span className="tile-cap">知识卡</span>
            <Network size={16} style={{ opacity: 0.85 }} />
          </div>
          <div style={{ fontSize: '1.75rem', fontWeight: 600, letterSpacing: '-0.02em', lineHeight: 1.1 }}>{knowledgeCardCount}</div>
          <span className="tile-cap">{evidenceCount} 条关联关系</span>
        </div>
        <div className="glass-card tile-pink" style={{ padding: '20px 22px', display: 'flex', flexDirection: 'column', gap: '6px' }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
            <span className="tile-cap">试卷项目</span>
            <FileQuestion size={16} style={{ opacity: 0.85 }} />
          </div>
          <div style={{ fontSize: '1.75rem', fontWeight: 600, letterSpacing: '-0.02em', lineHeight: 1.1 }}>{examProjects.length}</div>
          <span className="tile-cap">最近 {recentProjects.length} 个进行中</span>
        </div>
      </div>

      {/* 模块瓦片：大卡 2x2 放主入口，宽卡 2x1 放次级模块，全宽卡放项目动态 */}
      <div className="bento">
        {/* 1. 资料库 */}
        <div
          role="button"
          tabIndex={0}
          onClick={() => handleCardNavigate(`/courses/${activeCourseId}/materials`)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' || e.key === ' ') {
              e.preventDefault();
              handleCardNavigate(`/courses/${activeCourseId}/materials`);
            }
          }}
          style={{ cursor: 'pointer', display: 'flex' }}
          className="stagger-item bento-col-2 bento-row-2"
        >
          <Card
            className="card-hover tile-dark"
            style={{
              flex: 1,
              display: 'flex',
              flexDirection: 'column',
              // 余量均摊到每个内容带之间（space-between），不让 auto 边距
              // 把虚空堆在一处——bento hero 的均匀呼吸感
              justifyContent: 'space-between',
              gap: 18,
              padding: 28,
              // 主卡内一层极淡的蓝色径向光：深色瓦片的层次感，不破坏 bento 的干净
              backgroundImage: 'radial-gradient(420px 200px at 88% -10%, rgba(59,130,246,0.22), transparent 70%)',
            }}
          >
            {/* 头部：品牌蓝图标牌 + 标题 */}
            <div style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
              <span style={{
                width: 48, height: 48, borderRadius: 14, background: 'var(--brand)', color: '#ffffff',
                display: 'inline-flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0,
              }}>
                <FolderOpen size={24} />
              </span>
              <div>
                <h3 className="card-title" style={{ marginBottom: 2, fontSize: '1.125rem' }}>资料库</h3>
                <p style={{ fontSize: '0.8125rem', color: 'rgba(255,255,255,0.72)' }}>管理课程教学资料</p>
              </div>
            </div>

            {/* Hero 大数字 */}
            <div style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
              <span style={{ fontSize: '2.75rem', fontWeight: 600, letterSpacing: '-0.02em', lineHeight: 1, color: '#ffffff' }}>
                {materialStats.total}
              </span>
              <span style={{ fontSize: '0.85rem', color: 'rgba(255,255,255,0.72)' }}>
                份资料 · {materialStats.categories} 类
              </span>
            </div>

            {/* 按类型分布：小条形图（bento hero 的多条进度模式），
                与顶部统计瓦片不重复 */}
            {typeCounts.length > 0 && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
                <span style={{ fontSize: '0.75rem', color: 'rgba(255,255,255,0.6)' }}>按类型分布</span>
                {typeCounts.map(([type, count]) => (
                  <div key={type} style={{ display: 'flex', alignItems: 'center', gap: 10, minWidth: 0 }}>
                    <span style={{ fontSize: '0.78rem', color: 'rgba(255,255,255,0.72)', width: 58, flexShrink: 0 }}>
                      {MATERIAL_TYPE_LABELS[type] ?? type}
                    </span>
                    <div style={{ flex: 1, height: 6, borderRadius: 999, background: 'rgba(255,255,255,0.12)', overflow: 'hidden' }}>
                      <div style={{
                        width: materialStats.total ? Math.round((count / materialStats.total) * 100) + '%' : '0%',
                        height: '100%', borderRadius: 999, background: 'rgba(255,255,255,0.85)',
                        transition: 'width 0.3s var(--ease-snappy)',
                      }} />
                    </div>
                    <span style={{ fontSize: '0.75rem', color: 'rgba(255,255,255,0.72)', width: 24, textAlign: 'right', flexShrink: 0, fontVariantNumeric: 'tabular-nums' }}>
                      {count}
                    </span>
                  </div>
                ))}
              </div>
            )}

            {/* 解析进度：紧随分布块，CTA 之前 */}
            <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
                <span style={{ fontSize: '0.75rem', color: 'rgba(255,255,255,0.6)' }}>解析进度</span>
                <span style={{ fontSize: '0.75rem', color: 'rgba(255,255,255,0.72)', fontVariantNumeric: 'tabular-nums' }}>
                  {materialStats.total ? Math.round((materialStats.parsed / materialStats.total) * 100) : 0}%
                </span>
              </div>
              <div style={{ height: 6, borderRadius: 999, background: 'rgba(255,255,255,0.14)', overflow: 'hidden' }}>
                <div style={{
                  width: materialStats.total ? Math.round((materialStats.parsed / materialStats.total) * 100) + '%' : '0%',
                  height: '100%', borderRadius: 999, background: '#ffffff',
                  transition: 'width 0.3s var(--ease-snappy)',
                }} />
              </div>
            </div>

            <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
              <Button
                variant="secondary"
                size="sm"
                onClick={(e) => handleButtonClick(e, `/courses/${activeCourseId}/materials`)}
              >
                查看资料库
                <ChevronRight size={16} />
              </Button>
            </div>
          </Card>
        </div>

        {/* 2. 命题框架 */}
        <div
          role="button"
          tabIndex={0}
          onClick={() => handleCardNavigate(`/courses/${activeCourseId}/framework`)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' || e.key === ' ') {
              e.preventDefault();
              handleCardNavigate(`/courses/${activeCourseId}/framework`);
            }
          }}
          style={{ cursor: 'pointer', display: 'flex' }}
          className="stagger-item bento-col-2"
        >
          <Card className="card-hover" style={{ flex: 1, display: 'flex', flexDirection: 'column' }}>
            <div style={{ display: 'flex', alignItems: 'flex-start', gap: '16px', marginBottom: '16px' }}>
              <div className="icon-box" style={{ width: 44, height: 44, flexShrink: 0 }}>
                <ClipboardList size={22} />
              </div>
              <div>
                <h3 className="card-title" style={{ marginBottom: '4px' }}>命题框架</h3>
                <p style={{ fontSize: '0.8125rem', color: 'var(--text-secondary)' }}>构建试卷命题框架</p>
              </div>
            </div>
            <div style={{ display: 'flex', gap: '8px', marginBottom: '12px', flexWrap: 'wrap' }}>
              {isFrameworkPublished ? (
                <BadgeSuccess>已发布</BadgeSuccess>
              ) : (
                <BadgeWarning>草稿</BadgeWarning>
              )}
              <span className="badge badge-default">{examPointCount} 个考核点</span>
            </div>
            <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 'auto' }}>
              <Button
                size="sm"
                onClick={(e) => handleButtonClick(e, `/courses/${activeCourseId}/framework`)}
              >
                构建框架
                <ChevronRight size={16} />
              </Button>
            </div>
          </Card>
        </div>

        {/* 3. 知识目录 */}
        <div
          role="button"
          tabIndex={0}
          onClick={() => handleCardNavigate(`/courses/${activeCourseId}/knowledge`)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' || e.key === ' ') {
              e.preventDefault();
              handleCardNavigate(`/courses/${activeCourseId}/knowledge`);
            }
          }}
          style={{ cursor: 'pointer', display: 'flex' }}
          className="stagger-item bento-col-2"
        >
          <Card className="card-hover" style={{ flex: 1, display: 'flex', flexDirection: 'column' }}>
            <div style={{ display: 'flex', alignItems: 'flex-start', gap: '16px', marginBottom: '16px' }}>
              <div className="icon-box" style={{ width: 44, height: 44, flexShrink: 0 }}>
                <Network size={22} />
              </div>
              <div>
                <h3 className="card-title" style={{ marginBottom: '4px' }}>知识目录</h3>
                <p style={{ fontSize: '0.8125rem', color: 'var(--text-secondary)' }}>结构化知识卡片管理</p>
              </div>
            </div>
            <div style={{ display: 'flex', gap: '8px', marginBottom: '12px', flexWrap: 'wrap' }}>
              <BadgePurple>{knowledgeCardCount} 张知识卡</BadgePurple>
              <span className="badge badge-default">{evidenceCount} 条关联关系</span>
            </div>
            <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 'auto' }}>
              <Button
                variant="secondary"
                size="sm"
                onClick={(e) => handleButtonClick(e, `/courses/${activeCourseId}/knowledge`)}
              >
                查看知识目录
                <ChevronRight size={16} />
              </Button>
            </div>
          </Card>
        </div>

        {/* 4. 试卷项目 */}
        <div
          role="button"
          tabIndex={0}
          onClick={() => handleCardNavigate(`/courses/${activeCourseId}/paper`)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' || e.key === ' ') {
              e.preventDefault();
              handleCardNavigate(`/courses/${activeCourseId}/paper`);
            }
          }}
          style={{ cursor: 'pointer', display: 'flex' }}
          className="stagger-item bento-col-4"
        >
          <Card className="card-hover" style={{ flex: 1, display: 'flex', flexDirection: 'column' }}>
            <div style={{ display: 'flex', alignItems: 'flex-start', gap: '16px', marginBottom: '16px' }}>
              <div className="icon-box" style={{ width: 44, height: 44, flexShrink: 0 }}>
                <FileQuestion size={22} />
              </div>
              <div>
                <h3 className="card-title" style={{ marginBottom: '4px' }}>试卷项目</h3>
                <p style={{ fontSize: '0.8125rem', color: 'var(--text-secondary)' }}>创建和管理试卷项目</p>
              </div>
            </div>
            <div style={{ marginBottom: '12px', display: 'flex', flexWrap: 'wrap', gap: '8px' }}>
              {recentProjects.length === 0 ? (
                <p style={{ fontSize: '0.8125rem', color: 'var(--text-tertiary)' }}>暂无试卷项目</p>
              ) : (
                recentProjects.map((project) => (
                  <span
                    key={project.id}
                    title={project.status}
                    style={{
                      display: 'inline-flex', alignItems: 'center', gap: '8px',
                      padding: '6px 12px', borderRadius: 'var(--radius-sm)',
                      background: 'var(--fill)', fontSize: '0.8125rem',
                    }}
                  >
                    {project.name}
                    <Badge variant={EXAM_PROJECT_STATUS_META[project.status]?.variant ?? 'default'}>
                      {EXAM_PROJECT_STATUS_META[project.status]?.label ?? '未知状态'}
                    </Badge>
                  </span>
                ))
              )}
            </div>
            <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 'auto' }}>
              <Button
                size="sm"
                onClick={(e) => handleButtonClick(e, `/courses/${activeCourseId}/paper`)}
              >
                <Plus size={16} />
                新建试卷项目
              </Button>
            </div>
          </Card>
        </div>
      </div>
    </div>
  );
};

export default DashboardPage;
