import { useEffect, useMemo, useRef, useState } from 'react'
import { Alert, Button, Empty, Input, Segmented, Steps, Tabs, Tag, message } from 'antd'
import {
  DownloadOutlined,
  FilePdfOutlined,
  FileSearchOutlined,
  LinkOutlined,
  SearchOutlined,
} from '@ant-design/icons'
import type { DownstreamFact, ExtractedField, FactEvidence, ParseResultResponse } from '@/types/api'
import PdfPreview from './PdfPreview'
import HumanReviewPanel from './HumanReviewPanel'
import { exportRunFacts, getRunFacts } from '@/services/parse'
import { apiError } from '@/services/api'
import ExecutionPanel from './ExecutionPanel'
import AssistantPanel from './AssistantPanel'
import AssistantErrorBoundary from './AssistantErrorBoundary'
import type { ExecutionResponse } from '@/types/api'
import { executionStatusNames } from '@/services/execution'
import '@/styles/result-workspace.css'

export const statusNames: Record<string, string> = {
  ...executionStatusNames,
  PENDING: '等待处理',
  RUNNING: '解析中',
  PROCESSING: '解析中',
  RETRYING: '等待重试',
  COMPLETED: '解析结束',
  FAILED: '解析失败',
}

/** 提取方法标识 -> 中文名 */
const methodNames: Record<string, string> = {
  keyword_search: '关键词检索',
  keyword: '关键词检索',
  regex: '正则匹配',
  table_lookup: '表格查找',
  table_text: '文本表头检索',
  llm_extract: '模型提取',
  llm: '模型提取',
  fallback: '兜底值',
  none: '未命中',
  multipath: '多路径',
  vision_agent: '清单智能体',
  agent_extract: '清单智能体',
}

/** Plan C7 (2026-10-10): stable reason codes from the checklist agent, in reviewer wording. */
const incompleteReasonNames: Record<string, string> = {
  token_budget_exhausted: '本字段 Token 额度用尽，剩余候选未核对',
  candidates_truncated: '强或中等候选超出核对上限，未全部核对',
  candidates_unverified: '部分强或中等候选未核对',
  ocr_unavailable_pages: '存在无法读取的页面（OCR 缺失或失败），不能判定为未找到',
  ocr_unverified: 'OCR 候选缺少原图核对记录，未采信',
  image_budget_exhausted: '单次原图张数达到上限，部分 OCR 候选未核对',
  image_unavailable: '原图渲染失败，相关候选未核对',
  invalid_output: '模型输出无法解析为结果',
  output_truncated: '模型输出被截断',
  value_not_in_candidate: '模型给出的值不在候选原文中，已拒绝',
  round_budget_exhausted: '调用轮次用尽',
  no_finish_within_rounds: '未在轮次内结束取证（旧引擎）',
  context_budget_exhausted: '上下文超出预算（旧引擎）',
  actions_truncated: '工具动作被截断（旧引擎）',
  no_action: '模型未给出动作（旧引擎）',
}

const targetStatusNames: Record<string, string> = {
  NOT_RUN: '未运行',
  RUNNING: '提取中',
  FAILED: '执行失败',
  NOT_FOUND_AFTER_COMPLETE_SEARCH: '完成检索未找到',
  PARTIAL: '部分完成',
  REVIEW_REQUIRED: '需要复核',
  DELIVERABLE: '可交付',
}

function fieldStatus(item: ExtractedField) {
  const status = item.target_status || 'NOT_RUN'
  return { label: targetStatusNames[status] || status, review: status !== 'DELIVERABLE' }
}

const valueKindNames: Record<string, string> = {
  original: '原始', design: '设计', actual: '实际', calculated: '计算', measured: '实测',
  threshold: '阈值', reported: '报告记录', unknown: '未知',
}

const dimensionNames: Record<string, string> = {
  workface: '工作面', evaluation_unit: '评价单元', measurement_point: '测点',
  roadway: '巷道', measurement_date: '测定日期', scope_level: '范围',
}

function dimensionValue(dimension: unknown): string | null {
  if (dimension == null || dimension === '' || dimension === 'unknown') return null
  if (typeof dimension === 'string') return dimension
  if (typeof dimension === 'object') {
    const value = (dimension as { value?: unknown }).value
    if (value == null || value === '' || value === 'unknown') return null
    return String(value)
  }
  return String(dimension)
}

function dimensionEntries(dimensions?: Record<string, unknown> | null) {
  return Object.entries(dimensions || {}).flatMap(([key, dimension]) => {
    const value = dimensionValue(dimension)
    return value ? [[dimensionNames[key] || key, value] as const] : []
  })
}

function identitySummary(items: Array<Record<string, unknown> | null | undefined>) {
  const faces = new Set<string>()
  const units = new Set<string>()
  items.forEach((item) => {
    const face = dimensionValue(item?.workface)
    const unit = dimensionValue(item?.evaluation_unit)
    if (face) faces.add(face)
    if (unit) units.add(unit)
  })
  const text = (label: string, values: Set<string>) => {
    const list = [...values]
    if (!list.length) return ''
    return `${label} ${list.slice(0, 3).join('、')}${list.length > 3 ? '…' : ''}`
  }
  return [text('工作面', faces), text('单元', units)].filter(Boolean).join(' · ')
}

function boxNumbers(bbox: unknown): number[] | null {
  const raw = Array.isArray(bbox)
    ? bbox
    : bbox && typeof bbox === 'object'
      ? [(bbox as { x0?: unknown }).x0, (bbox as { y0?: unknown }).y0, (bbox as { x1?: unknown }).x1, (bbox as { y1?: unknown }).y1]
      : null
  if (!raw || raw.length !== 4 || raw.some((value) => typeof value !== 'number' || !Number.isFinite(value))) return null
  return raw
}

function located(item: FactEvidence): FactEvidence {
  const box = boxNumbers(item.bbox)
  if (!box) return item
  return {
    ...item,
    bbox: box,
    coordinate_system: item.coordinate_system || 'PDF_POINT',
    coordinate_origin: item.coordinate_origin || 'MUPDF_UNROTATED_TOP_LEFT',
  }
}

function sourceCards(items: FactEvidence[]) {
  const seen = new Set<string>()
  const cards: FactEvidence[] = []
  items.forEach((item) => {
    if (!item.page_number) return
    const quote = (item.quote || '').replace(/\s+/g, '')
    const key = `${item.page_number}|${quote}`
    if (seen.has(key)) return
    seen.add(key)
    cards.push(item)
  })
  return cards
}

type TargetKind = 'fact' | 'statement' | 'table'
type ProfileRule = {
  field_key: string
  target_type?: TargetKind
  multi_instance?: boolean
  output_schema?: { columns?: Array<{ key?: string; header?: string; unit?: string }>; row_identity?: string[] }
  strategy?: { semantic_contract?: { cardinality?: string } }
}
type ResultRow = {
  id: string
  text: string
  unit?: string | null
  quote?: string | null
  polarity?: string | null
  page?: number | null
  pages?: number[]
  evidenceId?: string
  dimensions: Record<string, unknown>
  status: 'ready' | 'review' | 'conflict'
  statusLabel: string
  cells: Record<string, string>
  kind?: string
}

const polarityNames: Record<string, string> = { affirmed: '肯定', negated: '否定', uncertain: '不确定', unknown: '未知' }
const targetKindNames: Record<TargetKind, string> = { fact: '普通事实', statement: '原文内容', table: '表格明细' }
const dimensionOrder = ['evaluation_unit', 'roadway', 'measurement_point', 'measurement_date', 'scope_level']

function profileRules(result?: ParseResultResponse): ProfileRule[] {
  return (result?.extraction_profile?.rules || []) as ProfileRule[]
}

function profileRule(item: ExtractedField, rules: ProfileRule[]) {
  return rules.find((rule) => rule.field_key === item.field_key)
}

function targetKind(item: ExtractedField, rule?: ProfileRule): TargetKind {
  return item.target_type || rule?.target_type || 'fact'
}

function multiConfigured(rule?: ProfileRule) {
  const cardinality = rule?.strategy?.semantic_contract?.cardinality
  return Boolean(rule?.multi_instance || cardinality === 'multi_identity' || cardinality === 'all_occurrences')
}

function clipText(value: string, limit = 28) {
  const sentence = value.split(/[。！？\n]/)[0]?.trim() || value.trim()
  return sentence.length > limit ? `${sentence.slice(0, limit)}…` : sentence
}

function cellText(cell: unknown) {
  if (cell == null || cell === '') return ''
  if (typeof cell === 'object' && 'value' in (cell as object)) {
    const value = (cell as { value?: unknown }).value
    return value == null || value === '' ? '' : String(value)
  }
  return String(cell)
}

function identityKey(dimensions: Record<string, unknown>) {
  return dimensionEntries(dimensions).map(([key, value]) => `${key}=${value}`).join('|')
}

function canonicalWorkface(value: string) {
  const core = value.replace(/\s+/g, '').replace(/(综采|综放|掘进)?工作面$/g, '')
  return core ? `${core}工作面` : value.replace(/\s+/g, '')
}

function canonicalUnit(value: string) {
  const compact = value.replace(/\s+/g, '')
  const match = compact.match(/第([一二三四五六七八九十\d]+)(?:评判)?单元/)
  return match ? `第${match[1]}单元` : compact
}

function normalizeDimensions(dimensions: Record<string, unknown>) {
  const next: Record<string, unknown> = { ...dimensions }
  const face = dimensionValue(next.workface)
  const unit = dimensionValue(next.evaluation_unit)
  if (face) next.workface = canonicalWorkface(face)
  if (unit) next.evaluation_unit = canonicalUnit(unit)
  Object.keys(next).forEach((key) => {
    if (key === 'workface' || key === 'evaluation_unit') return
    const value = dimensionValue(next[key])
    if (value) next[key] = value.replace(/\s+/g, '')
  })
  return next
}

function spanSignature(text: string, workface: string) {
  const face = workface.replace(/工作面$/g, '')
  let compact = text.replace(/\s+/g, '')
  if (face) compact = compact.split(face).join('')
  compact = compact.replace(/第[一二三四五六七八九十\d]+(?:评判)?单元/g, '')
  const numbers = [...compact.matchAll(/\d+(?:\.\d+)?/g)].map((item) => Number(item[0])).filter((value) => value >= 30 && value < 10000)
  for (let index = 0; index < numbers.length - 1; index += 1) {
    if (numbers[index + 1] > numbers[index]) return `${numbers[index]}-${numbers[index + 1]}`
  }
  return ''
}

function mergeCluster(rows: ResultRow[], conflict: boolean): ResultRow {
  const best = [...rows].sort((left, right) => right.text.length - left.text.length)[0]
  const pages = [...new Set(rows.flatMap((row) => row.pages?.length ? row.pages : row.page ? [row.page] : []))].sort((left, right) => left - right)
  const cleared = best.status === 'conflict' ? 'review' as const : best.status
  return {
    ...best,
    pages,
    page: best.page && pages.includes(best.page) ? best.page : pages[0],
    status: conflict ? 'conflict' : cleared,
    statusLabel: conflict ? '同身份冲突' : cleared === 'ready' ? '可交付' : '待核对',
  }
}

function collapseInstances(rows: ResultRow[]) {
  const grouped = new Map<string, ResultRow[]>()
  rows.forEach((row) => {
    const dimensions = normalizeDimensions(row.dimensions)
    const key = identityKey(dimensions) || row.id
    const list = grouped.get(key) || []
    list.push({ ...row, dimensions })
    grouped.set(key, list)
  })
  const collapsed: ResultRow[] = []
  grouped.forEach((group) => {
    const face = dimensionValue(group[0].dimensions.workface) || ''
    const ranged = new Map<string, ResultRow[]>()
    const labels: ResultRow[] = []
    group.forEach((row) => {
      const signature = spanSignature(row.text, face)
      if (!signature) labels.push(row)
      else ranged.set(signature, [...(ranged.get(signature) || []), row])
    })
    if (!ranged.size) {
      collapsed.push(mergeCluster(group, false))
      return
    }
    const conflict = ranged.size > 1
    ranged.forEach((list) => collapsed.push(mergeCluster(conflict ? list : [...list, ...labels], conflict)))
    if (conflict && labels.length) collapsed.push(mergeCluster(labels, false))
  })
  return collapsed
}

function rowFromFact(fact: DownstreamFact): ResultRow {
  const evidenceList = fact.evidence || []
  const evidence = evidenceList.find((item) => item.page_number && item.quote) || evidenceList.find((item) => item.page_number) || evidenceList[0]
  const evidenceIndex = evidence ? Math.max(0, evidenceList.indexOf(evidence)) : 0
  const cells = Object.fromEntries(Object.entries(fact.table_row?.cells || {}).map(([key, cell]) => [key, cellText(cell)]))
  const text = fact.statement?.text || (fact.target_type === 'table' ? fact.table_row_key || fact.value : fact.value) || fact.normalized || '不可确认'
  const ready = fact.available_for_downstream === true || fact.target_status === 'DELIVERABLE' || fact.row_status === 'DELIVERABLE'
  const review = fact.row_status === 'REVIEW_REQUIRED' || fact.target_status === 'REVIEW_REQUIRED' || fact.available_for_downstream === false
  return {
    id: fact.fact_id,
    text: String(text),
    unit: fact.unit,
    quote: fact.statement?.quote || evidence?.quote || fact.raw || null,
    polarity: fact.statement?.polarity || null,
    page: evidence?.page_number,
    evidenceId: evidence?.evidence_id || (evidence ? `fact-evidence-${fact.fact_id}-${evidenceIndex}` : undefined),
    dimensions: fact.dimensions || {},
    status: review && !ready ? 'review' : ready ? 'ready' : 'review',
    statusLabel: ready ? '可交付' : '待核对',
    cells,
    kind: fact.value_kind,
  }
}

function rowFromInstance(instance: NonNullable<ExtractedField['instances']>[number], index: number): ResultRow {
  return {
    id: instance.occurrence_id || `instance-${index}`,
    text: instance.value ?? '不可确认',
    unit: instance.unit,
    quote: instance.context || null,
    page: instance.page_num,
    evidenceId: `instance-${index}`,
    dimensions: instance.dimensions || {},
    status: instance.quality_status && instance.quality_status !== 'passed' ? 'review' : 'review',
    statusLabel: '待核对',
    cells: {},
    kind: instance.value_kind || undefined,
  }
}

function buildRows(item: ExtractedField, facts: DownstreamFact[]) {
  const own = facts.filter((fact) => fact.field_key === item.field_key).map(rowFromFact)
  const rows = own.length ? own : (item.instances || item.extraction_detail?.instances || []).map(rowFromInstance)
  return collapseInstances(rows)
}

function activeDimensionKeys(rows: ResultRow[], skip: string[] = []) {
  const present = new Set<string>()
  rows.forEach((row) => Object.keys(row.dimensions).forEach((key) => {
    if (!skip.includes(key) && dimensionValue(row.dimensions[key])) present.add(key)
  }))
  return [...dimensionOrder.filter((key) => present.has(key)), ...[...present].filter((key) => !dimensionOrder.includes(key))]
}

function emptyFieldText(item: ExtractedField) {
  const status = item.validation_status || item.extraction_detail?.validation_status
  if (status === 'partial_candidates') return '已有候选，未完成核对'
  if (status === 'not_found') return '未找到'
  if (status === 'incomplete') return '未完成'
  return '未找到'
}

function fieldDisplayValue(item: ExtractedField, rules: ProfileRule[] = [], facts: DownstreamFact[] = []) {
  const rule = profileRule(item, rules)
  const kind = targetKind(item, rule)
  const rows = buildRows(item, facts)
  const summary = identitySummary(rows.map((row) => row.dimensions))
  const several = multiConfigured(rule) || rows.length > 1
  if (!rows.length) return emptyFieldText(item)
  if (kind === 'table') return `${rows.length} 行`
  if (kind === 'statement') return several ? `${rows.length} 段原文` : clipText(rows[0].text)
  if (several) return summary ? `${rows.length} 组 · ${summary}` : `${rows.length} 组`
  const text = `${rows[0].text}${rows[0].unit ? ` ${rows[0].unit}` : ''}`
  return summary ? `${text} · ${summary}` : text
}

function focusLabel(row: ResultRow) {
  const pages = row.pages?.length ? row.pages : row.page ? [row.page] : []
  if (!pages.length) return '无页码'
  if (pages.length === 1) return `第 ${pages[0]} 页`
  const shown = pages.slice(0, 4).map((page) => `第 ${page} 页`).join('、')
  return pages.length > 4 ? `${shown} 等 ${pages.length} 页` : shown
}

function tableColumns(rule: ProfileRule | undefined, rows: ResultRow[]) {
  const configured = (rule?.output_schema?.columns || []).filter((column) => column.key).map((column) => ({
    key: column.key || '', label: column.header || column.key || '', from: 'cell' as const,
  }))
  if (configured.length) return configured
  return [...new Set(rows.flatMap((row) => Object.keys(row.cells)))].map((key) => ({ key, label: key, from: 'cell' as const }))
}

function ResultTable({ rows, columns, selectedId, onSelect, valueHeader = '值' }: {
  rows: ResultRow[]
  columns: Array<{ key: string; label: string; from: 'dimension' | 'cell' }>
  selectedId?: string
  onSelect: (row: ResultRow) => void
  valueHeader?: string
}) {
  return (
    <div className="result-grid-wrap">
      <table className="result-grid">
        <thead>
          <tr>
            {columns.map((column) => <th key={column.key}>{column.label}</th>)}
            {valueHeader && <th>{valueHeader}</th>}
            <th>页</th>
            <th>状态</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.id} tabIndex={0} aria-selected={!!selectedId && selectedId === row.evidenceId} className={selectedId && selectedId === row.evidenceId ? 'selected' : ''} onClick={() => onSelect(row)} onKeyDown={(event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); onSelect(row) } }}>
              {columns.map((column) => <td key={column.key}>{column.from === 'cell' ? (row.cells[column.key] || '—') : (dimensionValue(row.dimensions[column.key]) || '—')}</td>)}
              {valueHeader && <td>{row.text}{row.unit ? ` ${row.unit}` : ''}</td>}
              <td>{focusLabel(row)}</td>
              <td><span className={`result-status ${row.status}`}>{row.statusLabel}</span></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function FactGroups({ rows, selectedId, onSelect }: { rows: ResultRow[]; selectedId?: string; onSelect: (row: ResultRow) => void }) {
  const grouped = rows.some((row) => dimensionValue(row.dimensions.workface))
  const groups = grouped
    ? [...rows.reduce((map, row) => {
      const face = dimensionValue(row.dimensions.workface) || '未标明工作面'
      map.set(face, [...(map.get(face) || []), row])
      return map
    }, new Map<string, ResultRow[]>())]
    : [['', rows] as [string, ResultRow[]]]
  return (
    <>
      {groups.map(([face, group]) => {
        const columns = activeDimensionKeys(group, grouped ? ['workface'] : []).map((key) => ({ key, label: dimensionNames[key] || key, from: 'dimension' as const }))
        return (
          <section className="result-group" key={face || 'all'}>
            {face && <h4>工作面 {face}</h4>}
            <ResultTable rows={group} columns={columns} selectedId={selectedId} onSelect={onSelect} />
          </section>
        )
      })}
    </>
  )
}

function FactCard({ row, selected, onSelect }: { row: ResultRow; selected: boolean; onSelect: (row: ResultRow) => void }) {
  const identity = dimensionEntries(row.dimensions)
  return (
    <article className={`result-card ${selected ? 'selected' : ''}`}>
      <div className="evidence-value">{row.text}{row.unit ? ` ${row.unit}` : ''}</div>
      {!!identity.length && <p className="result-identity">{identity.map(([key, value]) => `${key} ${value}`).join(' · ')}</p>}
      <p className="result-meta">
        {row.kind && row.kind !== 'unknown' && <span>值类型 {valueKindNames[row.kind] || row.kind}</span>}
        <span className={`result-status ${row.status}`}>{row.statusLabel}</span>
        <Button type="link" size="small" disabled={!row.page} onClick={() => onSelect(row)}>{selected ? '正在查看原文' : '在原文中查看'} · {focusLabel(row)}</Button>
      </p>
      {row.quote && <blockquote>{row.quote}</blockquote>}
    </article>
  )
}

function Passages({ rows, selectedId, onSelect }: { rows: ResultRow[]; selectedId?: string; onSelect: (row: ResultRow) => void }) {
  return (
    <div className="passage-list">
      {rows.map((row) => {
        const identity = dimensionEntries(row.dimensions).map(([key, value]) => `${key} ${value}`).join(' · ')
        return (
          <button type="button" key={row.id} className={`result-passage ${selectedId === row.evidenceId ? 'selected' : ''}`} onClick={() => onSelect(row)}>
            {row.polarity && <Tag>{polarityNames[row.polarity] || row.polarity}</Tag>}
            <span className={`result-status ${row.status}`}>{row.statusLabel}</span>
            <p>{row.text}</p>
            <span>{[identity, row.page ? `第 ${row.page} 页` : ''].filter(Boolean).join(' · ') || '未标明位置'}</span>
          </button>
        )
      })}
    </div>
  )
}

export function exportResult(result: ParseResultResponse) {
  const blob = new Blob([JSON.stringify(result, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = `解析结果-${result.parse_run_id}.json`
  link.click()
  URL.revokeObjectURL(url)
}

interface Props {
  fileName?: string
  fileUrl?: string
  result?: ParseResultResponse
  extra?: React.ReactNode
}

export default function ParseWorkspace({ fileName, fileUrl, result, extra }: Props) {
  const [selected, setSelected] = useState<string>()
  const [selectedOccurrence, setSelectedOccurrence] = useState<number>()
  const [reviewPage, setReviewPage] = useState<number>()
  const [reviewEvidence, setReviewEvidence] = useState<FactEvidence>()
  const [selectedEvidenceId, setSelectedEvidenceId] = useState<string>()
  const [selectionRequest, setSelectionRequest] = useState(0)
  const [factView, setFactView] = useState<'automatic' | 'reviewed'>('automatic')
  const [reviewedFacts, setReviewedFacts] = useState<DownstreamFact[]>([])
  const [automaticFacts, setAutomaticFacts] = useState<DownstreamFact[]>()
  const [reviewedLoading, setReviewedLoading] = useState(false)
  const [reviewedError, setReviewedError] = useState('')
  const [reviewedRefresh, setReviewedRefresh] = useState(0)
  const [assistantOpen, setAssistantOpen] = useState(false)
  const [assistantStarted, setAssistantStarted] = useState(false)
  const [assistantEvidence, setAssistantEvidence] = useState<FactEvidence[]>([])
  const [assistantSelectedEvidence, setAssistantSelectedEvidence] = useState<FactEvidence>()
  const assistantRef = useRef<HTMLDivElement>(null)
  const [execution, setExecution] = useState<ExecutionResponse>()
  const [fieldQuery, setFieldQuery] = useState('')
  const [fieldFilter, setFieldFilter] = useState<'all' | 'review' | 'ready'>('all')
  const [evidenceTab, setEvidenceTab] = useState('facts')
  const [evidenceOpen, setEvidenceOpen] = useState(false)
  const extracted = result?.fields || []
  const fields: ExtractedField[] = useMemo(() => {
    const found = result?.fields || []
    const missing = (result?.missing_fields || []).filter((item) => !found.some((value) => value.field_key === item.field_key))
    return [...found, ...missing.map((item) => ({
      field_key: item.field_key, field_name: item.field_name || item.field_key, field_value: null,
      required: item.required, data_type: item.data_type, unit: item.unit,
      validation_status: 'missing', extraction_method: 'none', extraction_detail: item,
    }))]
  }, [result?.fields, result?.missing_fields])
  const field = fields.find((item) => item.field_key === selected) || fields[0]
  const visibleFields = fields.filter((item) => {
    const query = fieldQuery.trim().toLowerCase()
    const matchesQuery = !query || `${item.field_name} ${item.field_key} ${fieldDisplayValue(item, profileRules(result), result?.facts || [])}`.toLowerCase().includes(query)
    const review = fieldStatus(item).review
    return matchesQuery && (fieldFilter === 'all' || (fieldFilter === 'review' ? review : !review))
  })
  const displayStatus = execution && !execution.legacy ? execution.status : result?.status || ''
  const active = ['PENDING', 'QUEUED', 'RUNNING', 'PROCESSING', 'RETRYING', 'WAITING_QUOTA', 'WAITING_SERVICE'].includes(displayStatus)
  const reliable = result?.contract_version === 'reliable-extraction-v2'
  const completed = execution && !execution.legacy ? execution.automatic_complete : !reliable && displayStatus === 'COMPLETED'
  const footerStatusClass = execution?.automatic_complete === true ? '' : displayStatus === 'FAILED' ? 'offline' : ['CANCELLED', 'SUPERSEDED'].includes(displayStatus) || !result ? 'neutral' : 'checking'
  useEffect(() => {
    if (!assistantOpen) return
    assistantRef.current?.scrollIntoView({ block: 'start', behavior: 'auto' })
  }, [assistantOpen])
  useEffect(() => {
    setSelected(undefined)
    setFieldQuery('')
    setFieldFilter('all')
    setEvidenceTab('facts')
    setEvidenceOpen(false)
    setSelectedOccurrence(undefined)
    setReviewPage(undefined)
    setReviewEvidence(undefined)
    setSelectedEvidenceId(undefined)
    setFactView('automatic')
    setReviewedFacts([])
    setAutomaticFacts(undefined)
    setReviewedError('')
    setAssistantOpen(false)
    setAssistantStarted(false)
    setAssistantEvidence([])
    setAssistantSelectedEvidence(undefined)
    setExecution(undefined)
  }, [result?.parse_run_id])

  const evidence = field?.evidence
  const matchScore = (value?: number | null) => (value == null ? '未知' : value.toFixed(3))
  const requiredCount = extracted.filter((item) => item.required && (item.field_value?.trim() || item.instances?.length)).length
  const instances = field?.instances || field?.extraction_detail?.instances || []
  const hasExtractedValue = (item: ExtractedField) => {
    const status = item.validation_status || item.extraction_detail?.validation_status
    return (item.instances?.length || item.extraction_detail?.instances?.length || 0) > 0
      || (item.field_value != null && !['missing', 'blocked', 'incomplete', 'not_found'].includes(status || ''))
  }
  const extractedCount = extracted.filter(hasExtractedValue).length
  const occurrence = selectedOccurrence == null ? undefined : instances[selectedOccurrence]
  const candidates = field?.extraction_detail?.fusion?.candidate_groups || []
  const rejected = field?.extraction_detail?.rejected_candidates || field?.extraction_detail?.fusion?.rejected_candidates || []
  const runtime = result?.extraction_profile?.agent_runtime
  const visitedPages = runtime?.coverage?.visited_pages || runtime?.coverage?.viewed_pages || []
  const allAutomaticFacts = automaticFacts || result?.facts || []
  const fieldFacts: DownstreamFact[] = (factView === 'reviewed' ? reviewedFacts : allAutomaticFacts).filter((fact) => fact.field_key === field?.field_key)
  const readingRule = field ? profileRule(field, profileRules(result)) : undefined
  const readingKind = field ? targetKind(field, readingRule) : 'fact'
  const readingRows = field ? buildRows(field, fieldFacts) : []
  const readingMulti = readingRule ? multiConfigured(readingRule) : readingRows.length > 1
  const selectReading = (row: ResultRow) => {
    setSelectionRequest((value) => value + 1)
    setSelectedOccurrence(undefined)
    setReviewEvidence(undefined)
    setSelectedEvidenceId(row.evidenceId)
    setReviewPage(row.page || undefined)
  }
  const evidenceRegistry = field?.extraction_detail?.evidence_registry || {}
  const legacyEvidence: FactEvidence[] = Object.entries(evidenceRegistry).map(([id, value]) => ({ ...value, evidence_id: id }))
  const instanceEvidence: FactEvidence[] = instances.map((item, index) => located({
    evidence_id: `instance-${index}`, page_number: item.page_num, bbox: item.bbox,
    coordinate_system: item.coordinate_system, coordinate_origin: item.coordinate_origin,
    page_rotation: item.page_rotation, image_width: item.image_width, image_height: item.image_height,
    location_status: item.location_status, quote: item.context, role: 'value',
  })).filter((item) => !!item.page_number)
  const fieldEvidence: FactEvidence[] = evidence?.page_number ? [located({ ...evidence.locator,
    evidence_id: 'field-evidence', page_number: evidence.page_number, quote: evidence.quote, role: 'value',
    location_status: evidence.location_status,
    bbox: evidence.location_status && evidence.location_status !== 'unavailable' ? evidence.locator?.bbox : null,
  })] : []
  const factEvidence: FactEvidence[] = fieldFacts.flatMap((fact) => fact.evidence.map((item, index) => located({ ...item, evidence_id: item.evidence_id || `fact-evidence-${fact.fact_id}-${index}` })))
  // 列表按引句合并，预览保留证据身份，避免事实行选中的 ID 被去重丢掉。
  const previewEvidence: FactEvidence[] = [...(reviewEvidence ? [reviewEvidence] : []), ...assistantEvidence, ...factEvidence, ...(factView === 'automatic' ? [...legacyEvidence, ...instanceEvidence, ...fieldEvidence] : [])]
    .filter((item, index, all) => item.evidence_id ? all.findIndex((candidate) => candidate.evidence_id === item.evidence_id && candidate.role === item.role) === index : true)
  const sourceEvidence = sourceCards(previewEvidence)
  const selectedFact = fieldFacts.find((fact) => fact.evidence.some((item) => item.evidence_id === selectedEvidenceId))
  const loadReviewedFacts = () => setReviewedRefresh((value) => value + 1)
  const downloadFacts = async (format: 'json' | 'csv') => {
    if (!result) return
    try {
      const data = await exportRunFacts(result.parse_run_id, factView, format)
      const url = URL.createObjectURL(data)
      const link = document.createElement('a'); link.href = url; link.download = `事实-${factView}-${result.parse_run_id}.${format}`; link.click(); URL.revokeObjectURL(url)
    } catch (failure) { message.error(apiError(failure, '导出事实失败')) }
  }
  useEffect(() => {
    if (!result?.parse_run_id || !result.facts) return
    let disposed = false
    setReviewedLoading(true); setReviewedError(''); setReviewedFacts([])
    void getRunFacts(result.parse_run_id, factView).then((value) => {
      if (disposed) return
      if (factView === 'automatic') setAutomaticFacts(value.facts)
      else setReviewedFacts(value.facts)
    })
      .catch((failure) => { if (!disposed) setReviewedError(apiError(failure, '读取人工事实失败')) })
      .finally(() => { if (!disposed) setReviewedLoading(false) })
    return () => { disposed = true }
  }, [result?.parse_run_id, result?.facts, factView, reviewedRefresh])

  const renderField = (item: ExtractedField, index: number) => (
    <button
      className={`field-row ${field?.field_key === item.field_key ? 'selected' : ''}`}
      key={item.field_key}
      onClick={() => { setSelected(item.field_key); setEvidenceOpen(true); setSelectedOccurrence(undefined); setReviewPage(undefined); setReviewEvidence(undefined); setSelectedEvidenceId(undefined) }}
      aria-pressed={field?.field_key === item.field_key}
    >
      <span>{index + 1}</span>
      <span className="field-name-cell" title={item.field_name}><strong>{item.field_name}{item.required && <em aria-label="必填"> *</em>}</strong><small>{methodNames[item.extraction_method || ''] || item.extraction_method || '提取方法未知'}</small></span>
      <span title={fieldDisplayValue(item, profileRules(result), allAutomaticFacts)}>{fieldDisplayValue(item, profileRules(result), allAutomaticFacts)}</span>
      <span>{methodNames[item.extraction_method || ''] || item.extraction_method || '—'}</span>
      <span>{matchScore(item.confidence)}</span>
      <span
        className={`field-status ${fieldStatus(item).review ? 'review' : 'done'}`}
      >
        <i />
        {fieldStatus(item).label}
      </span>
    </button>
  )

  return (
    <>
      <section className="document-strip">
        <div className="document-identity">
          <div className="pdf-badge">
            <FilePdfOutlined />
          </div>
          <div>
            <strong title={fileName}>{fileName || '尚未选择文档'}</strong>
            <span>
              {result
                ? `${result.mine_name || '未填写煤矿'} · ${result.checklist_type || '未标注清单'} · ${
                    result.reporting_period || '未填写周期'
                  }${result.duration_seconds != null ? ` · 耗时 ${result.duration_seconds.toFixed(1)} 秒` : ''}`
                : '上传 PDF 后，在此查看原文与解析结果'}
            </span>
          </div>
        </div>
        <Steps
          size="small"
          current={completed ? 3 : result ? 1 : fileUrl ? 0 : -1}
          status={result?.status === 'FAILED' ? 'error' : 'process'}
          items={[
            { title: '文档上传' },
            { title: '异步解析' },
            { title: '字段抽取' },
            { title: '结果核对' },
          ]}
        />
        <Tag color={completed ? 'green' : displayStatus === 'FAILED' ? 'red' : active ? 'blue' : 'gold'}>
          {result ? reliable && !execution ? '完整性待核验' : execution && !execution.legacy ? executionStatusNames[displayStatus] || displayStatus : statusNames[displayStatus] || displayStatus : '待上传'}
        </Tag>
        {result && <Button size="small" type={assistantOpen ? 'default' : 'primary'} onClick={() => { setAssistantStarted(true); setAssistantOpen((value) => !value) }} aria-pressed={assistantOpen}>{assistantOpen ? '返回提取核对' : '文档 AI 助手'}</Button>}
      </section>

      {assistantStarted && result && <div className="assistant-workspace" ref={assistantRef} style={!assistantOpen ? { display: 'none' } : undefined}>
        <section className="preview-panel"><div className="panel-bar"><strong>附件原文</strong>{fileUrl && <a href={fileUrl} target="_blank" rel="noreferrer">打开原文</a>}</div><div className="assistant-paper-stage">{fileUrl ? <PdfPreview url={fileUrl} evidence={assistantEvidence} evidencePage={assistantSelectedEvidence?.page_number || undefined} selectedEvidenceId={assistantSelectedEvidence?.evidence_id} /> : <Empty description="当前附件没有可预览的 PDF" />}</div></section>
        <AssistantErrorBoundary key={result.document_id}><AssistantPanel documentId={result.document_id} parseRunId={result.parse_run_id} onCitations={(citations, selected) => {
          setAssistantEvidence(citations)
          setAssistantSelectedEvidence(selected)
          setReviewEvidence(selected)
          setSelectedEvidenceId(selected?.evidence_id)
          setReviewPage(selected?.page_number || undefined)
        }} /></AssistantErrorBoundary>
      </div>}

      {!assistantOpen && <div className="parse-results-shell">
      {result?.status === 'FAILED' && (
        <Alert
          className="run-alert"
          type="error"
          showIcon
          message="解析未完成"
          description={result.error_message || '请检查解析服务日志后重新提交。'}
        />
      )}
      {active && (
        <Alert
          className="run-alert"
          type="info"
          showIcon
          message="规则仍在处理中"
          description="请以执行账本的范围与候选状态核验完整性；当前已发现值不能代表全部规则已完成。"
        />
      )}
      {completed && result?.quality_status && result.quality_status !== 'QUALITY_OK' && (
        <Alert className="run-alert" type="warning" showIcon message="解析已完成，结果需要核对"
          description={result.quality_status === 'FAILED' ? '未获得可用字段，请检查原文与提取规则。' : '请核对待核对字段，以及各工作面、单元或表格行上的取值。'} />
      )}
      {!!result?.warnings?.length && (
        <Alert className="run-alert" type="warning" showIcon message="解析提示"
          description={<ul>{result.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul>} />
      )}
      <details className="run-records">
      <summary>解析进度与运行记录 <span>查看处理范围、执行状态与模型调用</span></summary>
      {result && (result.artifact_id || result.mode || result.parent_parse_run_id) && (
        <Alert className="run-alert" type="info" showIcon message="文档前处理结果已关联"
          description={<>
            Artifact：{result.artifact_id || '待关联'} · 状态：{result.artifact_status || '—'} · 复用状态：{result.cache_status || '—'}
            {result.mode && ` · 提取模式：${result.mode}`}
            {result.parent_parse_run_id && ` · 父任务：${result.parent_parse_run_id}`}
          </>} />
      )}
      {runtime && <Alert className="run-alert" type="info" showIcon message="清单智能体执行记录"
        description={<>
          <p>引擎：{runtime.engine || '—'} · 模型：{runtime.model || '—'} · 模型版本：{runtime.model_version || '未声明'} · 执行状态：{runtime.status || '—'}</p>
          <p>已查看页面 {visitedPages.length} · 未查看页面 {runtime.coverage?.unvisited_pages?.length ?? '—'}。执行完成不表示全文查全或取值正确。</p>
          <p>源文核验：UNVERIFIED（未核验） · 字段核验：UNVERIFIED（未核验）</p>
          <details><summary>覆盖范围、调用预算与工具轨迹</summary><pre style={{ whiteSpace: 'pre-wrap', maxHeight: 300, overflow: 'auto' }}>
            {JSON.stringify({ coverage: runtime.coverage, field_coverage: runtime.field_coverage, budget: runtime.budget, token_usage: runtime.token_usage, trace: runtime.trace, failures: runtime.failures }, null, 2)}
          </pre></details>
        </>} />}
      {result?.parse_run_id && <ExecutionPanel runId={result.parse_run_id} fields={fields} onChange={setExecution} />}
      </details>

      <div className="workspace-three" style={assistantOpen ? { display: 'none' } : undefined}>
        <section className="preview-panel">
          <div className="panel-bar">
            <strong>文档预览</strong>
            {fileUrl && (
              <a href={fileUrl} target="_blank" rel="noreferrer">
                <LinkOutlined /> 打开原文
              </a>
            )}
          </div>
          <div className="real-paper-stage">
            {fileUrl ? (
              <PdfPreview
                url={fileUrl}
                evidencePage={reviewPage || occurrence?.page_num || evidence?.page_number}
                evidence={previewEvidence}
                selectedEvidenceId={selectedEvidenceId}
                selectionRequest={selectionRequest}
              />
            ) : (
              <div className="preview-empty">
                <FileSearchOutlined />
                <h3>从一份真实文档开始</h3>
                <p>
                  选择 PDF，上传前即可预览原文。
                  <br />
                  解析后，字段与证据会在右侧出现。
                </p>
                {extra}
              </div>
            )}
          </div>
        </section>

        <div className="results-side">
        <section className="fields-panel">
          <div className="panel-bar">
            <div>
              <strong>字段抽取结果</strong>
              <span>{fields.length ? `共 ${fields.length} 个字段 · 显示 ${visibleFields.length}` : '等待解析'}</span>
            </div>
            {result && completed && (
              <Button size="small" icon={<DownloadOutlined />} onClick={() => exportResult(result)}>
                导出
              </Button>
            )}
          </div>

          <div className="field-toolbar">
            <Input size="small" allowClear prefix={<SearchOutlined />} placeholder="搜索字段、字段键或抽取值" value={fieldQuery} onChange={(event) => setFieldQuery(event.target.value)} />
            <Segmented size="small" value={fieldFilter} options={[{ label: '全部', value: 'all' }, { label: '待核对', value: 'review' }, { label: '已就绪', value: 'ready' }]} onChange={(value) => setFieldFilter(value as typeof fieldFilter)} />
          </div>
          <div className="result-summary">
            <span className="green-dot" />
            必填命中 {requiredCount}
            <span className="amber-dot" />
            已发现 {extractedCount} · 可供下游使用 {result?.facts ? new Set(result.facts.filter((fact) => fact.available_for_downstream === true).map((fact) => fact.field_key)).size : '未评估'} · 缺失 {fields.filter((item) => ['missing', 'blocked', 'incomplete', 'not_found'].includes(item.validation_status || '')).length}
            <span className="summary-confidence">平均方法匹配分 {matchScore(result?.mean_confidence)}</span>
          </div>

          <div className="field-table">
            <div className="field-head">
              <span>#</span>
              <span>字段名称</span>
              <span>抽取值</span>
              <span>提取方法</span>
              <span>方法匹配分</span>
              <span>核对状态</span>
            </div>
            {visibleFields.map((item) => renderField(item, fields.indexOf(item)))}
            {!!fields.length && !visibleFields.length && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有匹配的字段，请调整搜索或筛选条件" />}
          </div>

          {!fields.length && (
            <div className="workspace-empty">
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description={
                  completed ? '解析完成，但未提取到字段' : '上传并解析后显示真实字段'
                }
              />
            </div>
          )}

          <details className="field-scope">
            <summary>当前提取范围与方法说明</summary>
            <div>
              {fields.length ? (
                fields.map((item) => (
                  <span
                    key={item.field_key}
                    className={fieldStatus(item).review ? '' : 'scope-found'}
                  >
                    {item.field_name}
                    {item.required && ' *'}
                  </span>
                ))
              ) : (
                <span>按所选清单的规则配置提取，规则可在「规则管理」中调整。</span>
              )}
            </div>
            <p>方法匹配分仅反映提取路径匹配程度，未经正确率校准，不能作为字段正确概率。</p>
          </details>
        </section>

        <details className="evidence-panel" open={evidenceOpen} onToggle={(event) => setEvidenceOpen(event.currentTarget.open)}>
          <summary className="panel-bar">
            <strong>证据详情</strong>
            <span className="evidence-selection">{field?.field_name || '未选择字段'}</span>
            <span className="evidence-toggle">{evidenceOpen ? '收起详情' : '展开详情'}</span>
          </summary>
          {field ? (
            <>
              <div className="evidence-title">
                <strong>{field.field_name}</strong>
                <Tag>{targetKindNames[readingKind]}</Tag>
                {readingMulti && readingKind !== 'table' && <Tag color="purple">多实例</Tag>}
                <Tag color={fieldStatus(field).review ? 'gold' : 'blue'}>{fieldStatus(field).label}</Tag>
              </div>
              <Tabs className="evidence-tabs" activeKey={evidenceTab} onChange={setEvidenceTab} items={[
                { key: 'facts', label: '事实结果' }, { key: 'source', label: '原文证据' },
                { key: 'review', label: '人工复核' }, { key: 'trace', label: '提取追溯' },
              ]} />
              <div className="evidence-content">
                <div hidden={evidenceTab !== 'facts'}>
                {!readingRows.length && <p className="muted result-empty">{emptyFieldText(field)}</p>}
                {!!readingRows.length && readingKind === 'fact' && !readingMulti && (
                  <>
                    <FactCard row={readingRows.find((row) => row.evidenceId === selectedEvidenceId) || readingRows[0]} selected={selectedEvidenceId === (readingRows.find((row) => row.evidenceId === selectedEvidenceId) || readingRows[0]).evidenceId} onSelect={selectReading} />
                    {readingRows.length > 1 && (
                      <>
                        <p className="muted">规则配置为单实例，结果里还有其他条。点一行可在原文中定位。</p>
                        <FactGroups rows={readingRows} selectedId={selectedEvidenceId} onSelect={selectReading} />
                      </>
                    )}
                  </>
                )}
                {!!readingRows.length && readingKind === 'fact' && readingMulti && (
                  <>
                    <p className="muted">点一行，在左侧原文标出这一条的位置。</p>
                    <FactGroups rows={readingRows} selectedId={selectedEvidenceId} onSelect={selectReading} />
                  </>
                )}
                {!!readingRows.length && readingKind === 'statement' && (
                  <>
                    <p className="muted">点一段原文，在左侧标出它所在的位置。</p>
                    <Passages rows={readingRows} selectedId={selectedEvidenceId} onSelect={selectReading} />
                  </>
                )}
                {!!readingRows.length && readingKind === 'table' && (
                  <>
                    <p className="muted">一行对应原文中的一行。点一行，在左侧标出位置。</p>
                    <ResultTable
                      rows={readingRows}
                      valueHeader={tableColumns(readingRule, readingRows).length ? '' : '内容'}
                      selectedId={selectedEvidenceId}
                      onSelect={selectReading}
                      columns={tableColumns(readingRule, readingRows)}
                    />
                  </>
                )}
                {(readingKind === 'table' || (readingKind === 'fact' && readingMulti)) && readingRows.find((row) => row.evidenceId === selectedEvidenceId)?.quote && (
                  <blockquote>{readingRows.find((row) => row.evidenceId === selectedEvidenceId)?.quote}</blockquote>
                )}

                </div>
                <div hidden={evidenceTab !== 'source'}>
                  <p className="muted">同一页、同一句原文只列一次。点「查看第几页」可定位原文；可匹配的引句会高亮，区域坐标显示为取证范围。</p>
                  {!sourceEvidence.length && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前字段未返回原文证据，请对照 PDF 核验" />}
                  <div className="source-evidence-list">{sourceEvidence.map((ref, index) => <article key={`${ref.evidence_id || index}-${ref.role}`} className={`source-evidence ${ref.evidence_id === selectedEvidenceId ? 'selected' : ''}`}>
                    <div className="source-evidence-heading"><strong>{({ value: '数值证据', unit: '单位证据', header: '表头证据', scope: '范围证据', dimension: '维度证据', object: '对象证据' } as Record<string, string>)[ref.role || 'value'] || ref.role}</strong>
                      <Button size="small" type="link" disabled={!ref.page_number} onClick={() => { setSelectionRequest((value) => value + 1); setReviewEvidence(undefined); setSelectedEvidenceId(ref.evidence_id); setReviewPage(ref.page_number || undefined); setSelectedOccurrence(undefined) }}>查看第 {ref.page_number || '—'} 页</Button></div>
                    {ref.quote ? <blockquote>{ref.quote}</blockquote> : <p className="muted">未返回原文引句，可按页查看。</p>}
                    <p className="muted">{ref.bbox && ref.coordinate_system ? '已返回区域坐标，请结合原文核对' : '页级证据 · 未提供精确定位'}</p>
                  </article>)}</div>
                </div>
                <div hidden={evidenceTab !== 'trace'}>
                <details className="audit-fold" open>
                  <summary>核对依据</summary>
                  <div className="audit-body">
                    <Segmented size="small" value={factView} options={[{ label: '自动结果', value: 'automatic' }, { label: '人工修订', value: 'reviewed' }]} onChange={(value) => {
                      setFactView(value as 'automatic' | 'reviewed'); setSelectedEvidenceId(undefined); setReviewEvidence(undefined)
                      if (value === 'reviewed') void loadReviewedFacts()
                    }} />
                    <div className="audit-actions"><Button size="small" onClick={() => void downloadFacts('json')}>导出 JSON</Button><Button size="small" onClick={() => void downloadFacts('csv')}>导出 CSV</Button></div>
                    {reviewedLoading && <p className="muted">读取人工修订…</p>}
                    {factView === 'reviewed' && reviewedError && <Alert type="error" showIcon message={reviewedError} action={<Button size="small" onClick={() => void loadReviewedFacts()}>重试</Button>} />}
                    {factView === 'reviewed' && !reviewedLoading && !reviewedError && !readingRows.length && <p className="muted">该字段没有可展示的人工修订。</p>}
                    <div className="evidence-meta">
                      <span>方法匹配分<strong>{matchScore(field.confidence)}</strong></span>
                      <span>提取方法<strong>{methodNames[field.extraction_method || ''] || field.extraction_method || '—'}</strong></span>
                    </div>
                    {!!field.extraction_detail?.incomplete_reasons?.length && <ul className="audit-list">{field.extraction_detail.incomplete_reasons.map((code) => <li key={code}>{incompleteReasonNames[code] || code}</li>)}</ul>}
                    {field.extraction_detail?.retrieval && <p className="muted">候选 {field.extraction_detail.retrieval.candidates_total ?? '—'} · 已接受 {field.extraction_detail.retrieval.accepted ?? '—'} · 未通过 {field.extraction_detail.retrieval.rejected ?? '—'}</p>}
                    {!!candidates.length && <p className="muted">多路径候选：{candidates.map((candidate) => candidate.value).join('、')}</p>}
                    {!!rejected.length && <p className="muted">未通过校验：{rejected.map((candidate) => candidate.value).join('、')}</p>}
                    {selectedFact && <p className="muted">当前事实 {selectedFact.fact_id} · 可供下游 {selectedFact.available_for_downstream == null ? '未评估' : selectedFact.available_for_downstream ? '是' : '否'}</p>}
                    {selectedEvidenceId && !readingRows.find((row) => row.evidenceId === selectedEvidenceId)?.page && <p className="muted">选中条目没有页码，原文上不画框。</p>}
                  </div>
                </details>
                <details className="audit-fold"><summary>完整事实、质量风险与覆盖记录</summary><pre className="trace-json">{JSON.stringify({ facts: fieldFacts, extraction_detail: field.extraction_detail, evidence: field.evidence }, null, 2)}</pre></details>
                </div>
                <div hidden={evidenceTab !== 'review'}>
                {result && field && ['COMPLETED', 'COMPLETED_WITH_REVIEW', 'FAILED'].includes(displayStatus) && <HumanReviewPanel result={result} field={field} onPage={(page) => { setReviewPage(page); setSelectedOccurrence(undefined) }} onEvidence={(ref) => {
                  const id = ref.evidence_id || 'review-evidence'
                  setReviewEvidence({ ...ref, evidence_id: id }); setReviewPage(ref.page_number || undefined); setSelectedEvidenceId(id)
                }} onSaved={loadReviewedFacts} />}
                {(!result || !['COMPLETED', 'COMPLETED_WITH_REVIEW', 'FAILED'].includes(displayStatus)) && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前执行状态尚未开放人工复核" />}
                </div>
              </div>
            </>
          ) : (
            <div className="evidence-empty">
              <FileSearchOutlined />
              <h3>选中字段，查看证据</h3>
              <p>
                原文引用、页码和提取方法
                <br />
                在这里集中核对。
              </p>
            </div>
          )}
        </details>
        </div>
      </div>

      <div className="workspace-bottom">
        <span>
          <span className={`health-dot ${footerStatusClass}`} />
          {completed ? '解析结果已保存' : result ? statusNames[displayStatus] || displayStatus : '准备上传'}
        </span>
        <span>文档前处理 → 清单知识与按需取证 → 同值归并与多来源证据 → 核对后使用</span>
        <span>提取信息，不作达标判断</span>
      </div>
      </div>}
    </>
  )
}
