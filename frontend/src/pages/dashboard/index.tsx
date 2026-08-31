import { useEffect, useState, type FC } from 'react';
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
import type { MaterialResponse, CurrentFrameworkResponse, PublishedKnowledgeResponse, ExamProject } from '@/types/api';

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

  // ── 加载中 ──
  if (loading) {
    return (
      <div className="page-enter">
        <div style={{ marginBottom: 'var(--space-xl)' }}>
          <div className="skeleton skeleton-title" style={{ width: '200px' }} />
          <div className="skeleton skeleton-text" style={{ width: '320px', marginTop: '8px' }} />
        </div>
        <SkeletonCardGrid count={4} />
      </div>
    );
  }

  // ── 统计数据 ──
  const materialStats = {
    categories: 4,
    parsed: materials.filter((m) =>
      ['parsed', 'completed', 'success'].includes(m.parse_status.status),
    ).length,
    unparsed: materials.filter((m) =>
      !['parsed', 'completed', 'success'].includes(m.parse_status.status),
    ).length,
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
    <div className="page-enter">
      <div style={{ marginBottom: 'var(--space-xl)' }}>
        <h1 style={{ fontSize: '1.75rem', fontWeight: 700, letterSpacing: '-0.03em', marginBottom: '6px' }}>课程工作台</h1>
        <p style={{ fontSize: '0.9375rem', color: 'var(--text-secondary)' }}>智能出卷系统 - 您的 AI 辅助教学助手</p>
      </div>

      <div className="card-grid">
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
          style={{ cursor: 'pointer' }}
          className="stagger-item"
        >
          <Card className="card-hover">
            <div style={{ display: 'flex', alignItems: 'flex-start', gap: '16px', marginBottom: '16px' }}>
              <div style={{
                width: 44, height: 44, borderRadius: '14px',
                background: 'var(--accent-subtle)', color: 'var(--accent)',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                flexShrink: 0,
              }}>
                <FolderOpen size={22} />
              </div>
              <div>
                <h3 style={{ fontSize: '1.05rem', fontWeight: 600, marginBottom: '4px' }}>资料库</h3>
                <p style={{ fontSize: '0.8125rem', color: 'var(--text-secondary)' }}>管理课程教学资料</p>
              </div>
            </div>
            <div style={{ display: 'flex', gap: '8px', marginBottom: '12px', flexWrap: 'wrap' }}>
              <BadgeSuccess>已解析 {materialStats.parsed}</BadgeSuccess>
              {materialStats.unparsed > 0 && <BadgeWarning>待解析 {materialStats.unparsed}</BadgeWarning>}
              <span className="badge badge-default">{materialStats.categories} 类资料</span>
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
          style={{ cursor: 'pointer' }}
          className="stagger-item"
        >
          <Card className="card-hover">
            <div style={{ display: 'flex', alignItems: 'flex-start', gap: '16px', marginBottom: '16px' }}>
              <div style={{
                width: 44, height: 44, borderRadius: '14px',
                background: 'var(--purple-subtle)', color: 'var(--purple)',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                flexShrink: 0,
              }}>
                <ClipboardList size={22} />
              </div>
              <div>
                <h3 style={{ fontSize: '1.05rem', fontWeight: 600, marginBottom: '4px' }}>命题框架</h3>
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
            <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
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
          style={{ cursor: 'pointer' }}
          className="stagger-item"
        >
          <Card className="card-hover">
            <div style={{ display: 'flex', alignItems: 'flex-start', gap: '16px', marginBottom: '16px' }}>
              <div style={{
                width: 44, height: 44, borderRadius: '14px',
                background: 'var(--info-subtle)', color: 'var(--info)',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                flexShrink: 0,
              }}>
                <Network size={22} />
              </div>
              <div>
                <h3 style={{ fontSize: '1.05rem', fontWeight: 600, marginBottom: '4px' }}>知识目录</h3>
                <p style={{ fontSize: '0.8125rem', color: 'var(--text-secondary)' }}>结构化知识卡片管理</p>
              </div>
            </div>
            <div style={{ display: 'flex', gap: '8px', marginBottom: '12px', flexWrap: 'wrap' }}>
              <BadgePurple>{knowledgeCardCount} 张知识卡</BadgePurple>
              <span className="badge badge-default">{evidenceCount} 条关联关系</span>
            </div>
            <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
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
          onClick={() => handleCardNavigate(`/courses/${activeCourseId}/exam-projects`)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' || e.key === ' ') {
              e.preventDefault();
              handleCardNavigate(`/courses/${activeCourseId}/exam-projects`);
            }
          }}
          style={{ cursor: 'pointer' }}
          className="stagger-item"
        >
          <Card className="card-hover">
            <div style={{ display: 'flex', alignItems: 'flex-start', gap: '16px', marginBottom: '16px' }}>
              <div style={{
                width: 44, height: 44, borderRadius: '14px',
                background: 'var(--success-subtle)', color: 'var(--success)',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                flexShrink: 0,
              }}>
                <FileQuestion size={22} />
              </div>
              <div>
                <h3 style={{ fontSize: '1.05rem', fontWeight: 600, marginBottom: '4px' }}>试卷项目</h3>
                <p style={{ fontSize: '0.8125rem', color: 'var(--text-secondary)' }}>创建和管理试卷项目</p>
              </div>
            </div>
            <div style={{ marginBottom: '12px' }}>
              {recentProjects.length === 0 ? (
                <p style={{ fontSize: '0.8125rem', color: 'var(--text-tertiary)' }}>暂无试卷项目</p>
              ) : (
                recentProjects.map((project) => (
                  <div
                    key={project.id}
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'space-between',
                      padding: '6px 0',
                      borderBottom: '1px solid rgba(0,0,0,0.04)',
                    }}
                  >
                    <span style={{ fontSize: '0.8125rem' }}>{project.name}</span>
                    <Badge>{project.status}</Badge>
                  </div>
                ))
              )}
            </div>
            <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
              <Button
                size="sm"
                onClick={(e) => handleButtonClick(e, `/courses/${activeCourseId}/exam-projects`)}
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
