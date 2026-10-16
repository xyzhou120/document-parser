import { useEffect, useState } from 'react'
import { Alert, Button, Input, InputNumber, Select, Space, Tag, message } from 'antd'
import api, { apiError } from '@/services/api'
import type { ExtractedField, FactEvidence, ParseResultResponse } from '@/types/api'

interface Evidence extends FactEvidence {
  page_number: number
  bbox: number[] | null
  coordinate_system: string
  evidence_id?: string
  role: string
  quote?: string
}
interface Fact {
  raw_value: string
  value: string
  unit: string | null
  value_kind: string
  source_role: string
  dimensions: Record<string, string | null>
  evidence: Evidence[]
}
interface Revision {
  revision: number
  action: string
  reviewer: string
  reason: string
  created_at: string
  review_status: string
  facts: Fact[]
}
interface History {
  latest_revision: number
  review_status: string
  history: Revision[]
}

const blankFact = (): Fact => ({ raw_value: '', value: '', unit: null, value_kind: 'unknown', source_role: 'unknown', dimensions: {}, evidence: [{ page_number: 1, bbox: null, coordinate_system: 'PDF_POINT', coordinate_origin: 'MUPDF_UNROTATED_TOP_LEFT', role: 'value' }] })

function automaticFacts(field: ExtractedField, result: ParseResultResponse): Fact[] {
  const delivered = result.facts?.filter((fact) => fact.field_key === field.field_key)
  if (delivered?.length) return delivered.map((fact) => ({
    raw_value: fact.raw, value: fact.value, unit: fact.unit, value_kind: fact.value_kind, source_role: fact.source_role,
    dimensions: Object.fromEntries(Object.entries(fact.dimensions).map(([key, value]) => [key, value == null ? null : typeof value === 'object' ? String((value as { value?: unknown }).value ?? '') : String(value)])),
    evidence: fact.evidence.filter((ref) => !!ref.page_number).map((ref) => ({ ...ref, page_number: ref.page_number!,
      bbox: ref.coordinate_system !== 'PDF_POINT' ? null : Array.isArray(ref.bbox) ? ref.bbox : ref.bbox ? [ref.bbox.x0, ref.bbox.y0, ref.bbox.x1, ref.bbox.y1] : null,
      coordinate_system: 'PDF_POINT', role: ref.role || 'value', quote: ref.quote || undefined })),
  }))
  const registry = field.extraction_detail?.evidence_registry || {}
  return (field.instances || field.extraction_detail?.instances || []).map((instance) => {
    const raw = instance as unknown as Record<string, unknown>
    const dims = instance.dimensions || {}
    const ids = [...new Set([...(instance.evidence_ids || []), ...Object.values(dims).flatMap((dim) => typeof dim === 'object' && dim ? (dim as { evidence_ids?: string[] }).evidence_ids || [] : [])])]
    return {
      raw_value: String(raw.raw_value ?? raw.value ?? ''), value: String(raw.value ?? ''), unit: instance.unit || null,
      value_kind: String(raw.value_kind || 'unknown'), source_role: String(raw.source_role || 'unknown'),
      dimensions: Object.fromEntries(Object.entries(dims).map(([key, dim]) => [key, typeof dim === 'string' ? dim : dim?.value || null])),
      evidence: ids.flatMap((id) => {
        const evidence = registry[id]
        const bbox = evidence?.coordinate_system !== 'PDF_POINT' ? null : Array.isArray(evidence.bbox) ? evidence.bbox : evidence.bbox ? [evidence.bbox.x0, evidence.bbox.y0, evidence.bbox.x1, evidence.bbox.y1] : null
        return evidence?.page_number ? [{ ...evidence, page_number: evidence.page_number, bbox,
          coordinate_system: 'PDF_POINT', evidence_id: id, role: 'value', quote: String(raw.quote || '') }] : []
      }),
    }
  })
}

export default function HumanReviewPanel({ result, field, onPage, onEvidence, onSaved }: {
  result: ParseResultResponse; field: ExtractedField; onPage: (page: number) => void
  onEvidence?: (evidence: FactEvidence) => void
  onSaved?: () => void
}) {
  const [history, setHistory] = useState<History>()
  const [facts, setFacts] = useState<Fact[]>([])
  const [reviewer, setReviewer] = useState('')
  const [reason, setReason] = useState('')
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const sourceRunId = field.source_parse_run_id || result.parse_run_id
  const path = `/v1/reviews/runs/${sourceRunId}/fields/${encodeURIComponent(field.field_key)}`
  const fetchHistory = async () => {
    const value: History = await api.get(path)
    setHistory(value)
    return value
  }
  useEffect(() => {
    let disposed = false
    setLoading(true); setHistory(undefined); setError(''); setReason('')
    void api.get<unknown, History>(path).then((value) => {
      if (disposed) return
      setHistory(value)
      const latest = value.history[value.history.length - 1]
      setFacts(latest?.facts.length ? latest.facts : automaticFacts(field, result))
    }).catch((failure) => { if (!disposed) setError(apiError(failure, '读取复核历史失败')) })
      .finally(() => { if (!disposed) setLoading(false) })
    return () => { disposed = true }
  }, [path, field, result])

  const changeFact = (index: number, patch: Partial<Fact>) => setFacts((rows) => rows.map((row, i) => i === index ? { ...row, ...patch } : row))
  const submit = async (action: 'ACCEPT' | 'CORRECT' | 'REJECT') => {
    if (!reviewer.trim()) { message.error('请填写实际复核人'); return }
    if (action !== 'ACCEPT' && !reason.trim()) { message.error('请填写校正或拒绝原因'); return }
    if (action === 'CORRECT' && (!facts.length || facts.some((fact) => !fact.value.trim() || !fact.raw_value.trim() || !fact.evidence.length))) {
      message.error('每个事实须填写原文值、规范值和至少一处证据'); return
    }
    setSaving(true); setError('')
    try {
      await api.post(path, { action, reviewer: reviewer.trim(), reason: reason.trim(), file_hash: result.file_hash,
        expected_revision: history?.latest_revision || 0, facts: action === 'CORRECT' ? facts.map((fact) => ({ ...fact,
          evidence: fact.evidence.map((evidence) => ({ page_number: evidence.page_number, bbox: evidence.bbox,
            coordinate_system: evidence.coordinate_system, evidence_id: evidence.evidence_id,
            role: evidence.role, quote: evidence.quote })) })) : [] })
      await fetchHistory()
      onSaved?.()
      message.success('复核记录已追加，自动结果保留')
    } catch (failure) { setError(apiError(failure, '保存复核失败；若版本冲突，请刷新历史后重新核对')) }
    finally { setSaving(false) }
  }
  const exportReviewed = async () => {
    try {
      const value = await api.get(`/v1/reviews/runs/${sourceRunId}/export`)
      const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], { type: 'application/json' }))
      const link = document.createElement('a'); link.href = url; link.download = `人工复核-${sourceRunId}.json`; link.click(); URL.revokeObjectURL(url)
    } catch (failure) { message.error(apiError(failure, '导出人工复核失败')) }
  }
  const dimensionKeys = [...new Set([...Object.keys((result.extraction_profile?.rules || []).find((rule) => rule.field_key === field.field_key)?.dimension_schema || {}), ...facts.flatMap((fact) => Object.keys(fact.dimensions))])]
  return <section style={{ marginTop: 20, borderTop: '1px solid #e5e7eb', paddingTop: 12 }}>
    <strong>人工复核</strong> <Tag color={history?.review_status === 'HUMAN_REVIEWED' ? 'blue' : 'gold'}>{history?.review_status === 'HUMAN_REVIEWED' ? '已有人工复核' : history?.review_status === 'REJECTED' ? '已拒绝' : '未复核'}</Tag>
    <p>复核用于确认、纠错和追溯，并非所有自动事实的下游读取前提。接受当前审核版本或原始自动事实；编辑后使用“保存校正”。</p>
    {error && <Alert type="error" message={error} showIcon />}
    <Input placeholder="实际复核人" value={reviewer} onChange={(event) => setReviewer(event.target.value)} disabled={saving} />
    <Input.TextArea style={{ marginTop: 8 }} rows={2} placeholder="核对依据、校正或拒绝原因" value={reason} onChange={(event) => setReason(event.target.value)} />
    <details style={{ marginTop: 10 }}><summary>编辑事实与证据（{facts.length} 条）</summary>
      {facts.map((fact, index) => <div key={index} style={{ padding: '12px 0', borderBottom: '1px solid #eee' }}>
        <Space><strong>事实 {index + 1}</strong><Button size="small" danger onClick={() => setFacts(facts.filter((_, i) => i !== index))}>移除</Button></Space>
        <label>原文值<Input value={fact.raw_value} onChange={(event) => changeFact(index, { raw_value: event.target.value })} /></label>
        <label>规范值<Input value={fact.value} onChange={(event) => changeFact(index, { value: event.target.value })} /></label>
        <label>原文单位<Input value={fact.unit || ''} placeholder="无单位可留空" onChange={(event) => changeFact(index, { unit: event.target.value || null })} /></label>
        <label>值类型<Select style={{ width: '100%' }} value={fact.value_kind} options={['original', 'design', 'actual', 'calculated', 'measured', 'reported', 'threshold', 'process', 'presence', 'unknown'].map((value) => ({ value, label: value }))} onChange={(value) => changeFact(index, { value_kind: value })} /></label>
        <label>来源角色<Input value={fact.source_role} placeholder="如 report_body、report_sheet" onChange={(event) => changeFact(index, { source_role: event.target.value })} /></label>
        {dimensionKeys.map((key) => <label key={key}>{key}<Input value={fact.dimensions[key] || ''} placeholder="未确认或不适用留空" onChange={(event) => changeFact(index, { dimensions: { ...fact.dimensions, [key]: event.target.value || null } })} /></label>)}
        {fact.evidence.map((evidence, evIndex) => <div key={evIndex} style={{ padding: 8, marginTop: 8, background: '#f6f8fa' }}>
          <Space>PDF 页序<InputNumber min={1} value={evidence.page_number} onChange={(value) => changeFact(index, { evidence: fact.evidence.map((ev, i) => i === evIndex ? { ...ev, page_number: value || 1, evidence_id: undefined } : ev) })} /><Button size="small" onClick={() => { onPage(evidence.page_number); onEvidence?.(evidence) }}>查看</Button></Space>
          <label>原文引句<Input.TextArea rows={2} value={evidence.quote || ''} onChange={(event) => changeFact(index, { evidence: fact.evidence.map((ev, i) => i === evIndex ? { ...ev, quote: event.target.value } : ev) })} /></label>
          <label>证据角色<Input value={evidence.role} onChange={(event) => changeFact(index, { evidence: fact.evidence.map((ev, i) => i === evIndex ? { ...ev, role: event.target.value } : ev) })} /></label>
          <label>区域坐标（PDF 点，可留空只定位到页）<Input defaultValue={evidence.bbox?.join(',') || ''} placeholder="x0,y0,x1,y1" onBlur={(event) => {
            const value = event.target.value.trim(); const bbox = value ? value.split(',').map(Number) : null
            if (bbox && (bbox.length !== 4 || bbox.some((n) => !Number.isFinite(n)) || bbox[0] < 0 || bbox[1] < 0 || bbox[0] >= bbox[2] || bbox[1] >= bbox[3])) { message.error('坐标需要四个有限数字，且右下角必须大于左上角'); return }
            changeFact(index, { evidence: fact.evidence.map((ev, i) => i === evIndex ? { ...ev, bbox, evidence_id: undefined } : ev) })
          }} /></label>
          <Button size="small" danger onClick={() => changeFact(index, { evidence: fact.evidence.filter((_, i) => i !== evIndex) })}>移除证据</Button>
        </div>)}
        <Button size="small" onClick={() => changeFact(index, { evidence: [...fact.evidence, { page_number: 1, bbox: null, coordinate_system: 'PDF_POINT', coordinate_origin: 'MUPDF_UNROTATED_TOP_LEFT', role: 'value' }] })}>添加证据</Button>
      </div>)}
      <Button size="small" onClick={() => setFacts([...facts, blankFact()])}>添加事实</Button>
    </details>
    <Space wrap style={{ marginTop: 12 }}>
      <Button size="small" loading={saving} disabled={loading || !history || !result.file_hash} onClick={() => void submit('ACCEPT')}>接受当前事实</Button>
      <Button size="small" type="primary" loading={saving} disabled={loading || !history || !result.file_hash} onClick={() => void submit('CORRECT')}>保存校正</Button>
      <Button size="small" danger loading={saving} disabled={loading || !history || !result.file_hash} onClick={() => void submit('REJECT')}>拒绝</Button>
      <Button size="small" onClick={() => void fetchHistory().catch((failure) => setError(apiError(failure, '读取历史失败')))}>刷新历史</Button>
      <Button size="small" onClick={() => void exportReviewed()}>导出人工复核</Button>
    </Space>
    <details style={{ marginTop: 10 }}><summary>修订历史（{history?.history.length || 0}）</summary>{history?.history.map((revision) => <div key={revision.revision} style={{ padding: '8px 0' }}>
      <strong>v{revision.revision} · {revision.action} · {revision.reviewer}</strong><p>{revision.created_at} · {revision.reason}</p>
      {revision.facts.flatMap((fact, index) => fact.evidence.map((evidence, evidenceIndex) => <Button key={`${index}-${evidenceIndex}`} type="link" size="small" onClick={() => { onPage(evidence.page_number); onEvidence?.(evidence) }}>事实{index + 1} · {evidence.role} · 第{evidence.page_number}页</Button>))}
      <pre style={{ whiteSpace: 'pre-wrap', maxHeight: 220, overflow: 'auto' }}>{JSON.stringify(revision.facts, null, 2)}</pre>
    </div>)}</details>
  </section>
}
