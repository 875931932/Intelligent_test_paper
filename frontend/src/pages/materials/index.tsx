import { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import { useParams } from 'react-router-dom';
import {
  Upload, RefreshCw, Trash2, FileText, Loader2,
  Folder, FolderOpen, BookOpen, ClipboardCheck, BookMarked, X,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import { api } from '@/api/client';
import { useCourseStore } from '@/stores/course';
import { useToastStore } from '@/stores/toast';
import { Button, Modal, Badge, Spinner } from '@/components/ui';
import { computeSha256 } from '@/lib/sha256';
import type { MaterialResponse } from '@/types/api';

type FolderKey = 'syllabus' | 'materials';
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

const PARSE_STATUS_LABELS: Record<string, string> = {
  pending: '待解析',
  parsing: '解析中',
  completed: '已完成',
  failed: '失败',
};

const PARSE_STATUS_VARIANTS: Record<string, string> = {
  pending: 'default',
  parsing: 'warning',
  completed: 'success',
  failed: 'error',
};

const FOLDER_GROUPS: FolderMeta[] = [
  {
    key: 'syllabus',
    name: '课程大纲',
    description: '教学大纲与考核大纲',
    icon: Folder,
    subFolders: [
      { key: 'teaching_syllabus', name: '教学大纲', description: '课程教学目标与内容范围', icon: BookOpen, color: '#0071e3' },
      { key: 'assessment_syllabus', name: '考核大纲', description: '考核方式与评分标准', icon: ClipboardCheck, color: '#34c759' },
    ],
  },
  {
    key: 'materials',
    name: '课程资料',
    description: '教材与习题等教学资源',
    icon: FolderOpen,
    subFolders: [
      { key: 'teaching_material', name: '教材', description: '教学用书与讲义', icon: BookMarked, color: '#af52de' },
      { key: 'exercise', name: '习题', description: '练习与试卷', icon: FileText, color: '#ff9500' },
    ],
  },
];

const ALL_TYPE_OPTIONS = [
  { value: 'teaching_syllabus', label: '教学大纲' },
  { value: 'assessment_syllabus', label: '考核大纲' },
  { value: 'teaching_material', label: '教材' },
  { value: 'exercise', label: '习题' },
];

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

  const [materials, setMaterials] = useState<MaterialResponse[]>([]);
  const [loading, setLoading] = useState(true);

  const [activeFolder, setActiveFolder] = useState<FolderKey | null>(null);
  const [activeSubFolder, setActiveSubFolder] = useState<SubFolderKey | null>(null);

  const [uploadOpen, setUploadOpen] = useState(false);
  const [uploadItems, setUploadItems] = useState<UploadItem[]>([]);
  const [uploading, setUploading] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const [deleteId, setDeleteId] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);

  const [parsingIds, setParsingIds] = useState<Set<string>>(new Set());
  const pollingRef = useRef<Map<string, ReturnType<typeof setInterval>>>(new Map());

  const loadMaterials = useCallback(async () => {
    if (!courseId) return;
    try {
      setLoading(true);
      const data = await api.materials.list(courseId);
      setMaterials(Array.isArray(data) ? data : []);
    } catch {
      addToast('加载资料列表失败', 'error');
    } finally {
      setLoading(false);
    }
  }, [courseId, addToast]);

  useEffect(() => {
    loadMaterials();
    return () => {
      pollingRef.current.forEach((timer) => clearInterval(timer));
      pollingRef.current.clear();
    };
  }, [loadMaterials]);

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

  const defaultTypeForNewFile = (): string => activeSubFolder || 'teaching_material';

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
          await fetch(session.upload_url, {
            method: 'PUT',
            body: item.file,
            headers: {
              'Content-Type': item.file.type || 'application/octet-stream',
              'x-amz-meta-sha256': sha256,
              ...(session.headers || {}),
            },
          });
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
    } catch {
      addToast(
        successCount > 0 ? `部分上传失败（成功 ${successCount} 份）` : '上传失败，请重试',
        successCount > 0 ? 'info' : 'error'
      );
    } finally {
      setUploading(false);
    }
  };

  // ── 解析 ──
  const handleParse = async (material: MaterialResponse) => {
    const materialId = material.id;
    try {
      await api.materials.parse(courseId, materialId);
      setParsingIds((prev) => new Set(prev).add(materialId));
      addToast('开始解析，请稍候...', 'info');

      if (pollingRef.current.has(materialId)) {
        clearInterval(pollingRef.current.get(materialId)!);
      }

      const timer = setInterval(async () => {
        try {
          const updated = await api.materials.pollParse(courseId, materialId);
          const status = (updated as Record<string, unknown>)?.parse_status;
          const statusValue = (status as Record<string, unknown>)?.status as string | undefined;
          if (statusValue === 'completed' || statusValue === 'failed') {
            clearInterval(timer);
            pollingRef.current.delete(materialId);
            setParsingIds((prev) => {
              const next = new Set(prev);
              next.delete(materialId);
              return next;
            });
            addToast(statusValue === 'completed' ? '解析完成' : '解析失败', statusValue === 'completed' ? 'success' : 'error');
            loadMaterials();
          }
        } catch {
          clearInterval(timer);
          pollingRef.current.delete(materialId);
          setParsingIds((prev) => {
            const next = new Set(prev);
            next.delete(materialId);
            return next;
          });
        }
      }, 2000);

      pollingRef.current.set(materialId, timer);
    } catch {
      addToast('触发解析失败', 'error');
    }
  };

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

  const fileGrid = () => {
    if (loading) {
      return (
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', padding: '80px 0' }}>
          <Spinner size="lg" />
        </div>
      );
    }

    if (filteredMaterials.length === 0) {
      return (
        <div style={{ padding: '80px 20px', display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '12px', textAlign: 'center' }}>
          <div style={{ width: 56, height: 56, borderRadius: '18px', background: 'var(--accent-subtle)', color: 'var(--accent)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
            <FileText size={28} />
          </div>
          <h3 style={{ fontSize: '1.125rem', fontWeight: 600 }}>暂无资料</h3>
          <p style={{ fontSize: '0.875rem', color: 'var(--text-secondary)' }}>点击「上传资料」按钮添加文件</p>
        </div>
      );
    }

    return (
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(260px, 1fr))', gap: '16px' }}>
        {filteredMaterials.map((m) => (
          <div
            key={m.id}
            className="glass-card"
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
              <Badge variant={PARSE_STATUS_VARIANTS[m.parse_status.status] || 'default'}>
                {PARSE_STATUS_LABELS[m.parse_status.status] || m.parse_status.status}
              </Badge>
            </div>

            <div style={{ display: 'flex', gap: '8px', marginTop: '8px' }}>
              <Button
                variant="secondary"
                size="sm"
                onClick={() => handleParse(m)}
                disabled={parsingIds.has(m.id)}
                icon={parsingIds.has(m.id) ? <Loader2 size={14} /> : <RefreshCw size={14} />}
                style={{ flex: 1 }}
              >
                解析
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
    <div className="page-enter" style={{ display: 'flex', flexDirection: 'column', gap: '20px' }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: '16px', flexWrap: 'wrap' }}>
        <div>
          <h1 style={{ fontSize: '1.75rem', fontWeight: 700, letterSpacing: '-0.03em' }}>资料库</h1>
          <p style={{ fontSize: '0.9375rem', color: 'var(--text-secondary)', marginTop: '6px' }}>
            按文件夹管理课程大纲与教学资料
          </p>
        </div>
        <Button onClick={openUpload} icon={<Upload size={16} />}>
          上传资料
        </Button>
      </div>

      {/* ── 根视图：两个一级文件夹 ── */}
      {activeFolder === null && (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(300px, 1fr))', gap: '20px' }}>
          {FOLDER_GROUPS.map((f) => {
            const Icon = f.icon;
            const count = f.subFolders.reduce((sum, s) => sum + countByType(s.key), 0);
            return (
              <button
                key={f.key}
                onClick={() => { setActiveFolder(f.key); setActiveSubFolder(null); }}
                className="glass-card"
                style={{
                  padding: '24px', textAlign: 'left', background: 'none', border: '1px solid var(--glass-border)',
                  cursor: 'pointer', display: 'flex', alignItems: 'center', gap: '18px',
                }}
              >
                <div style={{
                  width: 56, height: 56, borderRadius: '16px',
                  background: 'var(--accent-subtle)', color: 'var(--accent)',
                  display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0,
                }}>
                  <Icon size={28} />
                </div>
                <div style={{ minWidth: 0 }}>
                  <h3 style={{ fontSize: '1.05rem', fontWeight: 600, marginBottom: '4px' }}>{f.name}</h3>
                  <p style={{ fontSize: '0.85rem', color: 'var(--text-secondary)' }}>{f.description}</p>
                  <p style={{ fontSize: '0.75rem', color: 'var(--text-tertiary)', marginTop: '6px' }}>{count} 份文件</p>
                </div>
              </button>
            );
          })}
        </div>
      )}

      {/* ── 一级视图：子文件夹 ── */}
      {activeFolder !== null && activeSubFolder === null && currentFolder && (
        <div className="glass-card" style={{ padding: '20px', display: 'flex', flexDirection: 'column', gap: '18px' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
            <Button variant="secondary" size="sm" onClick={() => setActiveFolder(null)}>
              返回文件夹
            </Button>
            <span style={{ color: 'var(--text-tertiary)' }}>/</span>
            <h2 style={{ fontSize: '1.15rem', fontWeight: 600 }}>{currentFolder.name}</h2>
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(260px, 1fr))', gap: '16px' }}>
            {currentFolder.subFolders.map((s) => {
              const Icon = s.icon;
              return (
                <button
                  key={s.key}
                  onClick={() => setActiveSubFolder(s.key)}
                  className="glass-card"
                  style={{
                    padding: '20px', textAlign: 'left', background: 'none', border: '1px solid var(--glass-border)',
                    cursor: 'pointer', display: 'flex', alignItems: 'center', gap: '14px',
                  }}
                >
                  <div style={{
                    width: 48, height: 48, borderRadius: '14px', background: s.color + '1a', color: s.color,
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
          <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
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
                    background: 'var(--surface-elevated)', borderRadius: '10px', border: '1px solid var(--glass-border)',
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
    </div>
  );
}
