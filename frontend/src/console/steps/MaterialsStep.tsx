/** 步骤一 · 课程资料：按固定资料区批量上传 / 文件夹上传 → 解析 → 就绪。 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { materialsApi } from '../client'
import { MATERIAL_TYPE_LABELS, formatBytes } from '../types'
import type { Material, MaterialType, ParseStatus } from '../types'
import { Button, Card, EmptyState, Notice, Pill } from '../ui'
import { InlineProgress } from '../ProgressFeedback'

const TERMINAL = new Set(['ready', 'failed'])
const ZONES: { key: MaterialType; label: string; description: string; accept: string }[] = [
  { key: 'teaching_syllabus', label: '教学大纲', description: '通常一个当前生效版本', accept: '.pdf,.doc,.docx,.ppt,.pptx,.txt,.md' },
  { key: 'assessment_syllabus', label: '考核大纲', description: '通常一个当前生效版本', accept: '.pdf,.doc,.docx,.ppt,.pptx,.txt,.md' },
  { key: 'teaching_material', label: '教学资料', description: '支持按章节/周次文件夹批量导入', accept: '.pdf,.doc,.docx,.ppt,.pptx,.xls,.xlsx,.txt,.md,.jpg,.jpeg,.png,.gif,.webp,.bmp' },
  { key: 'exercise', label: '习题资料', description: '练习、作业或历史试卷', accept: '.pdf,.doc,.docx,.ppt,.pptx,.txt,.md,.jpg,.jpeg,.png,.gif,.webp,.bmp' },
]

function fileExt(name: string) {
  const parts = name.split('.')
  return parts.length > 1 ? parts.pop()!.toUpperCase() : 'FILE'
}

function parsePill(parse: ParseStatus | null) {
  if (parse == null) return <Pill kind="neutral">未解析</Pill>
  switch (parse.status) {
    case 'ready':
      return <Pill kind="success" dot>解析就绪</Pill>
    case 'failed':
      return <Pill kind="danger" dot>解析失败</Pill>
    default:
      return <Pill kind="info" dot>解析中</Pill>
  }
}

function statusLabel(status: string | null) {
  if (status == null) return '待处理'
  const map: Record<string, string> = {
    staged: '暂存',
    organizing: '整理中',
    organizing_parsing: '解析中',
    organizing_indexing: '索引中',
    candidate: '候选',
    ready: '就绪',
    failed: '失败',
    blocked: '受限',
    superseded: '已替换',
  }
  return map[status] ?? status
}

function statusKind(status: string | null): 'neutral' | 'info' | 'warning' | 'success' | 'danger' {
  if (status == null) return 'neutral'
  if (status === 'ready') return 'success'
  if (status === 'failed' || status === 'blocked') return 'danger'
  if (status === 'superseded') return 'neutral'
  if (status.includes('organizing') || status.includes('parsing') || status.includes('indexing')) return 'info'
  if (status === 'candidate') return 'warning'
  return 'neutral'
}

export function MaterialsStep({ courseId, materials, onRefresh }: {
  courseId: string
  materials: Material[]
  onRefresh: () => Promise<void>
}) {
  const [uploadingZone, setUploadingZone] = useState<MaterialType | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [error, setError] = useState('')
  const [info, setInfo] = useState('')
  const [dragOverZone, setDragOverZone] = useState<MaterialType | null>(null)
  const [fileView, setFileView] = useState<Record<string, 'table' | 'compact'>>({})
  const zoneInputRefs = useRef<Record<string, HTMLInputElement | null>>({})

  const active = materials.filter((m) => m.parse_status != null && !TERMINAL.has(m.parse_status.status))
  const readyCount = materials.filter((m) => m.parse_status?.status === 'ready').length
  const failedCount = materials.filter((m) => m.parse_status?.status === 'failed').length
  const parsePercent = materials.length > 0 ? Math.round((readyCount / materials.length) * 100) : 0

  const refreshRef = useRef(onRefresh)
  refreshRef.current = onRefresh
  useEffect(() => {
    if (active.length === 0) return
    let stopped = false
    const timer = setInterval(async () => {
      for (const material of active) {
        if (stopped) break
        try {
          const result = await materialsApi.pollParse(courseId, material.id)
          if (TERMINAL.has(result.status)) {
            await refreshRef.current()
            if (result.status === 'failed') {
              setError(`「${material.logical_name}」解析失败：${result.error_summary ?? result.error_code ?? '未知错误'}`)
            }
          }
        } catch {
          // 单次轮询失败不打断整体节奏，下一轮重试
        }
      }
    }, 3000)
    return () => {
      stopped = true
      clearInterval(timer)
    }
  }, [courseId, active.length])

  const uploadFiles = useCallback(async (zoneKey: MaterialType, files: FileList | File[]) => {
    setUploadingZone(zoneKey)
    setError('')
    setInfo('')
    const fileArray = Array.from(files)
    let ok = 0
    let fail = 0
    for (const file of fileArray) {
      try {
        await materialsApi.upload(courseId, file, zoneKey)
        ok++
      } catch (err) {
        fail++
        console.error(err)
      }
    }
    setInfo(`「${MATERIAL_TYPE_LABELS[zoneKey]}」上传完成：${ok} 份成功${fail > 0 ? `，${fail} 份失败` : ''}。`)
    await onRefresh()
    setUploadingZone(null)
  }, [courseId, onRefresh])

  const handleFileChange = useCallback((zoneKey: MaterialType, files: FileList | null) => {
    if (files && files.length > 0) {
      void uploadFiles(zoneKey, files)
    }
  }, [uploadFiles])

  const handleDrop = useCallback((zoneKey: MaterialType, e: React.DragEvent) => {
    e.preventDefault()
    setDragOverZone(null)
    const files = e.dataTransfer.files
    if (files && files.length > 0) {
      void uploadFiles(zoneKey, files)
    }
  }, [uploadFiles])

  const startParse = useCallback(async (material: Material) => {
    setBusyId(material.id)
    setError('')
    setInfo('')
    try {
      const result = await materialsApi.startParse(courseId, material.id)
      setInfo(
        result.reused
          ? `「${material.logical_name}」命中同哈希解析结果，直接就绪。`
          : `「${material.logical_name}」已提交解析，正在轮询进度…`,
      )
      await onRefresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : '解析提交失败')
    } finally {
      setBusyId(null)
    }
  }, [courseId, onRefresh])

  const startAllParseByZone = useCallback(async (zoneKey: MaterialType) => {
    const pending = materials.filter(
      (m) => m.material_type === zoneKey && m.latest_version != null && !TERMINAL.has(m.parse_status?.status ?? 'pending'),
    )
    if (pending.length === 0) return
    setBusyId(`__batch-${zoneKey}__`)
    setError('')
    setInfo('')
    let ok = 0
    let fail = 0
    for (const m of pending) {
      try {
        await materialsApi.startParse(courseId, m.id)
        ok++
      } catch {
        fail++
      }
    }
    setInfo(`${MATERIAL_TYPE_LABELS[zoneKey]}批量解析：${ok} 份成功${fail > 0 ? `，${fail} 份失败` : ''}，正在轮询进度…`)
    await onRefresh()
    setBusyId(null)
  }, [courseId, materials, onRefresh])

  const remove = useCallback(async (material: Material) => {
    if (!window.confirm(`确认删除资料「${material.logical_name}」？该操作不可恢复。`)) return
    setBusyId(material.id)
    setError('')
    try {
      await materialsApi.remove(courseId, material.id)
      await onRefresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : '删除失败')
    } finally {
      setBusyId(null)
    }
  }, [courseId, onRefresh])

  const updateType = useCallback(async (material: Material, materialType: MaterialType) => {
    setBusyId(material.id)
    setError('')
    try {
      await materialsApi.updateType(courseId, material.id, materialType)
      await onRefresh()
    } catch {
      setError('修改类型失败')
    } finally {
      setBusyId(null)
    }
  }, [courseId, onRefresh])

  const zoneStats = (zoneKey: MaterialType) => {
    const zoneMaterials = materials.filter((m) => m.material_type === zoneKey)
    const readyCount = zoneMaterials.filter((m) => m.parse_status?.status === 'ready').length
    const failedCount = zoneMaterials.filter((m) => m.parse_status?.status === 'failed').length
    const activeCount = zoneMaterials.filter((m) => m.parse_status != null && !TERMINAL.has(m.parse_status.status)).length
    return { total: zoneMaterials.length, ready: readyCount, failed: failedCount, active: activeCount }
  }

  return (
    <div className="material-zones">
      <div className="material-zones-head">
        <div>
          <h3>课程资料库</h3>
          <p className="material-zones-sub">按固定资料区分区上传；支持多文件与文件夹导入，文件夹名会作为章节/周次弱标签保留。</p>
        </div>
        <div className="material-zone-summary">
          <Pill kind={readyCount === materials.length && materials.length > 0 ? 'success' : 'neutral'}>
            {readyCount}/{materials.length} 已就绪
          </Pill>
        </div>
      </div>

      {error ? <Notice kind="error">{error}</Notice> : null}
      {info ? <Notice kind="info">{info}</Notice> : null}
      {active.length > 0 ? (
        <InlineProgress label="资料整理" percent={parsePercent} message={`${readyCount}/${materials.length} 份资料已解析，${active.length} 份处理中`} />
      ) : null}
      {materials.length > 0 && active.length === 0 && readyCount < materials.length ? (
        <InlineProgress label="资料整理" percent={parsePercent} status={failedCount > 0 ? 'warning' : 'idle'} message={`${readyCount}/${materials.length} 份资料已解析${failedCount > 0 ? `，${failedCount} 份失败待重试` : ''}`} />
      ) : null}
      {materials.some((m) => m.parse_status?.status === 'ready') && active.length === 0 ? (
        <Notice kind="success">
          所有资料解析就绪，请进入下一步「考纲框架」提取考核锚点与考点。
        </Notice>
      ) : null}

      <div className="material-zones-grid">
        {ZONES.map((zone) => {
          const stats = zoneStats(zone.key)
          const zoneMaterials = materials.filter((m) => m.material_type === zone.key)
          return (
            <div key={zone.key} className="material-zone">
              <Card title={zone.label} sub={zone.description}>
                <div
                  className={`zone-drop${dragOverZone === zone.key ? ' is-dragover' : ''}`}
                  onDragOver={(e) => { e.preventDefault(); setDragOverZone(zone.key) }}
                  onDragLeave={() => setDragOverZone(null)}
                  onDrop={(e) => handleDrop(zone.key, e)}
                >
                <input
                  ref={(el) => { zoneInputRefs.current[zone.key] = el }}
                  type="file"
                  multiple
                  accept={zone.accept}
                  onChange={(e) => handleFileChange(zone.key, e.target.files)}
                  style={{ display: 'none' }}
                />
                <div className="zone-drop-content">
                  <div className="zone-drop-icon">+</div>
                  <div className="zone-drop-text">
                    <strong>点击上传或拖入文件夹</strong>
                    <span>支持批量文件；{zone.accept.split(',').slice(0, 3).join(', ')} 等</span>
                  </div>
                </div>
                <div className="zone-drop-actions">
                  <Button size="sm" variant="secondary" onClick={() => zoneInputRefs.current[zone.key]?.click()}>
                    选择文件
                  </Button>
                  {(zone.key === 'teaching_material' || zone.key === 'exercise') && (
                    <Button size="sm" variant="ghost" onClick={() => {
                      const input = zoneInputRefs.current[zone.key]
                      if (!input) return
                      input.removeAttribute('multiple')
                      input.setAttribute('webkitdirectory', '')
                      input.setAttribute('directory', '')
                      input.click()
                      input.removeAttribute('webkitdirectory')
                      input.removeAttribute('directory')
                      input.setAttribute('multiple', '')
                    }}>
                      选择文件夹
                    </Button>
                  )}
                </div>
              </div>
              </Card>

              {uploadingZone === zone.key && (
                <div className="zone-uploading-bar">
                  <div className="zone-uploading-line" />
                  <span>正在上传到{zone.label}…</span>
                </div>
              )}

              {zoneMaterials.length > 0 && (
                <div className="material-list">
                  <div className="material-list-head">
                    <span>{zoneMaterials.length} 个文件</span>
                    <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                      {(zone.key === 'teaching_material' || zone.key === 'exercise') && (
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() => setFileView((v) => ({ ...v, [zone.key]: v[zone.key] === 'compact' ? 'table' : 'compact' }))}
                        >
                          {fileView[zone.key] === 'compact' ? '表格视图' : '文件夹视图'}
                        </Button>
                      )}
                      <Button
                        size="sm"
                        variant="primary"
                        loading={busyId === `__batch-${zone.key}__`}
                        disabled={zoneMaterials.every((m) => !m.latest_version || TERMINAL.has(m.parse_status?.status ?? 'pending'))}
                        onClick={() => void startAllParseByZone(zone.key)}
                      >
                        批量解析
                      </Button>
                    </div>
                  </div>

                  {(fileView[zone.key] === 'compact' && zone.key === 'teaching_material') || (fileView[zone.key] === 'compact' && zone.key === 'exercise') ? (
                    <div className="file-browser">
                      <div className="file-browser-head">
                        <span>文件名</span>
                        <div className="file-browser-meta">
                          <span>大小</span>
                          <span>状态</span>
                        </div>
                      </div>
                      {zoneMaterials.map((material) => {
                        const name = material.logical_name
                        const segments = name.includes('/') ? name.split('/') : name.includes('\\') ? name.split('\\') : [name]
                        const fileName = segments.pop()!
                        const folderPath = segments.join('/')
                        return (
                          <div key={material.id} className="file-browser-row">
                            <div className="file-icon">{fileExt(fileName)}</div>
                            <div className="file-browser-info">
                              {folderPath ? <div className="file-browser-path">{folderPath}/</div> : null}
                              <div className="file-browser-name">{fileName}</div>
                            </div>
                            <div className="file-browser-meta">
                              <span>{material.latest_version ? formatBytes(material.latest_version.size_bytes) : '—'}</span>
                              {material.parse_status?.status === 'ready' ? (
                                <Pill kind="success">可用</Pill>
                              ) : (
                                <Button
                                  size="sm"
                                  variant="primary"
                                  loading={busyId === material.id}
                                  disabled={material.latest_version == null || (material.parse_status != null && !TERMINAL.has(material.parse_status.status))}
                                  onClick={() => void startParse(material)}
                                >
                                  {material.parse_status?.status === 'failed' ? '重试解析' : '解析'}
                                </Button>
                              )}
                            </div>
                            <div className="file-browser-actions">
                              <select
                                className="select select-sm"
                                value={material.material_type}
                                disabled={busyId === material.id}
                                onChange={(e) => updateType(material, e.target.value as MaterialType)}
                              >
                                {Object.entries(MATERIAL_TYPE_LABELS).map(([value, label]) => (
                                  <option key={value} value={value}>{label}</option>
                                ))}
                              </select>
                              <Button size="sm" variant="danger-ghost" loading={busyId === material.id} onClick={() => void remove(material)}>
                                删除
                              </Button>
                            </div>
                          </div>
                        )
                      })}
                    </div>
                  ) : (
                    <table className="table">
                      <thead>
                        <tr>
                          <th>资料</th>
                          <th>大小</th>
                          <th>版本</th>
                          <th>状态</th>
                          <th>操作</th>
                        </tr>
                      </thead>
                      <tbody>
                        {zoneMaterials.map((material) => (
                          <tr key={material.id} className="material-item">
                            <td>
                              <div className="cell-title">{material.logical_name}</div>
                              {material.parse_status?.error_summary ? (
                                <div className="cell-sub">{material.parse_status.error_summary}</div>
                              ) : null}
                            </td>
                            <td className="num">{material.latest_version ? formatBytes(material.latest_version.size_bytes) : '—'}</td>
                            <td className="num">v{material.latest_version?.version_no ?? '—'}</td>
                            <td>
                              <Pill kind={statusKind(material.status)}>{statusLabel(material.status)}</Pill>
                              {parsePill(material.parse_status)}
                            </td>
                            <td style={{ textAlign: 'right' }}>
                              {material.parse_status?.status === 'ready' ? (
                                <Pill kind="success">可用</Pill>
                              ) : (
                                <Button
                                  size="sm"
                                  variant="primary"
                                  loading={busyId === material.id}
                                  disabled={material.latest_version == null || (material.parse_status != null && !TERMINAL.has(material.parse_status.status))}
                                  onClick={() => void startParse(material)}
                                >
                                  {material.parse_status?.status === 'failed' ? '重试解析' : '解析'}
                                </Button>
                              )}
                              <span style={{ display: 'inline-block', width: 8 }} />
                              <select
                                className="select select-sm"
                                value={material.material_type}
                                disabled={busyId === material.id}
                                onChange={(e) => updateType(material, e.target.value as MaterialType)}
                                style={{ marginRight: 6 }}
                              >
                                {Object.entries(MATERIAL_TYPE_LABELS).map(([value, label]) => (
                                  <option key={value} value={value}>{label}</option>
                                ))}
                              </select>
                              <Button size="sm" variant="danger-ghost" loading={busyId === material.id} onClick={() => void remove(material)}>
                                删除
                              </Button>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  )}
                </div>
              )}
            </div>
          )
        })}
      </div>

      {materials.length === 0 && (
        <div className="material-empty">
          <EmptyState>尚无课程资料。请按资料区上传教学大纲与考核大纲，或直接拖入教学资料文件夹。</EmptyState>
        </div>
      )}
    </div>
  )
}
