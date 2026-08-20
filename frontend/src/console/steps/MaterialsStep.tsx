/** 步骤一 · 课程资料：直传上传 → MinerU 解析 → 就绪供后续链路使用。 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { materialsApi } from '../client'
import { MATERIAL_TYPE_LABELS, formatBytes } from '../types'
import type { Material, MaterialType, ParseStatus } from '../types'
import { Button, Card, EmptyState, Field, Notice, Pill } from '../ui'
import { InlineProgress } from '../ProgressFeedback'

const TERMINAL = new Set(['ready', 'failed'])

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

export function MaterialsStep({ courseId, materials, onRefresh }: {
  courseId: string
  materials: Material[]
  onRefresh: () => Promise<void>
}) {
  const [file, setFile] = useState<File | null>(null)
  const [materialType, setMaterialType] = useState<MaterialType>('teaching_material')
  const [uploading, setUploading] = useState(false)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [error, setError] = useState('')
  const [info, setInfo] = useState('')
  const fileInput = useRef<HTMLInputElement>(null)

  const active = materials.filter(
    (m) => m.parse_status != null && !TERMINAL.has(m.parse_status.status),
  )
  const readyCount = materials.filter((m) => m.parse_status?.status === 'ready').length
  const failedCount = materials.filter((m) => m.parse_status?.status === 'failed').length
  const parsePercent = materials.length > 0 ? Math.round((readyCount / materials.length) * 100) : 0

  // 解析轮询：存在进行中的解析时每 3 秒推进一次状态机
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

  const upload = useCallback(async () => {
    if (file == null) {
      setError('请先选择文件')
      return
    }
    setUploading(true)
    setError('')
    setInfo('')
    try {
      await materialsApi.upload(courseId, file, materialType)
      setFile(null)
      if (fileInput.current) fileInput.current.value = ''
      setInfo(`「${file.name}」上传完成，请在列表中点击「解析」启动 MinerU 解析。`)
      await onRefresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : '上传失败')
    } finally {
      setUploading(false)
    }
  }, [courseId, file, materialType, onRefresh])

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

  const startAllParse = useCallback(async () => {
    const pending = materials.filter(
      (m) => m.latest_version != null && !TERMINAL.has(m.parse_status?.status ?? 'pending'),
    )
    if (pending.length === 0) return
    setBusyId('__batch__')
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
    setInfo(`批量解析已提交：${ok} 份成功${fail > 0 ? `，${fail} 份失败` : ''}，正在轮询进度…`)
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

  return (
    <>
      <Card title="上传资料" sub="支持教学大纲、考核大纲、教学材料与习题资料；同名文件将归档为新版本。">
        <div className="form-row">
          <Field label="文件">
            <input
              ref={fileInput}
              className="input"
              type="file"
              accept=".pdf,.doc,.docx,.ppt,.pptx,.xls,.xlsx,.txt,.md,.jpg,.jpeg,.png,.gif,.webp,.bmp"
              onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            />
          </Field>
          <Field label="资料类型">
            <select className="select" value={materialType} onChange={(e) => setMaterialType(e.target.value as MaterialType)}>
              {Object.entries(MATERIAL_TYPE_LABELS).map(([value, label]) => (
                <option key={value} value={value}>{label}</option>
              ))}
            </select>
          </Field>
          <Button variant="primary" loading={uploading} onClick={() => void upload()} disabled={file == null}>
            上传
          </Button>
        </div>
      </Card>

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

      {materials.length > 0 && (
        <div style={{ marginBottom: 12 }}>
          <Button
            size="sm"
            variant="secondary"
            loading={busyId === '__batch__'}
            disabled={materials.every((m) => !m.latest_version || TERMINAL.has(m.parse_status?.status ?? 'pending'))}
            onClick={() => void startAllParse()}
          >
            全部解析
          </Button>
        </div>
      )}

      <div className="table-card">
        {materials.length === 0 ? (
          <EmptyState>尚无课程资料。上传教学大纲与考核大纲后即可提取考纲框架。</EmptyState>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>资料</th>
                <th>类型</th>
                <th>版本</th>
                <th>大小</th>
                <th>解析状态</th>
                <th style={{ width: 190 }}></th>
              </tr>
            </thead>
            <tbody>
              {materials.map((material) => (
                <tr key={material.id}>
                  <td>
                    <div className="cell-title">{material.logical_name}</div>
                    {material.parse_status?.error_summary ? (
                      <div className="cell-sub">{material.parse_status.error_summary}</div>
                    ) : null}
                  </td>
                  <td>
                    <select
                      className="select select-sm"
                      value={material.material_type}
                      disabled={busyId === material.id}
                      onChange={async (e) => {
                        setBusyId(material.id)
                        try {
                          await materialsApi.updateType(courseId, material.id, e.target.value)
                          await onRefresh()
                        } catch {
                          setError('修改类型失败')
                        } finally {
                          setBusyId(null)
                        }
                      }}
                    >
                      {Object.entries(MATERIAL_TYPE_LABELS).map(([value, label]) => (
                        <option key={value} value={value}>{label}</option>
                      ))}
                    </select>
                  </td>
                  <td className="num">v{material.latest_version?.version_no ?? '—'}</td>
                  <td className="num">{material.latest_version ? formatBytes(material.latest_version.size_bytes) : '—'}</td>
                  <td>{parsePill(material.parse_status)}</td>
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
                    {' '}
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
    </>
  )
}
