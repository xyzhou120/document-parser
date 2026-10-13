import { useEffect, useMemo, useState } from 'react'
import { Alert, Button, Empty, Segmented, Steps, Tag, message } from 'antd'
import {
  DownloadOutlined,
  FilePdfOutlined,
  FileSearchOutlined,
  LinkOutlined,
} from '@ant-design/icons'
import type { DownstreamFact, ExtractedField, FactEvidence, ParseResultResponse } from '@/types/api'
import PdfPreview from './PdfPreview'
import HumanReviewPanel from './HumanReviewPanel'
import { exportRunFacts, getRunFacts } from '@/services/parse'
import { apiError } from '@/services/api'
import ExecutionPanel from './ExecutionPanel'
import AssistantPanel from './AssistantPanel'
import type { ExecutionResponse } from '@/types/api'
import { executionStatusNames } from '@/services/execution'

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

/** 校验状态 -> 展示样式 */
const validationNames: Record<string, { label: string; color: string }> = {
  passed: { label: '校验通过', color: 'green' },
  fallback_used: { label: '使用兜底值', color: 'gold' },
  missing: { label: '未提取到', color: 'red' },
  out_of_range: { label: '超出范围', color: 'orange' },
  outlier: { label: '偏离典型区间', color: 'gold' },
  invalid_format: { label: '格式不符', color: 'orange' },
  blacklisted: { label: '命中黑名单', color: 'volcano' },
  not_a_number: { label: '非数值', color: 'orange' },
  conflict: { label: '候选值冲突', color: 'red' },
  multiple_values: { label: '多条候选事实', color: 'orange' },
  multiple_occurrences: { label: '多处证据', color: 'blue' },
  low_confidence: { label: '待核对', color: 'gold' },
  review_required: { label: '待人工核对', color: 'gold' },
  blocked: { label: '已阻断，待处理', color: 'red' },
  unresolved: { label: '未确认，需补充取证', color: 'gold' },
  // Plan B statuses (2026-10-10): candidate unconfirmed, searched and absent, unfinished search.
  partial_candidates: { label: '已有候选，未完成核对', color: 'gold' },
  not_found: { label: '范围内未找到', color: 'orange' },
  incomplete: { label: '未完成（额度/轮次/输出异常）', color: 'red' },
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

function fieldStatus(item: ExtractedField) {
  const status = item.validation_status || item.extraction_detail?.validation_status
  const groups = item.extraction_detail?.value_groups
  if (groups?.length) return { label: `${groups.length} 组事实已发现`, review: false }
  if ((item.instances?.length || 0) > 1 && (!status || status === 'passed' || status === 'multiple_occurrences')) {
    return { label: '多条事实已发现', review: false }
  }
  if (status && status !== 'passed') return { label: validationNames[status]?.label || status, review: true }
  if (item.field_value == null || !item.field_value.trim()) return { label: '未提取到', review: true }
  // confidence is a method-match score, not a correctness probability. Every value remains reviewable.
  return { label: '已发现', review: false }
}

const typeNames: Record<string, string> = {
  string: '字符串',
  integer: '整数',
  float: '浮点数',
  date: '日期',
  array: '明细',
  json: 'JSON',
}

const valueKindNames: Record<string, string> = {
  original: '原始', design: '设计', actual: '实际', calculated: '计算', measured: '实测',
  threshold: '阈值', reported: '报告记录', unknown: '未知',
}

const bindingNames: Record<string, string> = { bound: '已绑定', unknown: '待核对', unverified: '未核验', partial: '部分绑定', not_required: '无需绑定' }
const qualityIssueNames: Record<string, string> = {
  UNIT_UNVERIFIED: '单位未核验', UNIT_MISMATCH: '单位不匹配', UNBOUND_DIMENSION: '维度未绑定',
  VALUE_KIND_UNVERIFIED: '值类型未核验', ROLE_UNVERIFIED: '角色未核验',
}

function fieldDisplayValue(item: ExtractedField) {
  const groups = item.extraction_detail?.value_groups
  if (groups?.length) return groups.length === 1 ? `${groups[0].value ?? '不可确认'}${groups[0].unit ? ` ${groups[0].unit}` : ''}` : `${groups.length} 组值（保留不同身份与不同值）`
  const status = item.validation_status || item.extraction_detail?.validation_status
  if (item.field_value != null) return item.field_value
  const candidateCount = item.instances?.length || item.extraction_detail?.instances?.length || 0
  if (status === 'multiple_values' || candidateCount > 1) return `${candidateCount} 条候选事实`
  // Plan B: say why there is no value instead of one generic "未提取到".
  if (status === 'partial_candidates') return `${candidateCount} 条候选未确认`
  if (status === 'not_found') return '未找到'
  if (status === 'incomplete') return '未完成'
  return '未提取到'
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
  const [execution, setExecution] = useState<ExecutionResponse>()
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
  const displayStatus = execution && !execution.legacy ? execution.status : result?.status || ''
  const active = ['PENDING', 'QUEUED', 'RUNNING', 'PROCESSING', 'RETRYING', 'WAITING_QUOTA', 'WAITING_SERVICE'].includes(displayStatus)
  const reliable = result?.contract_version === 'reliable-extraction-v2'
  const completed = execution && !execution.legacy ? execution.automatic_complete : !reliable && displayStatus === 'COMPLETED'
  const footerStatusClass = execution?.automatic_complete === true ? '' : displayStatus === 'FAILED' ? 'offline' : ['CANCELLED', 'SUPERSEDED'].includes(displayStatus) || !result ? 'neutral' : 'checking'
  useEffect(() => {
    setSelected(undefined)
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
  const validationStatus = field?.validation_status || field?.extraction_detail?.validation_status
  const validation = validationStatus ? validationNames[validationStatus] : undefined
  const candidates = field?.extraction_detail?.fusion?.candidate_groups || []
  const rejected = field?.extraction_detail?.rejected_candidates || field?.extraction_detail?.fusion?.rejected_candidates || []
  const valueGroups = field?.extraction_detail?.value_groups || []
  const runtime = result?.extraction_profile?.agent_runtime
  const visitedPages = runtime?.coverage?.visited_pages || runtime?.coverage?.viewed_pages || []
  const allAutomaticFacts = automaticFacts || result?.facts || []
  const fieldFacts: DownstreamFact[] = (factView === 'reviewed' ? reviewedFacts : allAutomaticFacts).filter((fact) => fact.field_key === field?.field_key)
  const evidenceRegistry = field?.extraction_detail?.evidence_registry || {}
  const legacyEvidence: FactEvidence[] = Object.entries(evidenceRegistry).map(([id, value]) => ({ ...value, evidence_id: id }))
  const instanceEvidence: FactEvidence[] = instances.map((item, index) => ({
    evidence_id: `instance-${index}`, page_number: item.page_num, bbox: item.bbox,
    coordinate_system: item.coordinate_system, coordinate_origin: item.coordinate_origin,
    page_rotation: item.page_rotation, image_width: item.image_width, image_height: item.image_height,
    location_status: item.location_status, quote: item.context, role: 'value',
  })).filter((item) => !!item.page_number)
  const fieldEvidence: FactEvidence[] = evidence?.page_number ? [{ ...evidence.locator,
    evidence_id: 'field-evidence', page_number: evidence.page_number, quote: evidence.quote, role: 'value',
    location_status: evidence.location_status,
    bbox: evidence.location_status && evidence.location_status !== 'unavailable' ? evidence.locator?.bbox : null,
  }] : []
  const factEvidence: FactEvidence[] = fieldFacts.flatMap((fact) => fact.evidence.map((item, index) => ({ ...item, evidence_id: item.evidence_id || `fact-evidence-${fact.fact_id}-${index}` })))
  const previewEvidence: FactEvidence[] = [...(reviewEvidence ? [reviewEvidence] : []), ...assistantEvidence, ...factEvidence, ...(factView === 'automatic' ? [...legacyEvidence, ...instanceEvidence, ...fieldEvidence] : [])]
    .filter((item, index, all) => item.evidence_id ? all.findIndex((candidate) => candidate.evidence_id === item.evidence_id && candidate.role === item.role) === index : true)
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
      onClick={() => { setSelected(item.field_key); setSelectedOccurrence(undefined); setReviewPage(undefined); setReviewEvidence(undefined); setSelectedEvidenceId(undefined) }}
      aria-pressed={field?.field_key === item.field_key}
    >
      <span>{index + 1}</span>
      <span title={item.field_name}>{item.field_name}</span>
      <span title={fieldDisplayValue(item)}>{fieldDisplayValue(item)}</span>
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
          description={result.quality_status === 'FAILED' ? '未获得可用字段，请检查原文与提取规则。' : '请核对缺失字段、全部出现位置及质量提示。不同值均保留，由下游按业务场景使用。'} />
      )}
      {!!result?.warnings?.length && (
        <Alert className="run-alert" type="warning" showIcon message="解析提示"
          description={<ul>{result.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul>} />
      )}
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

        <section className="fields-panel">
          <div className="panel-bar">
            <div>
              <strong>字段抽取结果</strong>
              <span>{fields.length ? `共 ${fields.length} 个字段` : '等待解析'}</span>
            </div>
            {result && completed && (
              <Button size="small" icon={<DownloadOutlined />} onClick={() => exportResult(result)}>
                导出
              </Button>
            )}
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
            {fields.map(renderField)}
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

          <div className="field-scope">
            <strong>当前提取范围</strong>
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
          </div>
        </section>

        <aside className="evidence-panel">
          <div className="panel-bar">
            <strong>证据详情</strong>
            <span>{field ? `${fields.indexOf(field) + 1} / ${fields.length}` : '未选择字段'}</span>
          </div>
          {field ? (
            <>
              <div className="evidence-title">
                <strong>{field.field_name}</strong>
                {validation ? (
                  <Tag color={validation.color}>{validation.label}</Tag>
                ) : (
                  <Tag color={fieldStatus(field).review ? 'gold' : 'blue'}>{fieldStatus(field).label}</Tag>
                )}
              </div>
              <div className="evidence-content">
                <div className="field-badges">
                  {field.required && <Tag color="red">必填</Tag>}
                  {field.data_type && <Tag>{typeNames[field.data_type] || field.data_type}</Tag>}
                  {field.unit && <Tag color="blue">{field.unit}</Tag>}
                  {instances.length > 1 && <Tag color="purple">{instances.length} 个实例</Tag>}
                </div>

                <label>提取值</label>
                <div className="evidence-value">{fieldDisplayValue(field)}</div>

                {result?.facts && <>
                  <label>事实交付（{fieldFacts.length} 条）</label>
                  <Segmented size="small" value={factView} options={[{ label: '自动事实', value: 'automatic' }, { label: '人工修订事实', value: 'reviewed' }]} onChange={(value) => {
                    setFactView(value as 'automatic' | 'reviewed'); setSelectedEvidenceId(undefined); setReviewEvidence(undefined)
                    if (value === 'reviewed') void loadReviewedFacts()
                  }} />
                  <div style={{ marginTop: 8 }}><Button size="small" onClick={() => void downloadFacts('json')}>导出事实 JSON</Button> <Button size="small" onClick={() => void downloadFacts('csv')}>导出事实 CSV</Button></div>
                  {reviewedLoading && <p className="muted">读取人工事实…</p>}
                  {factView === 'reviewed' && reviewedError && <Alert type="error" showIcon message={reviewedError} action={<Button size="small" onClick={() => void loadReviewedFacts()}>重试</Button>} />}
                  {factView === 'reviewed' && !reviewedLoading && !reviewedError && !fieldFacts.length && <p className="muted">该字段没有可展示的人工修订事实（未复核或最新修订已拒绝）。自动事实保留在独立视图中。</p>}
                  <div className="instance-list">
                    {fieldFacts.map((fact) => <div className="instance-item" key={fact.fact_id}>
                      <strong>{fact.value || fact.normalized || '不可确认'}{fact.unit ? ` ${fact.unit}` : ''}</strong>
                      <div className="instance-meta">
                        <span>事实ID {fact.fact_id}</span>
                        <span>状态 {fact.review_status === 'HUMAN_REVIEWED' ? '人工修订' : fact.review_status === 'REJECTED' ? '已拒绝' : '自动结果'}</span>
                        {fact.field_review_status && <span>字段复核 {fact.field_review_status === 'HUMAN_REVIEWED' ? '已有人工版本' : fact.field_review_status === 'REJECTED' ? '已拒绝' : '未复核'}</span>}
                        <span>可供下游 {fact.available_for_downstream == null ? '未评估' : fact.available_for_downstream ? '是' : '否'}</span>
                        <span>置信度 {fact.confidence?.score == null ? '未知' : fact.confidence.score.toFixed(3)} · {fact.confidence?.kind || '未知方法'}</span>
                      </div>
                      {!!Object.keys(fact.dimensions || {}).length && <div className="instance-meta">{Object.entries(fact.dimensions).map(([key, value]) => <span key={key}>{key}：{typeof value === 'object' && value ? String((value as { value?: unknown }).value ?? JSON.stringify(value)) : String(value ?? '—')}</span>)}</div>}
                      {!!fact.risk?.length && <div className="instance-meta">{fact.risk.map((risk, riskIndex) => <Tag color="orange" key={`${risk.code}-${riskIndex}`}>{String(risk.code)}</Tag>)}</div>}
                      <p>原文值：{fact.raw_values?.join('、') || fact.raw || '—'} · 值类型：{valueKindNames[fact.value_kind] || fact.value_kind} · 来源：{fact.source_role}</p>
                      <details><summary>检查项、覆盖与版本追溯</summary><pre style={{ whiteSpace: 'pre-wrap', maxHeight: 220, overflow: 'auto' }}>{JSON.stringify({ checks: fact.checks, risk: fact.risk, coverage: fact.coverage, confidence: fact.confidence, provenance: fact.provenance }, null, 2)}</pre></details>
                      {!!fact.evidence.length && <div className="instance-meta">{fact.evidence.map((ref, refIndex) => <Button type="link" size="small" key={`${ref.evidence_id || refIndex}-${ref.role}`} onClick={() => {
                        setSelectedEvidenceId(ref.evidence_id || `fact-evidence-${fact.fact_id}-${refIndex}`)
                        setReviewEvidence(undefined); setSelectedOccurrence(undefined)
                        setReviewPage(ref.page_number || undefined)
                      }}>查看{ref.role ? ` ${ref.role}` : ''} · 第 {ref.page_number || '—'} 页</Button>)}</div>}
                    </div>)}
                  </div>
                  <p className="muted">“可供下游使用”表示具备来源关联等结构条件，仍可能带风险。置信度是当前提取方法的质量信号，未校准为正确率。</p>
                </>}
                {selectedFact && <Alert type="info" showIcon message={`当前查看事实 ${selectedFact.fact_id}`} description={String(selectedFact.provenance?.source || '已选中事实的证据区域')} />}

                {!!valueGroups.length && <>
                  <label>同值归并结果（{valueGroups.length} 组）</label>
                  <div className="instance-list">{valueGroups.map((group, index) => <div className="instance-item" key={index}>
                    <strong>{group.value ?? '不可确认'}{group.unit ? ` ${group.unit}` : ''}</strong>
                    <p>未核验 · {(group.evidence_refs || group.evidence_ids || []).length} 处证据</p>
                    {!!group.identity && <p>业务身份：{JSON.stringify(group.identity)}</p>}
                    {!!group.raw_values?.length && <p>原文值：{group.raw_values.join('、')}</p>}
                    <details><summary>证据引用</summary>
                      {(group.evidence_refs || group.evidence_ids || []).map((ref, refIndex) => <p key={refIndex}>
                        {typeof ref === 'string' ? ref : `${ref.evidence_id || '—'} · 第 ${ref.page_number || '—'} 页 · ${ref.location_status || '定位未确认'}`}
                      </p>)}
                    </details>
                  </div>)}</div>
                </>}
                {field.extraction_detail?.coverage && <details><summary>该字段取证覆盖范围</summary><pre style={{ whiteSpace: 'pre-wrap' }}>{JSON.stringify(field.extraction_detail.coverage, null, 2)}</pre></details>}
                {!!field.extraction_detail?.incomplete_reasons?.length && <Alert type="warning" showIcon message="未完成原因（不能据此判定缺失或正确）"
                  description={<ul style={{ margin: 0, paddingLeft: 18 }}>{field.extraction_detail.incomplete_reasons.map((code) => <li key={code}>{incompleteReasonNames[code] || code}</li>)}</ul>} />}
                {field.extraction_detail?.retrieval && <div className="evidence-meta">
                  <span>候选<strong>{field.extraction_detail.retrieval.candidates_total ?? '—'}</strong></span>
                  <span>送审<strong>{field.extraction_detail.retrieval.candidates_sent ?? '—'}</strong></span>
                  <span>模型调用<strong>{field.extraction_detail.retrieval.calls ?? '—'}</strong></span>
                  <span>已接受<strong>{field.extraction_detail.retrieval.accepted ?? '—'}</strong></span>
                  <span>非目标<strong>{field.extraction_detail.retrieval.negative ?? '—'}</strong></span>
                  <span>未通过<strong>{field.extraction_detail.retrieval.rejected ?? '—'}</strong></span>
                </div>}
                {!!field.extraction_detail?.unresolved?.length && <Alert type="warning" showIcon message="存在不可确认内容"
                  description={<pre style={{ whiteSpace: 'pre-wrap' }}>{JSON.stringify(field.extraction_detail.unresolved, null, 2)}</pre>} />}

                {!!candidates.length && (
                  <>
                    <label>多路径候选{validationStatus === 'conflict' ? '（存在冲突）' : ''}</label>
                    <div className="instance-list">
                      {candidates.map((candidate, index) => <div className="instance-item" key={index}>
                        <strong>{candidate.value}</strong>
                        <p>{candidate.methods?.map((method) => methodNames[method] || method).join('、') || '来源未知'} · 页码 {candidate.pages?.join('、') || '—'}</p>
                      </div>)}
                    </div>
                  </>
                )}
                {!!rejected.length && (
                  <>
                    <label>未通过校验的候选</label>
                    <div className="instance-list">
                      {rejected.map((candidate, index) => <div className="instance-item" key={index}>
                        <strong>{candidate.value}</strong>
                        <p>{validationNames[candidate.validation_status || '']?.label || candidate.validation_status || '未通过校验'} · 第 {candidate.page_num || '—'} 页</p>
                      </div>)}
                    </div>
                  </>
                )}

                <div className="evidence-meta">
                  <span>
                    方法匹配分<strong>{matchScore(field.confidence)}</strong>
                  </span>
                  <span>
                    提取方法
                    <strong>{methodNames[field.extraction_method || ''] || field.extraction_method}</strong>
                  </span>
                </div>

                {!!instances.length && (
                  <>
                    <label>全部出现位置（{instances.length}）</label>
                    <div className="instance-list">
                      {instances.map((instance, index) => (
                        <div className="instance-item" key={instance.occurrence_id || `${instance.value}-${instance.page_num}-${index}`}>
                          <strong>
                            {instance.value ?? '（未提供值）'}
                            {instance.unit ? ` ${instance.unit}` : ''}
                          </strong>
                          <div className="instance-meta">
                            <span>第 {instance.page_num || '—'} 页</span>
                            <span>方法匹配分 {matchScore(instance.confidence)}</span>
                            <span>{instance.methods?.length ? instance.methods.map((method) => methodNames[method] || method).join('、') : methodNames[instance.source || ''] || instance.source || ''}</span>
                            {instance.value_kind && <span>值类型 {valueKindNames[instance.value_kind] || instance.value_kind}</span>}
                            {instance.binding_status && <span>维度 {bindingNames[instance.binding_status] || instance.binding_status}</span>}
                          </div>
                          {!!instance.quality_issues?.length && <div className="instance-meta">
                            {instance.quality_issues.map((issue, issueIndex) => <Tag color="orange" key={`${issue.code}-${issueIndex}`}>
                              {qualityIssueNames[issue.code] || issue.code}
                            </Tag>)}
                          </div>}
                          {instance.quality_status && instance.quality_status !== 'passed' && (
                            <div className="instance-meta"><Tag color="gold">质量：{validationNames[instance.quality_status]?.label || instance.quality_status}</Tag></div>
                          )}
                          {!!instance.dimensions && <div className="instance-meta">
                            {Object.entries(instance.dimensions).map(([key, dimension]) => (
                              <span key={key} title={typeof dimension === 'string' ? undefined : dimension?.evidence || undefined}>{key}：{typeof dimension === 'string' ? dimension : dimension?.value || '—'}</span>
                            ))}
                          </div>}
                          {(instance.table_title || instance.table_id || instance.row_index != null || instance.column_index != null) && (
                            <div className="instance-meta">
                              <span>表格 {instance.table_title || instance.table_id || '—'}</span>
                              {instance.row_index != null && <span>第 {instance.row_index + 1} 行</span>}
                              {instance.column_index != null && <span>第 {instance.column_index + 1} 列</span>}
                            </div>
                          )}
                          {instance.context && <p>{instance.context}</p>}
                          {instance.bbox && <p>{instance.source === 'agent_extract' ? '查看区域坐标（未核验为字段精确位置）' : '证据坐标'}{instance.coordinate_system ? `（${instance.coordinate_system}）` : ''}：({instance.bbox.x0}, {instance.bbox.y0}) – ({instance.bbox.x1}, {instance.bbox.y1})</p>}
                          {!instance.bbox && <p className="muted">未提供精确证据坐标，仅可按页查看原图。</p>}
                          {instance.start_offset != null && <p>页内文本位置：{instance.start_offset} – {instance.end_offset ?? '—'}</p>}
                          <Button type="link" size="small" disabled={!instance.page_num} onClick={() => { setSelectedOccurrence(index); setReviewPage(undefined); setReviewEvidence(undefined); setSelectedEvidenceId(`instance-${index}`) }}>
                            {selectedOccurrence === index ? '正在查看此处原文' : '查看此处原文'}
                          </Button>
                        </div>
                      ))}
                    </div>
                  </>
                )}

                <label>来源片段</label>
                {occurrence?.context || evidence?.quote ? (
                  <blockquote>{occurrence?.context || evidence?.quote}</blockquote>
                ) : (
                  <p className="muted">该字段未返回原文证据，请对照 PDF 核验。</p>
                )}

                <label>来源位置</label>
                <p className="muted">
                  {occurrence?.page_num || evidence?.page_number ? `第 ${occurrence?.page_num || evidence?.page_number} 页` : '暂无页码'}
                  {evidence?.locator?.bbox && !evidence.location_status && '（历史记录中的坐标为占位值，不能用于定位）'}
                </p>
                {evidence?.location_status === 'unavailable' && (
                  <p className="muted">该证据仅提供页码；未提供可靠坐标时不绘制区域框。</p>
                )}

                {result && field && ['COMPLETED', 'COMPLETED_WITH_REVIEW', 'FAILED'].includes(displayStatus) && <HumanReviewPanel result={result} field={field} onPage={(page) => { setReviewPage(page); setSelectedOccurrence(undefined) }} onEvidence={(ref) => {
                  const id = ref.evidence_id || 'review-evidence'
                  setReviewEvidence({ ...ref, evidence_id: id }); setReviewPage(ref.page_number || undefined); setSelectedEvidenceId(id)
                }} onSaved={loadReviewedFacts} />}
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
        </aside>
      </div>

      <div className="workspace-bottom">
        <span>
          <span className={`health-dot ${footerStatusClass}`} />
          {completed ? '解析结果已保存' : result ? statusNames[displayStatus] || displayStatus : '准备上传'}
        </span>
        <span>文档前处理 → 清单知识与按需取证 → 同值归并与多来源证据 → 核对后使用</span>
        <span>提取信息，不作达标判断</span>
      </div>
      {assistantStarted && result && <div className="assistant-workspace" style={!assistantOpen ? { display: 'none' } : undefined}>
        <section className="preview-panel"><div className="panel-bar"><strong>附件原文</strong>{fileUrl && <a href={fileUrl} target="_blank" rel="noreferrer">打开原文</a>}</div><div className="assistant-paper-stage">{fileUrl ? <PdfPreview url={fileUrl} evidence={assistantEvidence} evidencePage={assistantSelectedEvidence?.page_number || undefined} selectedEvidenceId={assistantSelectedEvidence?.evidence_id} /> : <Empty description="当前附件没有可预览的 PDF" />}</div></section>
        <AssistantPanel documentId={result.document_id} parseRunId={result.parse_run_id} onCitations={(citations, selected) => {
          setAssistantEvidence(citations)
          setAssistantSelectedEvidence(selected)
          setReviewEvidence(selected)
          setSelectedEvidenceId(selected?.evidence_id)
          setReviewPage(selected?.page_number || undefined)
        }} />
      </div>}
    </>
  )
}
