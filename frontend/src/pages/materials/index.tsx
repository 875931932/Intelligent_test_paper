import { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import { useNavigate, useParams, useSearchParams } from 'react-router-dom';
import {
  Upload, RefreshCw, Trash2, FileText,
  Folder, FolderOpen, BookOpen, ClipboardCheck, BookMarked, X, Files, Eye,
  Package, Pencil, RotateCcw,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import { api } from '@/api/client';
import { useCourseStore } from '@/stores/course';
import { useToastStore } from '@/stores/toast';
import { Button, Modal, Badge, SkeletonCardGrid } from '@/components/ui';
import { computeSha256 } from '@/lib/sha256';
import { downloadBlob } from '@/lib/download';
import { PARSE_STATUS_LABELS } from '@/utils/format';
import { qlabel } from '@/lib/examDisplay';
import type { PaperExportKind } from '@/api/domains/paperVersions';
import type { MaterialResponse, PaperArchiveDetail, PaperArchiveSummary } from '@/types/api';

type FolderKey = 'syllabus' | 'materials' | 'papers';
type SubFolderKey = 'teaching_syllabus' | 'assessment_syllabus' | 'teaching_material' | 'exercise';

interface SubFolderMeta {
  key: SubFolderKey;
  name: string;
  description: string;
  icon: LucideIcon;
  color: string;
}

interface FolderMeta {
  key: FolderKey;
  name: string;
  description: string;
  icon: LucideIcon;
  subFolders: SubFolderMeta[];
}

interface UploadItem {
  file: File;
  type: string;
}

// 资料页文案沿用本页历史用词（teaching_material 显示「教材」），与 utils/format
// 的共享版仅在该词条上有差异，故不整体替换
const MATERIAL_TYPE_LABELS: Record<string, string> = {
  teaching_syllabus: '教学大纲',
  assessment_syllabus: '考核大纲',
  teaching_material: '教材',
  exercise: '习题',
};

const MATERIAL_TYPE_VARIANTS: Record<string, string> = {
  teaching_syllabus: 'info',
  assessment_syllabus: 'success',
  teaching_material: 'default',
  exercise: 'warning',
};

const PARSE_STATUS_VARIANTS: Record<string, string> = {
  queued: 'default',
  submitted: 'warning',
  waiting_file: 'warning',
  pending: 'default',
  running: 'warning',
  converting: 'warning',
  ready: 'success',
  failed: 'error',
};

// document_parse_runs 的进行中状态；terminal 为 ready / failed
const RUNNING_PARSE_STATES = new Set([
  'queued',
  'submitted',
  'waiting_file',
  'pending',
  'running',
  'converting',
]);

// 以后端 parse_status.status 为唯一状态源，跨页面导航仍能恢复
const isParsing = (m: MaterialResponse): boolean =>
  !!m.parse_status && RUNNING_PARSE_STATES.has(m.parse_status.status);

// 解析已结束（terminal：已完成/失败），此时按钮应显示“重新解析”
const isParsed = (m: MaterialResponse): boolean =>
  !!m.parse_status &&
  (m.parse_status.status === 'ready' || m.parse_status.status === 'failed');

const FOLDER_GROUPS: FolderMeta[] = [
  {
    key: 'syllabus',
    name: '课程大纲',
    description: '教学大纲与考核大纲',
    icon: Folder,
    subFolders: [
      { key: 'teaching_syllabus', name: '教学大纲', description: '课程教学目标与内容范围', icon: BookOpen, color: 'var(--accent)' },
      { key: 'assessment_syllabus', name: '考核大纲', description: '考核方式与评分标准', icon: ClipboardCheck, color: 'var(--success)' },
    ],
  },
  {
    key: 'materials',
    name: '课程资料',
    description: '教材与习题等教学资源',
    icon: FolderOpen,
    subFolders: [
      { key: 'teaching_material', name: '教材', description: '教学用书与讲义', icon: BookMarked, color: 'var(--purple)' },
      { key: 'exercise', name: '习题', description: '练习与试卷', icon: FileText, color: 'var(--warning)' },
    ],
  },
  {
    key: 'papers',
    name: '试卷',
    description: '从试卷页保存的归档，可编辑可下载',
    icon: Files,
    // 无子分区：进文件夹直接看归档列表
    subFolders: [],
  },
];

const ALL_TYPE_OPTIONS = [
  { value: 'teaching_syllabus', label: '教学大纲' },
  { value: 'assessment_syllabus', label: '考核大纲' },
  { value: 'teaching_material', label: '教材' },
  { value: 'exercise', label: '习题' },
];

// 直传对象存储的最长等待时间，超时即中止并报错，避免无限挂起
const DIRECT_UPLOAD_TIMEOUT_MS = 5 * 60 * 1000;

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

function isSyllabus(type: string) {
  return type === 'teaching_syllabus' || type === 'assessment_syllabus';
}

export default function MaterialsPage() {
  const { courseId: routeCourseId } = useParams<{ courseId: string }>();
  const { activeCourseId } = useCourseStore();
  const courseId = routeCourseId || activeCourseId || '';
  const { addToast } = useToastStore();
  const navigate = useNavigate();
  // ?folder=<key> 深链：从归档编辑页「返回试卷文件夹」原路回到 试卷 文件夹
  const [searchParams] = useSearchParams();

  const [materials, setMaterials] = useState<MaterialResponse[]>([]);
  const [loading, setLoading] = useState(true);

  const [activeFolder, setActiveFolder] = useState<FolderKey | null>(() => {
    const f = searchParams.get('folder');
    return f === 'syllabus' || f === 'materials' || f === 'papers' ? f : null;
  });
  const [activeSubFolder, setActiveSubFolder] = useState<SubFolderKey | null>(null);

  const [uploadOpen, setUploadOpen] = useState(false);
  const [uploadItems, setUploadItems] = useState<UploadItem[]>([]);
  const [uploading, setUploading] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const [deleteId, setDeleteId] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);

  // ── 「试卷」文件夹：归档快照（与材料文件夹无关，走 paper-archives 接口） ──
  const [archives, setArchives] = useState<PaperArchiveSummary[]>([]);
  const [archivesLoading, setArchivesLoading] = useState(true);
  const [archiveDetail, setArchiveDetail] = useState<PaperArchiveDetail | null>(null);
  const [archiveDeleteId, setArchiveDeleteId] = useState<string | null>(null);
  const [archiveBusy, setArchiveBusy] = useState(false);
  // 「存回试卷区」确认弹窗（会成为项目当前卷，必须先说清后果）
  const [restoreOpen, setRestoreOpen] = useState(false);

  // 单一批量轮询定时器：多文件解析共享一个定时器，一次静默 list 返回全部状态，避免 N 个定时器各查一次库
  const pollingTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  // 定时器只建一次、回调闭包会拿首轮的旧清单：解析中的文件清单必须经 ref 取当前值
  const materialsRef = useRef<MaterialResponse[]>([]);
  useEffect(() => {
    materialsRef.current = materials;
  }, [materials]);

  const loadMaterials = useCallback(async (silent = false) => {
    if (!courseId) return;
    try {
      // 静默刷新用于批量轮询/单文件完成：不置 loading，避免整页闪烁
      if (!silent) setLoading(true);
      const data = await api.materials.list(courseId);
      setMaterials(Array.isArray(data) ? data : []);
    } catch {
      addToast('加载资料列表失败', 'error');
    } finally {
      if (!silent) setLoading(false);
    }
  }, [courseId, addToast]);

  // 归档列表随页面挂载取一次：状态变更全部落在 await 之后，不进 effect 同步 setState；
  // 供根文件夹计数与「试卷」文件夹列表共用
  const loadArchives = useCallback(async () => {
    if (!courseId) return;
    try {
      const data = await api.paperArchives.list(courseId);
      setArchives(Array.isArray(data) ? data : []);
    } catch {
      addToast('加载试卷归档失败', 'error');
    } finally {
      setArchivesLoading(false);
    }
  }, [courseId, addToast]);

  useEffect(() => {
    // 挂载一次取两个列表：材料（解析轮询的基准）+ 归档（根文件夹计数也依赖它）
    loadMaterials();
    void loadArchives();
    return () => {
      if (pollingTimerRef.current) clearInterval(pollingTimerRef.current);
      pollingTimerRef.current = null;
    };
  }, [loadMaterials, loadArchives]);

  const openArchive = async (id: string) => {
    if (!courseId) return;
    try {
      setArchiveDetail(await api.paperArchives.get(courseId, id));
    } catch {
      addToast('加载归档详情失败', 'error');
    }
  };

  // 归档删除 = 真删；只影响文件夹里的副本，不动原试卷
  const handleArchiveDelete = async () => {
    if (!courseId || !archiveDeleteId) return;
    setArchiveBusy(true);
    try {
      await api.paperArchives.remove(courseId, archiveDeleteId);
      setArchives((prev) => prev.filter((a) => a.id !== archiveDeleteId));
      setArchiveDeleteId(null);
      addToast('已删除归档试卷', 'success');
    } catch (err) {
      addToast(err instanceof Error ? err.message : '删除归档失败', 'error');
    } finally {
      setArchiveBusy(false);
    }
  };

  // ── P2c：归档编辑 / 存回试卷区 / 四件套导出 ──
  // 导出端点要 Authorization 头：token 不进 URL，带鉴权拉 Blob 再本地下载

  const editArchive = (id: string) => {
    setArchiveDetail(null);
    navigate('/courses/' + courseId + '/paper-archive/' + id);
  };

  const exportArchive = async (kind: PaperExportKind) => {
    if (!courseId || !archiveDetail) return;
    try {
      const { blob, filename } = await api.paperArchives.fetchExport(
        kind, courseId, archiveDetail.id, undefined,
        kind === 'card' || kind === 'student' ? 'docx' : undefined,
      );
      downloadBlob(blob, filename);
    } catch (err) {
      addToast(err instanceof Error ? err.message : '导出失败', 'error');
    }
  };

  const exportArchiveBundle = async () => {
    if (!courseId || !archiveDetail) return;
    try {
      const { blob, filename } = await api.paperArchives.fetchBundle(
        courseId, archiveDetail.id, archiveDetail.snapshot.version_no,
      );
      downloadBlob(blob, filename);
    } catch (err) {
      addToast(err instanceof Error ? err.message : '打包下载失败', 'error');
    }
  };

  // 存回 = 按快照新建一版并设为项目当前卷（归档本身不动）
  const handleArchiveRestore = async () => {
    if (!courseId || !archiveDetail) return;
    setArchiveBusy(true);
    try {
      const out = await api.paperArchives.restore(courseId, archiveDetail.id);
      setRestoreOpen(false);
      addToast(`已存回试卷区：${out.item_count} 道题，当前卷 v${out.version_no}`, 'success');
    } catch (err) {
      addToast(err instanceof Error ? err.message : '存回试卷区失败', 'error');
    } finally {
      setArchiveBusy(false);
    }
  };

  const filteredMaterials = useMemo(() => {
    if (activeSubFolder) return materials.filter((m) => m.material_type === activeSubFolder);
    if (activeFolder === 'syllabus') return materials.filter((m) => isSyllabus(m.material_type));
    if (activeFolder === 'materials') return materials.filter((m) => !isSyllabus(m.material_type));
    return materials;
  }, [materials, activeFolder, activeSubFolder]);

  const countByType = useCallback(
    (type: string) => materials.filter((m) => m.material_type === type).length,
    [materials]
  );

  // ── 多文件上传 ──
  const openUpload = () => {
    setUploadItems([]);
    setUploadOpen(true);
  };

  const defaultTypeForNewFile = (): string => {
    if (activeSubFolder) return activeSubFolder;
    // 在「课程大纲」父文件夹上传时默认按大纲归类（教学大纲），不再默认成教材；
    // 教材/习题文件夹保持原默认「教材」。用户仍可在弹窗内逐文件改类型。
    if (activeFolder === 'syllabus') return 'teaching_syllabus';
    return 'teaching_material';
  };

  const directPut = async (url: string, file: File, sha256: string, extraHeaders: Record<string, string>) => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), DIRECT_UPLOAD_TIMEOUT_MS);
    let res: Response;
    try {
      res = await fetch(url, {
        method: 'PUT',
        body: file,
        signal: controller.signal,
        headers: {
          'Content-Type': file.type || 'application/octet-stream',
          'x-amz-meta-sha256': sha256,
          ...extraHeaders,
        },
      });
    } finally {
      clearTimeout(timer);
    }
    if (!res.ok) {
      throw new Error('object storage upload failed: ' + res.status);
    }
  };

  const handleFilesChange = (files: FileList | null) => {
    if (!files || files.length === 0) return;
    const defaultType = defaultTypeForNewFile();
    const newItems: UploadItem[] = Array.from(files).map((file) => ({ file, type: defaultType }));
    setUploadItems((prev) => [...prev, ...newItems]);
    if (fileInputRef.current) fileInputRef.current.value = '';
  };

  const updateItemType = (index: number, type: string) => {
    setUploadItems((prev) => prev.map((it, i) => (i === index ? { ...it, type } : it)));
  };

  const removeItem = (index: number) => {
    setUploadItems((prev) => prev.filter((_, i) => i !== index));
  };

  const handleUpload = async () => {
    if (!courseId || uploadItems.length === 0) {
      addToast('请先选择要上传的文件', 'error');
      return;
    }
    setUploading(true);
    let successCount = 0;
    try {
      for (const item of uploadItems) {
        const sha256 = await computeSha256(item.file);
        const session = await api.materials.createUploadSession(courseId, {
          filename: item.file.name,
          material_type: item.type,
          size_bytes: item.file.size,
          sha256,
          mime_type: item.file.type || 'application/octet-stream',
        });

        // 直传对象存储（若后端返回 upload_url）
        if (session?.upload_url) {
          await directPut(session.upload_url, item.file, sha256, session.headers || {});
        } else {
          await api.uploadBinary('/_local-storage/' + session.object_key, item.file);
        }

        await api.materials.completeUpload(courseId, session.session_id);

        successCount++;
      }
      addToast(`成功上传 ${successCount} 份资料`, 'success');
      setUploadOpen(false);
      setUploadItems([]);
      loadMaterials();
    } catch (err) {
      // 如实暴露错误，便于排障（先前只弹 toast 导致控制台无任何信息）
      const reason = err instanceof Error ? err.message : String(err);
      console.error('[upload] 上传失败:', err);
      addToast(
        successCount > 0 ? `部分上传失败（成功 ${successCount} 份）` : `上传失败：${reason}`,
        successCount > 0 ? 'info' : 'error'
      );
    } finally {
      setUploading(false);
    }
  };

  // ── 解析 ──
  // 单一批量轮询：所有解析中的文件共享一个定时器，每次先逐份 /parse/poll 推进状态机
  //（MinerU 提交后必须有人推进，否则永远停在 submitted/running），再静默 list 一次拿到
  // 全部 parse_status。全部 terminal 即停止并提示一次。
  const startPolling = useCallback(() => {
    if (!courseId || pollingTimerRef.current) return;
    pollingTimerRef.current = setInterval(async () => {
      const pending = materialsRef.current.filter(isParsing);
      if (pending.length > 0) {
        // 单份推进失败不影响其余（如 run 尚未提交的 409）；真实终态由随后的 list 反映
        await Promise.allSettled(pending.map((m) => api.materials.pollParse(courseId, m.id)));
      }
      // 静默刷新（不置 loading），避免整页闪烁
      await loadMaterials(true);
    }, 2000);
  }, [courseId, loadMaterials]);

  // 状态变化时判定是否还有解析中的文件：无则停掉批量轮询；在解析中则确保轮询已启动。
  // 仅在发生一次“进行中 → 全部结束”的转变时提示结果，避免反复弹 toast。
  useEffect(() => {
    const hasParsing = materials.some(isParsing);
    if (hasParsing) {
      startPolling();
      return;
    }
    if (pollingTimerRef.current) {
      clearInterval(pollingTimerRef.current);
      pollingTimerRef.current = null;
    }
  }, [materials, startPolling]);

  // 解析结果提示：跟踪上一次“是否有解析中”的状态，一次完成态变化只提示一次
  const hadParsingRef = useRef(false);
  useEffect(() => {
    const hasParsing = materials.some(isParsing);
    if (hadParsingRef.current && !hasParsing) {
      const anyFailed = materials.some((m) => m.parse_status?.status === 'failed');
      addToast(anyFailed ? '部分文件解析失败' : '全部解析完成', anyFailed ? 'error' : 'success');
    }
    hadParsingRef.current = hasParsing;
  }, [materials, addToast]);

  // 标记是否已发起过解析，供批量完成提示判断（避免页面加载后默认弹出的“全部解析完成”）
  const handleParseAllStarted = useRef(false);

  const handleParse = async (material: MaterialResponse) => {
    // 记住乐观覆盖前的原始状态，请求失败时回退，避免卡死在 running
    const prevStatus = material.parse_status;
    try {
      // 乐观置为“解析中”，让按钮/标签立即反馈，无需等首次轮询
      setMaterials((prev) =>
        prev.map((m) =>
          m.id === material.id
            ? {
                ...m,
                parse_status: {
                  id: m.parse_status?.id ?? material.id,
                  status: 'running',
                  error_code: undefined,
                  error_summary: undefined,
                },
              }
            : m
        )
      );
      await api.materials.parse(courseId, material.id);
      handleParseAllStarted.current = true;
      // 由统一 effect 根据“是否有解析中”启动单个批量轮询，一次查询全部状态
      startPolling();
    } catch {
      // 触发失败：恢复原状态，避免残留“解析中”
      setMaterials((prev) =>
        prev.map((m) => (m.id === material.id ? { ...m, parse_status: prevStatus } : m))
      );
      addToast('触发解析失败', 'error');
    }
  };

  // 一键解析：批量解析当前文件夹下所有“未解析 / 已失败”的文件
  const handleParseAll = useCallback(() => {
    const pending = filteredMaterials.filter(
      (m) => !isParsing(m) && m.parse_status?.status !== 'ready'
    );
    if (pending.length === 0) {
      addToast('当前文件夹没有需要解析的文件', 'info');
      return;
    }
    pending.forEach((m) => handleParse(m));
    addToast(`已对 ${pending.length} 份文件发起解析`, 'info');
  }, [filteredMaterials, handleParse, addToast]);

  const handleDelete = async () => {
    if (!deleteId) return;
    try {
      setDeleting(true);
      await api.materials.delete(courseId, deleteId);
      addToast('删除成功', 'success');
      loadMaterials();
      setDeleteId(null);
    } catch {
      addToast('删除失败', 'error');
    } finally {
      setDeleting(false);
    }
  };

  const currentFolder = activeFolder ? FOLDER_GROUPS.find((f) => f.key === activeFolder) : null;
  const currentSubFolder = activeSubFolder ? currentFolder?.subFolders.find((s) => s.key === activeSubFolder) : null;

  const archiveGrid = () => {
    if (archivesLoading) return <SkeletonCardGrid count={3} />;

    if (archives.length === 0) {
      return (
        <div style={{ padding: '80px 20px', display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '12px', textAlign: 'center' }}>
          <div style={{ width: 56, height: 56, borderRadius: 'var(--radius-lg)', background: 'var(--brand)', color: '#ffffff', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
            <Files size={28} />
          </div>
          <h3 style={{ fontSize: '1.125rem', fontWeight: 600 }}>暂无归档试卷</h3>
          <p style={{ fontSize: '0.875rem', color: 'var(--text-secondary)', maxWidth: 460 }}>
            在试卷页点「保存到资料库」，这里就会多出一份副本。
            归档是独立快照：原卷被「只留最近 3 份」清掉后，它依然可看、可编辑、可下载。
          </p>
        </div>
      );
    }

    return (
      <div className="bento bento-3">
        {archives.map((a) => (
          <div
            key={a.id}
            className="sub-section"
            style={{ padding: '18px', display: 'flex', flexDirection: 'column', gap: '12px' }}
          >
            <div style={{ display: 'flex', alignItems: 'flex-start', gap: '12px' }}>
              <div style={{
                width: 44, height: 44, borderRadius: '12px',
                background: 'var(--accent-subtle)', color: 'var(--accent)',
                display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0,
              }}>
                <FileText size={22} />
              </div>
              <div style={{ minWidth: 0, flex: 1 }}>
                <h4 style={{
                  fontSize: '0.95rem', fontWeight: 600, margin: 0,
                  overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                }} title={a.name}>
                  {a.name}
                </h4>
                <p style={{ fontSize: '0.8rem', color: 'var(--text-tertiary)', marginTop: '4px' }}>
                  {a.project_name ?? '未知项目'}
                  {a.source_version_no != null ? ` · 源卷 v${a.source_version_no}` : ''}
                </p>
              </div>
            </div>

            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginTop: 'auto' }}>
              <Badge variant="default">{a.item_count} 题</Badge>
              <Badge variant="info">{a.total_score} 分</Badge>
              <span style={{ fontSize: '0.72rem', color: 'var(--text-tertiary)', marginLeft: 'auto' }}>
                {a.created_at.slice(0, 10)}
              </span>
            </div>

            <div style={{ display: 'flex', gap: '8px' }}>
              <Button
                variant="secondary" size="sm" onClick={() => void openArchive(a.id)}
                icon={<Eye size={14} />} style={{ flex: 1 }}
              >
                查看
              </Button>
              <Button
                variant="secondary" size="sm" onClick={() => editArchive(a.id)}
                icon={<Pencil size={14} />} title="编辑这份归档"
              />
              <Button
                variant="danger" size="sm" onClick={() => setArchiveDeleteId(a.id)}
                icon={<Trash2 size={14} />}
              />
            </div>
          </div>
        ))}
      </div>
    );
  };

  const fileGrid = () => {
    if (loading) {
      return <SkeletonCardGrid count={6} />;
    }

    if (filteredMaterials.length === 0) {
      return (
        <div style={{ padding: '80px 20px', display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '12px', textAlign: 'center' }}>
          <div style={{ width: 56, height: 56, borderRadius: 'var(--radius-lg)', background: 'var(--brand)', color: '#ffffff', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
            <FileText size={28} />
          </div>
          <h3 style={{ fontSize: '1.125rem', fontWeight: 600 }}>暂无资料</h3>
          <p style={{ fontSize: '0.875rem', color: 'var(--text-secondary)' }}>点击「上传资料」按钮添加文件</p>
        </div>
      );
    }

    return (
      <div className="bento bento-3">
        {filteredMaterials.map((m) => (
          // 卡内文件卡降级为 sub-section：二级视图容器卡已是唯一一层玻璃
          <div
            key={m.id}
            className="sub-section"
            style={{ padding: '18px', display: 'flex', flexDirection: 'column', gap: '12px' }}
          >
            <div style={{ display: 'flex', alignItems: 'flex-start', gap: '12px' }}>
              <div style={{
                width: 44, height: 44, borderRadius: '12px',
                background: 'var(--accent-subtle)', color: 'var(--accent)',
                display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0,
              }}>
                <FileText size={22} />
              </div>
              <div style={{ minWidth: 0, flex: 1 }}>
                <h4 style={{
                  fontSize: '0.95rem', fontWeight: 600, margin: 0,
                  overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                }} title={m.logical_name}>
                  {m.logical_name}
                </h4>
                <p style={{ fontSize: '0.8rem', color: 'var(--text-tertiary)', marginTop: '4px' }}>
                  {m.latest_version ? formatFileSize(m.latest_version.size_bytes) : '-'} · v{m.latest_version?.version_no ?? '-'}
                </p>
              </div>
            </div>

            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginTop: 'auto' }}>
              <Badge variant={MATERIAL_TYPE_VARIANTS[m.material_type] || 'default'}>
                {MATERIAL_TYPE_LABELS[m.material_type] || m.material_type}
              </Badge>
              <Badge variant={isParsing(m) ? 'warning' : ((m.parse_status && PARSE_STATUS_VARIANTS[m.parse_status.status]) || 'default')}>
                {isParsing(m)
                  ? '解析中'
                  : (m.parse_status ? (PARSE_STATUS_LABELS[m.parse_status.status] || m.parse_status.status) : '未解析')}
              </Badge>
            </div>

            <div style={{ display: 'flex', gap: '8px', marginTop: '8px' }}>
              <Button
                variant="secondary"
                size="sm"
                onClick={() => handleParse(m)}
                disabled={isParsing(m)}
                loading={isParsing(m)}
                icon={<RefreshCw size={14} />}
                style={{ flex: 1 }}
              >
                {isParsing(m) ? '解析中…' : (isParsed(m) ? '重新解析' : '解析')}
              </Button>
              <Button
                variant="danger"
                size="sm"
                onClick={() => setDeleteId(m.id)}
                icon={<Trash2 size={14} />}
              />
            </div>
          </div>
        ))}
      </div>
    );
  };

  return (
    <div className="page-enter page-stack">
      {/* Header（「试卷」文件夹不上传材料，藏掉上传入口） */}
      <div className="page-header">
        <div>
          <h1 className="page-title">资料库</h1>
          <p className="page-subtitle">按文件夹管理课程大纲与教学资料</p>
        </div>
        {activeFolder !== 'papers' && (
          <Button onClick={openUpload} icon={<Upload size={16} />}>
            上传资料
          </Button>
        )}
      </div>

      {/* ── 根视图：三个一级文件夹（bento 大格） ── */}
      {activeFolder === null && (
        <div className="bento bento-3">
          {FOLDER_GROUPS.map((f) => {
            const Icon = f.icon;
            // 「试卷」的计数来自归档接口，不走 material_type 统计
            const count = f.key === 'papers'
              ? archives.length
              : f.subFolders.reduce((sum, s) => sum + countByType(s.key), 0);
            return (
              <button
                key={f.key}
                onClick={() => { setActiveFolder(f.key); setActiveSubFolder(null); }}
                className="glass-card card-hover"
                style={{
                  padding: '24px', textAlign: 'left',
                  cursor: 'pointer', display: 'flex', alignItems: 'center', gap: '18px',
                }}
              >
                <div className="icon-box" style={{
                  width: 56, height: 56, borderRadius: 16,
                  display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0,
                }}>
                  <Icon size={28} />
                </div>
                <div style={{ minWidth: 0 }}>
                  <h3 style={{ fontSize: '1.05rem', fontWeight: 600, marginBottom: '4px' }}>{f.name}</h3>
                  <p style={{ fontSize: '0.85rem', color: 'var(--text-secondary)' }}>{f.description}</p>
                  <p style={{ fontSize: '0.75rem', color: 'var(--text-tertiary)', marginTop: '6px' }}>
                    {count} {f.key === 'papers' ? '份试卷' : '份文件'}
                  </p>
                </div>
              </button>
            );
          })}
        </div>
      )}

      {/* ── 「试卷」文件夹：归档列表（无子分区，进文件夹即列卷） ── */}
      {activeFolder === 'papers' && (
        <div className="glass-card" style={{ padding: '20px', display: 'flex', flexDirection: 'column', gap: '18px' }}>
          <div className="row">
            <Button variant="secondary" size="sm" onClick={() => setActiveFolder(null)}>
              返回文件夹
            </Button>
            <span style={{ color: 'var(--text-tertiary)' }}>/</span>
            <h2 style={{ fontSize: '1.15rem', fontWeight: 600 }}>试卷</h2>
            <span style={{ fontSize: '0.85rem', color: 'var(--text-tertiary)' }}>
              ({archives.length} 份)
            </span>
          </div>
          {archiveGrid()}
        </div>
      )}

      {/* ── 一级视图：子文件夹（「试卷」无子分区，走上一支） ── */}
      {activeFolder !== null && activeFolder !== 'papers' && activeSubFolder === null && currentFolder && (
        <div className="glass-card" style={{ padding: '20px', display: 'flex', flexDirection: 'column', gap: '18px' }}>
          <div className="row">
            <Button variant="secondary" size="sm" onClick={() => setActiveFolder(null)}>
              返回文件夹
            </Button>
            <span style={{ color: 'var(--text-tertiary)' }}>/</span>
            <h2 style={{ fontSize: '1.15rem', fontWeight: 600 }}>{currentFolder.name}</h2>
          </div>
          <div className="bento bento-2">
            {currentFolder.subFolders.map((s) => {
              const Icon = s.icon;
              return (
                // 容器卡内的可点分区：sub-section 降级 + is-clickable 保留点击反馈
                <button
                  key={s.key}
                  onClick={() => setActiveSubFolder(s.key)}
                  className="sub-section is-clickable"
                  style={{
                    padding: '20px', textAlign: 'left',
                    display: 'flex', alignItems: 'center', gap: '14px',
                  }}
                >
                  <div style={{
                    width: 48, height: 48, borderRadius: '14px', background: 'color-mix(in srgb, ' + s.color + ' 10%, transparent)', color: s.color,
                    display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0,
                  }}>
                    <Icon size={24} />
                  </div>
                  <div style={{ minWidth: 0 }}>
                    <h4 style={{ fontSize: '0.95rem', fontWeight: 600, marginBottom: '2px' }}>{s.name}</h4>
                    <p style={{ fontSize: '0.78rem', color: 'var(--text-secondary)' }}>{s.description}</p>
                    <p style={{ fontSize: '0.72rem', color: 'var(--text-tertiary)', marginTop: '4px' }}>{countByType(s.key)} 份文件</p>
                  </div>
                </button>
              );
            })}
          </div>
        </div>
      )}

      {/* ── 二级视图：文件列表 ── */}
      {activeSubFolder !== null && currentSubFolder && (
        <div className="glass-card" style={{ padding: '20px', display: 'flex', flexDirection: 'column', gap: '18px' }}>
          <div className="row">
            <Button variant="secondary" size="sm" onClick={() => setActiveSubFolder(null)}>
              返回上级
            </Button>
            <span style={{ color: 'var(--text-tertiary)' }}>/</span>
            <span style={{ color: 'var(--text-tertiary)' }}>{currentFolder?.name}</span>
            <span style={{ color: 'var(--text-tertiary)' }}>/</span>
            <h2 style={{ fontSize: '1.15rem', fontWeight: 600 }}>{currentSubFolder.name}</h2>
            <span style={{ fontSize: '0.85rem', color: 'var(--text-tertiary)' }}>
              ({filteredMaterials.length} 份)
            </span>
            <div style={{ marginLeft: 'auto', display: 'flex', gap: '8px' }}>
              <Button
                variant="secondary"
                size="sm"
                onClick={handleParseAll}
                icon={<RefreshCw size={14} />}
              >
                一键解析
              </Button>
            </div>
          </div>
          {fileGrid()}
        </div>
      )}

      {/* ── 多文件上传弹窗 ── */}
      <Modal
        open={uploadOpen}
        onClose={() => { if (!uploading) setUploadOpen(false); }}
        title="上传资料"
        onConfirm={handleUpload}
        confirmLabel={uploadItems.length > 0 ? `确认上传（${uploadItems.length} 份）` : '确认上传'}
        loading={uploading}
      >
        <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
            <label style={{ fontSize: '0.8125rem', fontWeight: 500, color: 'var(--text-secondary)' }}>
              选择文件（可多选）
            </label>
            <input
              ref={fileInputRef}
              type="file"
              multiple
              onChange={(e) => handleFilesChange(e.target.files)}
              className="input-field"
            />
            <p style={{ fontSize: '0.75rem', color: 'var(--text-tertiary)', marginTop: '4px' }}>
              支持同时选择多个文件，每个文件可单独指定类型
            </p>
          </div>

          {uploadItems.length > 0 && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '8px', maxHeight: '280px', overflowY: 'auto' }}>
              {uploadItems.map((item, idx) => (
                <div
                  key={idx}
                  style={{
                    display: 'flex', alignItems: 'center', gap: '10px', padding: '10px 12px',
                    background: 'var(--surface-elevated)', borderRadius: '12px', border: '1px solid var(--line)',
                  }}
                >
                  <FileText size={18} style={{ color: 'var(--text-tertiary)', flexShrink: 0 }} />
                  <div style={{ minWidth: 0, flex: 1 }}>
                    <p style={{ fontSize: '0.8125rem', fontWeight: 500, margin: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {item.file.name}
                    </p>
                    <p style={{ fontSize: '0.72rem', color: 'var(--text-tertiary)', margin: 0 }}>
                      {formatFileSize(item.file.size)}
                    </p>
                  </div>
                  <select
                    value={item.type}
                    onChange={(e) => updateItemType(idx, e.target.value)}
                    className="input-field"
                    style={{ width: 'auto', padding: '4px 8px', fontSize: '0.8125rem' }}
                  >
                    {ALL_TYPE_OPTIONS.map((o) => (
                      <option key={o.value} value={o.value}>{o.label}</option>
                    ))}
                  </select>
                  <button
                    onClick={() => removeItem(idx)}
                    style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text-tertiary)', padding: 4, display: 'flex' }}
                    title="移除"
                  >
                    <X size={16} />
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>
      </Modal>

      {/* ── 删除确认 ── */}
      <Modal
        open={!!deleteId}
        onClose={() => setDeleteId(null)}
        title="确认删除"
        onConfirm={handleDelete}
        confirmLabel="删除"
        loading={deleting}
        danger
      >
        <p style={{ fontSize: '0.875rem', color: 'var(--text-secondary)' }}>确定要删除这份资料吗？此操作不可撤销。</p>
      </Modal>

      {/* ── 归档试卷详情：预览快照 + 编辑 / 存回 / 四件套导出 ── */}
      <Modal
        open={!!archiveDetail}
        onClose={() => { setArchiveDetail(null); setRestoreOpen(false); }}
        title={archiveDetail ? archiveDetail.name : '归档试卷'}
        maxWidth="760px"
        footer={
          <div style={{ display: 'flex', width: '100%', gap: '8px', flexWrap: 'wrap', alignItems: 'center' }}>
            <Button size="sm" onClick={() => archiveDetail && editArchive(archiveDetail.id)} icon={<Pencil size={14} />}>
              编辑
            </Button>
            <Button variant="secondary" size="sm" onClick={() => setRestoreOpen(true)} icon={<RotateCcw size={14} />}>
              存回试卷区
            </Button>
            <div style={{ marginLeft: 'auto', display: 'flex', gap: '6px', flexWrap: 'wrap' }}>
              <Button variant="secondary" size="sm" onClick={() => void exportArchive('student')}>学生卷</Button>
              <Button variant="secondary" size="sm" onClick={() => void exportArchive('answer')}>答卷</Button>
              <Button variant="secondary" size="sm" onClick={() => void exportArchive('card')}>答题卡</Button>
              <Button variant="secondary" size="sm" onClick={() => void exportArchive('json')}>细则 JSON</Button>
              <Button variant="secondary" size="sm" onClick={() => void exportArchiveBundle()} icon={<Package size={14} />}>
                打包
              </Button>
            </div>
          </div>
        }
      >
        {archiveDetail && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '14px' }}>
            <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap', fontSize: '0.8rem', color: 'var(--text-secondary)' }}>
              <span>来源项目：{archiveDetail.project_name ?? '未知'}</span>
              {archiveDetail.source_version_no != null && <span>· 源卷 v{archiveDetail.source_version_no}</span>}
              <span>· {archiveDetail.item_count} 题 / {archiveDetail.total_score} 分</span>
              <span>· 保存于 {archiveDetail.created_at.slice(0, 10)}</span>
            </div>

            <div style={{ display: 'flex', flexDirection: 'column', gap: '8px', maxHeight: '360px', overflowY: 'auto' }}>
              {archiveDetail.questions.map((q) => (
                <div
                  key={q.item_index}
                  style={{
                    padding: '10px 12px', background: 'var(--surface-elevated)',
                    border: '1px solid var(--line)', borderRadius: '12px',
                  }}
                >
                  <div style={{ display: 'flex', gap: '8px', alignItems: 'center', fontSize: '0.75rem', color: 'var(--text-tertiary)' }}>
                    <span>{q.item_index}.</span>
                    <Badge variant="default">{qlabel(q.question_type)}</Badge>
                    <span>{q.score} 分</span>
                  </div>
                  <p style={{
                    fontSize: '0.85rem', margin: '6px 0 0', color: 'var(--text-secondary)',
                    display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical',
                    overflow: 'hidden',
                  }}>
                    {q.stem || '（无题干）'}
                  </p>
                </div>
              ))}
            </div>

            <p style={{ fontSize: '0.75rem', color: 'var(--text-tertiary)' }}>
              归档是独立副本：即使源卷已被「只留最近 3 份」从库里删除，这里的内容仍完整保留。
            </p>
          </div>
        )}
      </Modal>

      {/* ── 存回试卷区确认：成为当前卷、走「只留 3 份」，必须先说清 ── */}
      <Modal
        open={restoreOpen}
        onClose={() => setRestoreOpen(false)}
        title="存回试卷区"
        maxWidth="560px"
        footer={
          <>
            <Button variant="secondary" onClick={() => setRestoreOpen(false)}>取消</Button>
            <Button loading={archiveBusy} onClick={() => void handleArchiveRestore()}>确认存回</Button>
          </>
        }
      >
        <p style={{ fontSize: '0.875rem', lineHeight: 1.8, color: 'var(--text-secondary)' }}>
          将按这份归档的快照新建一版试卷，并设为该项目的<strong>当前卷</strong>：
        </p>
        <ul style={{ fontSize: '0.85rem', lineHeight: 1.9, color: 'var(--text-secondary)', paddingLeft: '1.2em', marginTop: 8 }}>
          <li>版本号在现有基础上追加，项目状态回到「待审核」；</li>
          <li>与生成建卷一样走「每个项目只留最近 3 份」，更早的版本会被物理删除；</li>
          <li>归档本身不受影响，之后仍可继续编辑、导出或再次存回。</li>
        </ul>
      </Modal>

      {/* ── 归档删除确认 ── */}
      <Modal
        open={!!archiveDeleteId}
        onClose={() => setArchiveDeleteId(null)}
        title="确认删除"
        onConfirm={handleArchiveDelete}
        confirmLabel="删除"
        loading={archiveBusy}
        danger
      >
        <p style={{ fontSize: '0.875rem', color: 'var(--text-secondary)' }}>
          确定要删除这份归档试卷吗？此操作不可撤销（不影响原试卷）。
        </p>
      </Modal>
    </div>
  );
}
