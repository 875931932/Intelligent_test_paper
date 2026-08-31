import { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import { useParams } from 'react-router-dom';
import {
  Upload, RefreshCw, Trash2, FileText, Loader2,
  Folder, FolderOpen,
} from 'lucide-react';
import { api } from '@/api/client';
import { useCourseStore } from '@/stores/course';
import { useToastStore } from '@/stores/toast';
import { Button, Modal, Input, Select, Badge, Spinner } from '@/components/ui';
import { computeSha256 } from '@/lib/sha256';
import type { MaterialResponse } from '@/types/api';

type FolderKey = 'syllabus' | 'materials' | 'all';

interface FolderMeta {
  key: FolderKey;
  name: string;
  description: string;
  icon: typeof Folder;
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

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

function isSyllabus(type: string) {
  return type === 'teaching_syllabus' || type === 'assessment_syllabus';
}

function fileIcon(_type: string) {
  return FileText;
}

export default function MaterialsPage() {
  const { courseId: routeCourseId } = useParams<{ courseId: string }>();
  const { activeCourseId } = useCourseStore();
  const courseId = routeCourseId || activeCourseId || '';
  const { addToast } = useToastStore();

  const [materials, setMaterials] = useState<MaterialResponse[]>([]);
  const [loading, setLoading] = useState(true);

  const [activeFolder, setActiveFolder] = useState<FolderKey | null>(null);

  const [uploadOpen, setUploadOpen] = useState(false);
  const [uploadFilename, setUploadFilename] = useState('');
  const [uploadType, setUploadType] = useState('teaching_material');
  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);

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

  const folders: FolderMeta[] = useMemo(
    () => [
      {
        key: 'syllabus',
        name: '课程大纲',
        description: '教学大纲与考核大纲',
        icon: Folder,
      },
      {
        key: 'materials',
        name: '课程资料',
        description: '教材、习题与参考资料',
        icon: FolderOpen,
      },
    ],
    []
  );

  const filteredMaterials = useMemo(() => {
    if (activeFolder === 'syllabus') return materials.filter((m) => isSyllabus(m.material_type));
    if (activeFolder === 'materials') return materials.filter((m) => !isSyllabus(m.material_type));
    return materials;
  }, [materials, activeFolder]);

  const handleUpload = async () => {
    if (!courseId || !uploadFile || !uploadFilename.trim()) {
      addToast('请填写完整信息', 'error');
      return;
    }
    try {
      setUploading(true);
      const sha256 = await computeSha256(uploadFile);
      const session = await api.materials.createUploadSession(courseId, {
        filename: uploadFilename,
        material_type: uploadType,
        size_bytes: uploadFile.size,
        sha256,
        mime_type: uploadFile.type || 'application/octet-stream',
      });

      if (!session?.upload_url) {
        throw new Error('未获取到上传地址');
      }

      await fetch(session.upload_url, {
        method: 'PUT',
        body: uploadFile,
        headers: { 'Content-Type': uploadFile.type || 'application/octet-stream' },
      });

      await api.materials.completeUpload(courseId, session.session_id);

      addToast('资料上传成功', 'success');
      setUploadOpen(false);
      setUploadFilename('');
      setUploadFile(null);
      setUploadType('teaching_material');
      loadMaterials();
    } catch {
      addToast('上传失败，请重试', 'error');
    } finally {
      setUploading(false);
    }
  };

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
      setDeleteId(null);
      loadMaterials();
    } catch {
      addToast('删除失败', 'error');
    } finally {
      setDeleting(false);
    }
  };

  const syllabusCount = useMemo(() => materials.filter((m) => isSyllabus(m.material_type)).length, [materials]);
  const materialCount = useMemo(() => materials.filter((m) => !isSyllabus(m.material_type)).length, [materials]);

  const folderContent = () => {
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
        {filteredMaterials.map((m) => {
          const Icon = fileIcon(m.material_type);
          return (
            <div
              key={m.id}
              className="glass-card"
              style={{ padding: '18px', display: 'flex', flexDirection: 'column', gap: '12px', position: 'relative' }}
            >
              <div style={{ display: 'flex', alignItems: 'flex-start', gap: '12px' }}>
                <div style={{
                  width: 44, height: 44, borderRadius: '12px',
                  background: 'var(--accent-subtle)', color: 'var(--accent)',
                  display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0,
                }}>
                  <Icon size={22} />
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
          );
        })}
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
        <Button onClick={() => setUploadOpen(true)} icon={<Upload size={16} />}>
          上传资料
        </Button>
      </div>

      {/* Folder grid / active folder content */}
      {activeFolder === null ? (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))', gap: '20px' }}>
          {folders.map((f) => {
            const Icon = f.icon;
            const count = f.key === 'syllabus' ? syllabusCount : materialCount;
            return (
              <button
                key={f.key}
                onClick={() => setActiveFolder(f.key)}
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
      ) : (
        <div className="glass-card" style={{ padding: '20px', display: 'flex', flexDirection: 'column', gap: '18px' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
            <Button variant="secondary" size="sm" onClick={() => setActiveFolder(null)}>
              返回文件夹
            </Button>
            <span style={{ color: 'var(--text-tertiary)' }}>/</span>
            <h2 style={{ fontSize: '1.15rem', fontWeight: 600 }}>
              {folders.find((f) => f.key === activeFolder)?.name}
            </h2>
            <span style={{ fontSize: '0.85rem', color: 'var(--text-tertiary)' }}>
              ({filteredMaterials.length} 份)
            </span>
          </div>
          {folderContent()}
        </div>
      )}

      {/* Upload Dialog */}
      <Modal
        open={uploadOpen}
        onClose={() => setUploadOpen(false)}
        title="上传资料"
        onConfirm={handleUpload}
        confirmLabel="确认上传"
        loading={uploading}
      >
        <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
          <Input
            label="文件名"
            placeholder="请输入文件名"
            value={uploadFilename}
            onChange={(e) => setUploadFilename(e.target.value)}
          />
          <Select
            label="资料类型"
            value={uploadType}
            onChange={(e) => setUploadType(e.target.value)}
            options={[
              { value: 'teaching_syllabus', label: '教学大纲' },
              { value: 'assessment_syllabus', label: '考核大纲' },
              { value: 'teaching_material', label: '教材' },
              { value: 'exercise', label: '习题' },
            ]}
          />
          <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
            <label style={{ fontSize: '0.8125rem', fontWeight: 500, color: 'var(--text-secondary)' }}>选择文件</label>
            <input
              type="file"
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) {
                  setUploadFile(f);
                  if (!uploadFilename) setUploadFilename(f.name);
                }
              }}
              className="input-field"
            />
            {uploadFile && (
              <p style={{ fontSize: '0.75rem', color: 'var(--text-tertiary)', marginTop: '4px' }}>已选择: {uploadFile.name}</p>
            )}
          </div>
        </div>
      </Modal>

      {/* Delete Confirm Dialog */}
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
