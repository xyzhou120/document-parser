import { useRef, useState } from 'react'
import { Alert, Button, Card, Checkbox, Descriptions, Empty, Form, Input, InputNumber, Select, Space, Table, Tag, Typography, message } from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import { Link, useSearchParams } from 'react-router-dom'
import axios from 'axios'
import { apiError } from '@/services/api'
import { createRequestId } from '@/utils/requestId'
import { getDocumentFile } from '@/services/parse'
import {
  createSourceRevision, extractSourceRevision, getArtifactHistory, getArtifactPage,
  previewSourceRevision, retrySourceExtraction,
  type ArtifactHistory, type ArtifactPage, type SourcePreview, type SourceRevision,
  type SourceRevisionPayload, type SourceRun,
} from '@/services/artifacts'

const typeLabels = { CORRECT: '订正文本', CONFIRM: '确认区域', UNREADABLE: '标记不可读' }
const bboxKeys = ['x0', 'y0', 'x1', 'y1'] as const
const codepoints = (text: string) => Array.from(text)

export default function SourceArtifacts() {
  const [searchParams] = useSearchParams()
  const [documentId, setDocumentId] = useState(searchParams.get('documentId') || '')
  const [loadedDocumentId, setLoadedDocumentId] = useState('')
  const [history, setHistory] = useState<ArtifactHistory>()
  const [artifactId, setArtifactId] = useState('')
  const [pageNumber, setPageNumber] = useState(1)
  const [page, setPage] = useState<ArtifactPage>()
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [kind, setKind] = useState<SourceRevision['revision_type']>('CORRECT')
  const [start, setStart] = useState<number | null>(null)
  const [end, setEnd] = useState<number | null>(null)
  const [replacement, setReplacement] = useState('')
  const [actor, setActor] = useState('')
  const [reason, setReason] = useState('')
  const [startExtraction, setStartExtraction] = useState(false)
  const [snapshotMode, setSnapshotMode] = useState<'current' | 'parent' | 'bundle'>('current')
  const [snapshotId, setSnapshotId] = useState('')
  const [withBbox, setWithBbox] = useState(false)
  const [bbox, setBbox] = useState<Record<typeof bboxKeys[number], number | null>>({ x0: null, y0: null, x1: null, y1: null })
  const [preview, setPreview] = useState<{ value: SourcePreview; signature: string }>()
  const [run, setRun] = useState<SourceRun>()
  const [lastRevisionId, setLastRevisionId] = useState('')
  const sequence = useRef(0)
  const pageSequence = useRef(0)
  const extractionKeys = useRef<Record<string, string>>({})
  const latestRevision = history?.revisions.find((row) => row.revision_id === history.latest_revision_id)
  const activeArtifactId = latestRevision?.derived_artifact_id || latestRevision?.parent_artifact_id || history?.artifacts[0]?.artifact_id
  const currentArtifact = history?.artifacts.find((row) => row.artifact_id === artifactId)
  const text = page?.page.text || ''
  const selectedText = start != null && end != null ? codepoints(text).slice(start, end).join('') : ''
  const rotation = page?.page.page_rotation ?? page?.page.rotation
  const geometryKnown = rotation != null && Number.isFinite(rotation) && (page?.page.width || 0) > 0 && (page?.page.height || 0) > 0
  const canEdit = !!page && artifactId === activeArtifactId && loadedDocumentId === documentId.trim()

  const fail = (failure: unknown, fallback: string) => {
    const status = axios.isAxiosError(failure) ? failure.response?.status : undefined
    const recovery = status === 409 ? ' 请刷新文档版本，重新选择文本并预览。' : status === 404 ? ' 请检查文档、Artifact 或页码，随后重新加载。' : ''
    setError(apiError(failure, fallback) + recovery)
  }
  const resetSelection = () => { setStart(null); setEnd(null); setReplacement(''); setPreview(undefined); setWithBbox(false) }
  const loadPage = async (id: string, number: number) => {
    const request = ++pageSequence.current
    setPage(undefined); resetSelection(); setLoading(true)
    try { const value = await getArtifactPage(id, number); if (request === pageSequence.current) { setPage(value); setError('') } }
    catch (failure) { if (request === pageSequence.current) fail(failure, '页面索引加载失败。') }
    finally { if (request === pageSequence.current) setLoading(false) }
  }
  const loadHistory = async (id = documentId.trim(), preservePage = false) => {
    if (!id) return message.warning('请输入文档 ID')
    const request = ++sequence.current
    ++pageSequence.current
    setLoading(true); setError(''); setHistory(undefined); setPage(undefined); resetSelection()
    try {
      const value = await getArtifactHistory(id)
      if (request !== sequence.current) return
      setHistory(value); setLoadedDocumentId(id)
      const latest = value.revisions.find((row) => row.revision_id === value.latest_revision_id)
      const selected = latest?.derived_artifact_id || latest?.parent_artifact_id || value.artifacts[0]?.artifact_id
      setArtifactId(selected || '')
      const number = preservePage ? pageNumber : 1
      setPageNumber(number)
      if (selected) await loadPage(selected, number)
    } catch (failure) { if (request === sequence.current) fail(failure, 'Artifact 历史加载失败。') }
    finally { if (request === sequence.current) setLoading(false) }
  }
  const payload = (): SourceRevisionPayload => {
    if (!canEdit || !currentArtifact) throw new Error('请加载当前 Artifact 版本')
    if (!actor.trim() || !reason.trim()) throw new Error('请填写操作人和原因')
    if (kind === 'CORRECT' && (start == null || end == null || end <= start || !selectedText)) throw new Error('请在只读原文中选中需要订正的文本')
    if (snapshotMode !== 'current' && !snapshotId.trim()) throw new Error('请填写规则快照 ID')
    if (withBbox && (!geometryKnown || bboxKeys.some((key) => bbox[key] == null))) throw new Error('请补全 PDF_POINT 边框坐标，或关闭区域框')
    return {
      document_id: loadedDocumentId, parent_artifact_id: artifactId,
      expected_parent_revision_id: history?.latest_revision_id || null,
      expected_file_hash: currentArtifact.file_hash, page_number: pageNumber,
      revision_type: kind, ...(start != null && end != null && end > start ? { start_offset: start, end_offset: end, original_text: selectedText } : {}),
      ...(kind === 'CORRECT' ? { replacement_text: replacement } : {}),
      ...(withBbox ? { bbox: bbox as { x0: number; y0: number; x1: number; y1: number } } : {}),
      created_by: actor.trim(), comment: reason.trim(), start_extraction: startExtraction,
      ...(snapshotMode === 'parent' ? { parent_run_id: snapshotId.trim() } : {}),
      ...(snapshotMode === 'bundle' ? { rule_bundle_id: snapshotId.trim() } : {}),
    }
  }
  let signature = ''
  try { signature = JSON.stringify(payload()) } catch { /* Incomplete forms cannot be saved. */ }
  const dryPreview = async () => {
    setBusy('preview'); setError('')
    try { const input = payload(); const value = await previewSourceRevision(input); setPreview({ value, signature: JSON.stringify(input) }) }
    catch (failure) { if (failure instanceof Error && !axios.isAxiosError(failure)) setError(failure.message); else fail(failure, '影响预览失败。') }
    finally { setBusy('') }
  }
  const save = async () => {
    setBusy('save'); setError('')
    try {
      const input = payload()
      if (preview?.signature !== JSON.stringify(input)) throw new Error('表单已变化，请重新预览影响')
      const result = await createSourceRevision(input)
      setLastRevisionId(result.revision.revision_id); setRun(result.run || undefined)
      message.success('源文修订已追加，原始 PDF 与历史 Artifact 保留')
      await loadHistory(loadedDocumentId, true)
    } catch (failure) { if (failure instanceof Error && !axios.isAxiosError(failure)) setError(failure.message); else fail(failure, '保存修订失败。') }
    finally { setBusy('') }
  }
  const extract = async (revisionId: string, retry = false) => {
    setBusy(revisionId); setError('')
    try {
      const requestId = extractionKeys.current[revisionId] ||= createRequestId()
      const result = retry ? await retrySourceExtraction(revisionId) : await extractSourceRevision(revisionId, {
        request_id: requestId,
        ...(snapshotMode === 'parent' ? { parent_run_id: snapshotId.trim() } : {}),
        ...(snapshotMode === 'bundle' ? { rule_bundle_id: snapshotId.trim() } : {}),
      })
      setLastRevisionId(revisionId); setRun(result)
      if (result.status === 'FAILED') setError(result.error_message || '重算提交失败，修订仍已保存，可显式重试。')
      else message.success('已返回派生源文的重算运行')
    } catch (failure) { fail(failure, '提交重算失败。') }
    finally { setBusy('') }
  }

  return <div style={{ padding: 24 }}>
    <Space direction="vertical" size={20} style={{ width: '100%' }}>
      <div className="page-title-row"><div><h1>源文订正</h1><p>以区域修订生成派生文本索引，保留原始 PDF、自动提取和全部历史版本。</p></div></div>
      <Space.Compact style={{ width: '100%', maxWidth: 800 }}>
        <Input aria-label="文档 ID" value={documentId} onChange={(event) => { setDocumentId(event.target.value); ++sequence.current; ++pageSequence.current; setPage(undefined); setHistory(undefined); setRun(undefined) }} onPressEnter={() => void loadHistory()} placeholder="从解析结果复制文档 ID" />
        <Button type="primary" icon={<ReloadOutlined />} loading={loading} onClick={() => void loadHistory()}>加载文档</Button>
      </Space.Compact>
      {error && <Alert type="error" showIcon message={error} action={loadedDocumentId ? <Button disabled={!!busy} onClick={() => void loadHistory(loadedDocumentId, true)}>刷新版本</Button> : undefined} />}
      {!history && !loading && <Empty description="加载文档后查看源文版本并选择要订正的区域" />}
      {history && <>
        <Card title="选择源文版本" extra={<Button href={getDocumentFile(loadedDocumentId)} target="_blank" rel="noopener noreferrer">查看原始 PDF</Button>}>
          {!history.artifacts.length ? <Empty description="尚无前处理 Artifact，请先完成一次文档解析" /> : <Space wrap align="center">
            <Select aria-label="Artifact 版本" value={artifactId} style={{ minWidth: 280, maxWidth: '100%' }} options={history.artifacts.map((item) => ({ value: item.artifact_id, label: `${item.created_at.slice(0, 19)} · ${item.parent_artifact_id ? '派生' : '原始'} · ${item.status}` }))}
              onChange={(id) => { setArtifactId(id); void loadPage(id, pageNumber) }} />
            <label>PDF 页码 <InputNumber aria-label="PDF 页码" value={pageNumber} min={1} precision={0} onChange={(value) => { setPageNumber(value || 1); setPage(undefined); resetSelection() }} /></label>
            <Button loading={loading} onClick={() => void loadPage(artifactId, pageNumber)}>读取页面</Button>
            <Tag>{currentArtifact?.source_review_status === 'PARTIALLY_REVIEWED' ? '部分区域已复核' : '未复核'}</Tag>
          </Space>}
          {currentArtifact && <Descriptions size="small" column={1} style={{ marginTop: 16 }} items={[
            { key: 'id', label: 'Artifact', children: <Typography.Text copyable>{artifactId}</Typography.Text> },
            { key: 'hash', label: '文件 SHA256', children: <Typography.Text style={{ overflowWrap: 'anywhere' }} copyable>{currentArtifact.file_hash}</Typography.Text> },
            { key: 'parent', label: '父 Artifact', children: currentArtifact.parent_artifact_id || '原始前处理产物' },
            { key: 'revision', label: '最新修订', children: history.latest_revision_id || '暂无修订' },
          ]} />}
        </Card>
        {page && <Card title={`第 ${pageNumber} 页源文索引`}>
          {!canEdit && <Alert type="warning" showIcon message="当前查看历史版本。保存订正前请刷新文档以切换到最新 Artifact。" style={{ marginBottom: 16 }} />}
          <Form layout="vertical">
            <Form.Item label="原始索引文本（只读，可拖选文本自动记录 offset）" help="offset 按 Unicode 字符计数，起点含、终点不含；页面订正不会修改 PDF 图像。">
              <Input.TextArea aria-label="只读源文文本" value={text} readOnly rows={12} onSelect={(event) => {
                const element = event.target as HTMLTextAreaElement
                if (element.selectionStart === element.selectionEnd) return
                setStart(codepoints(text.slice(0, element.selectionStart)).length)
                setEnd(codepoints(text.slice(0, element.selectionEnd)).length)
              }} />
            </Form.Item>
            <Space wrap><Form.Item label="起点 offset"><InputNumber min={0} max={codepoints(text).length} precision={0} value={start} onChange={setStart} /></Form.Item><Form.Item label="终点 offset"><InputNumber min={0} max={codepoints(text).length} precision={0} value={end} onChange={setEnd} /></Form.Item></Space>
            <Form.Item label="选中原文"><Input.TextArea value={selectedText} readOnly rows={2} /></Form.Item>
            <Form.Item label="修订类型"><Select value={kind} options={Object.entries(typeLabels).map(([value, label]) => ({ value, label }))} onChange={setKind} /></Form.Item>
            {kind === 'CORRECT' && <Form.Item label="替换文本（留空表示删除所选文本）"><Input.TextArea value={replacement} onChange={(event) => setReplacement(event.target.value)} rows={3} maxLength={20000} showCount /></Form.Item>}
            <Space wrap align="start"><Form.Item label="操作人" required><Input value={actor} onChange={(event) => setActor(event.target.value)} maxLength={100} /></Form.Item><Form.Item label="修订原因" required><Input.TextArea value={reason} onChange={(event) => setReason(event.target.value)} rows={2} maxLength={4000} style={{ minWidth: 260 }} /></Form.Item></Space>
            {!geometryKnown && <Alert type="info" showIcon message="本页坐标/旋转信息不完整，使用文本 offset 订正；区域框降级停用。" style={{ marginBottom: 16 }} />}
            <Form.Item><Checkbox disabled={!geometryKnown} checked={withBbox && geometryKnown} onChange={(event) => setWithBbox(event.target.checked)}>附加区域框（左上角原点，PDF_POINT，原页坐标）</Checkbox></Form.Item>
            {withBbox && geometryKnown && <Space wrap>{bboxKeys.map((key) => <Form.Item key={key} label={key}><InputNumber min={0} value={bbox[key]} onChange={(value) => setBbox((values) => ({ ...values, [key]: value }))} /></Form.Item>)}</Space>}
            <Form.Item label="重算规则来源"><Space wrap><Select value={snapshotMode} style={{ width: 220 }} onChange={(value) => { setSnapshotMode(value); setSnapshotId('') }} options={[{ value: 'current', label: '当前生效规则' }, { value: 'parent', label: '指定父运行固定快照' }, { value: 'bundle', label: '指定规则 bundle 试跑' }]} />{snapshotMode !== 'current' && <Input aria-label="规则快照 ID" value={snapshotId} onChange={(event) => setSnapshotId(event.target.value)} placeholder={snapshotMode === 'parent' ? '父解析运行 UUID' : '规则 bundle UUID'} style={{ width: 320, maxWidth: '100%' }} />}</Space></Form.Item>
            <Form.Item><Checkbox checked={startExtraction} onChange={(event) => setStartExtraction(event.target.checked)}>保存后立即启动全量字段重算（复用派生 Artifact）</Checkbox></Form.Item>
            <Space wrap><Button disabled={!canEdit || !!busy} loading={busy === 'preview'} onClick={() => void dryPreview()}>预览订正与影响</Button><Button type="primary" loading={busy === 'save'} disabled={!canEdit || !!busy || !signature || preview?.signature !== signature} onClick={() => void save()}>追加修订记录</Button></Space>
          </Form>
          {preview && <div style={{ marginTop: 20 }}><Alert type="info" showIcon message={`保守全量重算 ${preview.value.affected_fields.length} 个字段；区域依赖尚未完整建立，自动事实仍为未核验。`} description={<><p>仅订正所选文本；订正页的表格与 OCR 结构会失效。原始 PDF 图像保持原样，后续模型应同时考虑订正记录。</p><p>处理状态：{preview.value.extraction_available ? '可使用 READY Artifact 重算' : 'Artifact 不完整，暂不可直接重算'}。源文状态：部分区域已复核。</p><Typography.Text style={{ overflowWrap: 'anywhere' }}>{preview.value.affected_fields.join('、')}</Typography.Text></>} />{kind === 'CORRECT' && <Form.Item label="预览派生页文本" style={{ marginTop: 16 }}><Input.TextArea readOnly rows={6} value={preview.value.derived_text} /></Form.Item>}</div>}
        </Card>}
        {run && <Alert type={run.status === 'FAILED' ? 'error' : 'success'} showIcon message={`重算运行：${run.status}`} description={<Space wrap><Link to={`/result/${run.parse_run_id}`}>查看解析运行</Link>{run.error_message && <span>{run.error_message}</span>}{run.status === 'FAILED' && <Button loading={busy === lastRevisionId} onClick={() => void extract(lastRevisionId, true)}>重试队列提交</Button>}</Space>} />}
        <Card title="源文修订记录">
          <Table<SourceRevision> rowKey="revision_id" dataSource={history.revisions} scroll={{ x: 1000 }} pagination={{ pageSize: 8 }} columns={[
            { title: '时间 / 操作人', render: (_, row) => <>{row.created_at.slice(0, 19)}<br />{row.created_by}</> },
            { title: '类型 / 页码', render: (_, row) => <>{typeLabels[row.revision_type]} · 第 {row.page_number} 页</> },
            { title: '原文 → 订正', width: 260, render: (_, row) => <div style={{ overflowWrap: 'anywhere' }}>{row.original_text || '整页/区域标记'}{row.revision_type === 'CORRECT' && <> → {row.replacement_text || '（删除）'}</>}</div> },
            { title: '原因', dataIndex: 'comment', width: 200 },
            { title: '状态', render: () => <Tag>部分区域已复核</Tag> },
            { title: '操作', width: 230, render: (_, row) => <Space wrap>{row.extraction_run_id && <Link to={`/result/${row.extraction_run_id}`}>初始重算</Link>}<Button size="small" disabled={row.revision_id !== history.latest_revision_id || !!busy} loading={busy === row.revision_id} onClick={() => void extract(row.revision_id)}>固定此修订重算</Button>{row.extraction_run_id && <Button size="small" disabled={!!busy} onClick={() => void extract(row.revision_id, true)}>重试提交</Button>}</Space> },
          ]} locale={{ emptyText: '暂无源文修订；人工区域确认不代表全文或字段事实已经核验。' }} />
        </Card>
      </>}
    </Space>
  </div>
}
