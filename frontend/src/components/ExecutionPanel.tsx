import { useEffect, useRef, useState } from 'react'
import { Alert, Button, Popconfirm, Space, Table, Tag } from 'antd'
import { PauseOutlined, PlayCircleOutlined, ReloadOutlined } from '@ant-design/icons'
import axios from 'axios'
import { useNavigate } from 'react-router-dom'
import type { ExecutionJob, ExecutionResponse, ExtractedField } from '@/types/api'
import { apiError } from '@/services/api'
import { controlExecution, executionStateNames, executionStatusNames, getContentBuildStatus, getExecution, getFieldJobs, resumeContentBuild, semanticStateNames, startReliableExtraction } from '@/services/execution'

const autoStates = new Set(['PLANNED', 'PENDING', 'QUEUED', 'RUNNING', 'YIELDED', 'PARTIAL', 'PROCESSING', 'RETRYING', 'WAITING_SERVICE', 'WAITING_QUOTA', 'WAITING_EXTERNAL_QUOTA'])

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
}

function progress(job: ExecutionJob) {
  const checkpoint = record(job.checkpoint)
  const coverage = record(checkpoint.coverage)
  const candidates = record(checkpoint.candidates || checkpoint.candidate_counts)
  const scope = record(record(checkpoint.rule_snapshot).scope || checkpoint.scope_snapshot)
  const scopeNames: Record<string, string> = { attachment: '全文', page_ranges: '指定页范围', sections: '指定章节' }
  const ranges = Array.isArray(scope.ranges) ? scope.ranges.map((value) => {
    const range = record(value); return `${range.start_page ?? range.start ?? '—'}–${range.end_page ?? range.end ?? '—'}`
  }).join('、') : ''
  return { checkpoint, scope: `${scopeNames[String(scope.mode || '')] || '范围待返回'}${ranges ? `（${ranges} 页）` : ''}`,
    rangesDone: coverage.ranges_done ?? checkpoint.ranges_done,
    rangesTotal: coverage.ranges_total ?? checkpoint.ranges_total,
    candidatesDone: candidates.decided ?? checkpoint.candidates_decided,
    candidatesTotal: candidates.total ?? checkpoint.candidates_total }
}

export default function ExecutionPanel({ runId, fields, onChange }: {
  runId: string; fields: ExtractedField[]; onChange: (value: ExecutionResponse | undefined) => void
}) {
  const navigate = useNavigate()
  const [data, setData] = useState<ExecutionResponse>()
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [unavailable, setUnavailable] = useState(false)
  const [refresh, setRefresh] = useState(0)
  const [paging, setPaging] = useState(false)
  const [buildId, setBuildId] = useState('')
  const [buildStatus, setBuildStatus] = useState('')
  const [buildDispatchState, setBuildDispatchState] = useState('')
  const [buildRefresh, setBuildRefresh] = useState(0)
  const callback = useRef(onChange)
  callback.current = onChange
  const requestSequence = useRef(0)
  const loadedCount = useRef(100)
  const startRequest = useRef<{ signature: string; requestId: string }>()

  useEffect(() => {
    let disposed = false
    let timer: ReturnType<typeof setTimeout> | undefined
    const request = ++requestSequence.current
    let failures = 0
    setLoading(true); setError(''); setUnavailable(false); setData(undefined); callback.current(undefined)
    const load = async () => {
      try {
        const value = await getExecution(runId)
        while (value.next_cursor && value.jobs.length < loadedCount.current && !disposed) {
          const page = await getFieldJobs(runId, value.next_cursor)
          value.jobs.push(...page.jobs)
          value.next_cursor = page.next_cursor
        }
        if (disposed || request !== requestSequence.current) return
        failures = 0; setData(value); setError(''); callback.current(value)
        if (!value.legacy && autoStates.has(value.status)) timer = setTimeout(load, value.status.startsWith('WAITING_') ? 15000 : 4000)
      } catch (failure) {
        if (disposed || request !== requestSequence.current) return
        const status = axios.isAxiosError(failure) ? failure.response?.status : undefined
        if (status === 404) { setUnavailable(true); setError('当前服务未启用可靠执行查询，无法核验所有规则是否完成。') }
        else {
          setError(apiError(failure, '执行状态读取失败，请刷新重试。'))
          if ((status == null || status >= 500 || status === 429) && ++failures <= 3) timer = setTimeout(load, Math.min(4000 * 2 ** failures, 30000))
        }
      } finally { if (!disposed) setLoading(false) }
    }
    void load()
    return () => { disposed = true; clearTimeout(timer) }
  }, [runId, refresh])

  useEffect(() => { setBuildId(''); setBuildStatus(''); setBuildDispatchState('') }, [runId])

  useEffect(() => {
    if (!buildId || !data?.document_id || unavailable) return
    let disposed = false
    let timer: ReturnType<typeof setTimeout> | undefined
    let failures = 0
    const request = requestSequence.current
    const poll = async () => {
      try {
        const value = await getContentBuildStatus(data.document_id, buildId)
        if (disposed || request !== requestSequence.current) return
        failures = 0; setBuildDispatchState(value.dispatch_state); setBuildStatus(value.status === 'CONTENT_READY' ? '内容已准备，正在打开可靠运行' : value.dispatch_state === 'waiting_service' ? '等待内容服务恢复，系统将自动继续' : ['partial', 'failed', 'failed_terminal', 'cancelled', 'superseded'].includes(value.dispatch_state) ? '内容构建未完成，保留已处理检查点' : '正在准备全文内容')
        if (value.parse_run_id) { navigate(`/result/${value.parse_run_id}`); return }
        if (value.last_error) setError(value.last_error)
        if (!['partial', 'failed', 'failed_terminal', 'cancelled', 'superseded'].includes(value.dispatch_state)) timer = setTimeout(poll, value.dispatch_state === 'waiting_service' ? 15000 : 5000)
      } catch (failure) {
        if (disposed || request !== requestSequence.current) return
        const status = axios.isAxiosError(failure) ? failure.response?.status : undefined
        if (status === 404) { setUnavailable(true); setError('内容构建状态接口未启用，未继续轮询。') }
        else { setError(apiError(failure, '内容构建状态读取失败。')); if ((status == null || status >= 500 || status === 429) && ++failures <= 3) timer = setTimeout(poll, Math.min(5000 * 2 ** failures, 30000)) }
      }
    }
    void poll()
    return () => { disposed = true; clearTimeout(timer) }
  }, [buildId, data, unavailable, buildRefresh, navigate])

  const control = async (action: 'resume' | 'pause' | 'cancel') => {
    setBusy(action); setError('')
    try { await controlExecution(runId, action, action === 'resume'); setRefresh((value) => value + 1) }
    catch (failure) { setError(apiError(failure, '执行操作失败，请刷新状态后重试。')) }
    finally { setBusy('') }
  }
  const more = async () => {
    if (!data?.next_cursor) return
    const request = requestSequence.current
    setPaging(true)
    try {
      const page = await getFieldJobs(runId, data.next_cursor)
      if (request !== requestSequence.current) return
      loadedCount.current = data.jobs.length + page.jobs.length
      setData((current) => current ? { ...current, jobs: [...current.jobs, ...page.jobs.filter((row) => !current.jobs.some((existing) => existing.job_id === row.job_id))], next_cursor: page.next_cursor } : current)
    } catch (failure) { if (request === requestSequence.current) setError(apiError(failure, '加载后续规则失败。')) }
    finally { setPaging(false) }
  }

  const start = async () => {
    if (!data?.legacy || !data.document_id) return
    const signature = JSON.stringify({ document_id: data.document_id, parent_run_id: runId })
    if (startRequest.current?.signature !== signature) startRequest.current = { signature, requestId: crypto.randomUUID() }
    setBusy('start'); setError('')
    try {
      const response = await startReliableExtraction(data.document_id, runId, startRequest.current.requestId)
      if (response.parse_run_id) { startRequest.current = undefined; navigate(`/result/${response.parse_run_id}`) }
      else if (response.build_id) { startRequest.current = undefined; setBuildId(response.build_id); setBuildStatus('正在准备全文内容') }
      else setError('服务已接受请求，但没有返回可追踪的运行编号，请刷新状态后检查。')
    } catch (failure) {
      const code = axios.isAxiosError(failure) ? failure.response?.status : undefined
      if (code === 404) { setUnavailable(true); setError('当前服务未启用可靠完整提取，请启用后刷新状态。') }
      else setError(apiError(failure, '完整提取试运行提交失败，请检查当前规则和前处理状态。'))
    } finally { setBusy('') }
  }

  const resumeBuild = async () => {
    if (!buildId || !data?.document_id || !['failed', 'partial'].includes(buildDispatchState)) return
    setBusy('build-resume'); setError('')
    try {
      const response = await resumeContentBuild(data.document_id, buildId)
      setBuildDispatchState('queued'); setBuildStatus('已提交内容恢复，系统会自动继续')
      if (response.parse_run_id) navigate(`/result/${response.parse_run_id}`)
      else setBuildRefresh((value) => value + 1)
    } catch (failure) {
      const code = axios.isAxiosError(failure) ? failure.response?.status : undefined
      setError(code === 409 ? apiError(failure, '当前内容构建正在执行或已不可原地恢复，请按提示重新提交。') : apiError(failure, '内容恢复提交失败，请稍后重试。'))
    } finally { setBusy('') }
  }

  return <section className="execution-panel" aria-label="提取执行进度">
    <div className="panel-bar"><strong>规则执行与完整性</strong><Space wrap>
      {data && <Tag color={data.automatic_complete ? 'green' : 'gold'}>{executionStatusNames[data.status] || data.status}</Tag>}
      <Button size="small" icon={<ReloadOutlined />} loading={loading} disabled={!!busy} onClick={() => setRefresh((value) => value + 1)}>刷新状态</Button>
    </Space></div>
    {error && <Alert type={unavailable ? 'info' : 'error'} showIcon message={error} />}
    {loading && !data && <p className="execution-note" aria-live="polite">正在核验每条规则的执行状态…</p>}
    {data?.legacy && <><p className="execution-note">该运行使用旧引擎，没有可靠执行账本。原有补跑入口继续可用；“解析结束”不表示全文与所有规则已查全。</p>{!unavailable && !buildId && <div className="execution-summary"><Button size="small" type="primary" loading={busy === 'start'} disabled={!!busy} onClick={() => void start()}>按当前规则试运行完整提取</Button><span>创建独立可靠执行运行，覆盖当前全部启用规则。</span></div>}{buildId && <Alert type={['failed_terminal', 'cancelled', 'superseded'].includes(buildDispatchState) ? 'error' : ['failed', 'partial'].includes(buildDispatchState) ? 'warning' : 'info'} showIcon message={buildStatus || '正在查询构建状态…'} description={<>构建编号 {buildId} · 调度状态 {buildDispatchState || '查询中'}。<br />{['failed_terminal', 'cancelled', 'superseded'].includes(buildDispatchState) ? '该构建不可原地恢复，请按错误提示处理服务或原文件版本。' : ['failed', 'partial'].includes(buildDispatchState) ? '已保存区域保留，可重试内容准备；完成后将自动打开可靠运行。' : '系统会自动等待内容发布，成功后打开可靠运行。'}</>} action={<Space><Button size="small" disabled={unavailable || !!busy} onClick={() => setBuildRefresh((value) => value + 1)}>刷新状态</Button>{['failed', 'partial'].includes(buildDispatchState) && <Button size="small" type="primary" loading={busy === 'build-resume'} disabled={!!busy} onClick={() => void resumeBuild()}>重试内容准备</Button>}</Space>} />}</>}
    {data && !data.legacy && <>
      <div className="execution-summary">
        <span>配置规则 {data.total_fields}</span><span>完成提取 {data.counts['semantic:completed'] || 0}</span>
        <span>范围内未找到 {data.counts['semantic:not_found'] || 0}</span><span>待人工核对 {data.counts['semantic:review_required'] || 0}</span>
        <span>内容执行代次 {data.execution_generation}</span>
        {!['COMPLETED', 'COMPLETED_WITH_REVIEW', 'CANCELLED', 'SUPERSEDED'].includes(data.status) && <Space wrap>
          {data.status !== 'PAUSED' && <Button size="small" icon={<PauseOutlined />} loading={busy === 'pause'} disabled={!!busy} onClick={() => void control('pause')}>暂停</Button>}
          <Button size="small" icon={<PlayCircleOutlined />} loading={busy === 'resume'} disabled={!!busy || ['RUNNING', 'QUEUED', 'PENDING'].includes(data.status)} onClick={() => void control('resume')}>恢复未完成规则</Button>
          <Popconfirm title="取消此运行的后续处理？" description="已保存的结果与执行记录仍会保留。" onConfirm={() => control('cancel')} okText="取消运行" cancelText="继续处理"><Button size="small" danger disabled={!!busy} loading={busy === 'cancel'}>取消运行</Button></Popconfirm>
        </Space>}
      </div>
      {!data.automatic_complete && <p className="execution-note">内部时间片或调用额度用完后自动续排。外部额度、服务故障和人工暂停分别等待恢复；当前结果不能作为全部规则已完成。</p>}
      <Table<ExecutionJob> size="small" rowKey="job_id" pagination={false} dataSource={data.jobs} scroll={{ x: 790 }}
        locale={{ emptyText: '执行规则尚未写入账本，请刷新状态。' }} columns={[
          { title: '规则 / 范围', dataIndex: 'field_key', render: (key: string, job) => <><strong>{fields.find((field) => field.field_key === key)?.field_name || key}</strong><small className="table-subtitle">{progress(job).scope}</small></> },
          { title: '调度状态', dataIndex: 'execution_state', render: (value: string) => <Tag color={value === 'done' ? 'blue' : ['failed', 'failed_terminal', 'cancelled'].includes(value) ? 'red' : 'gold'}>{executionStateNames[value] || value}</Tag> },
          { title: '结果完整性', dataIndex: 'semantic_state', render: (value: string) => semanticStateNames[value] || value },
          { title: '已处理范围 / 候选', render: (_: unknown, job) => { const p = progress(job); return <span>范围 {String(p.rangesDone ?? '—')} / {String(p.rangesTotal ?? '—')}<br />候选 {String(p.candidatesDone ?? '—')} / {String(p.candidatesTotal ?? '—')}</span> } },
          { title: '阶段 / 原因', render: (_: unknown, job) => <><span>{job.phase}</span>{job.last_error && <small className="execution-error">{job.last_error}</small>}{job.next_attempt_at && <small className="table-subtitle">下次调度 {job.next_attempt_at}</small>}<details><summary>范围与检查点</summary><pre className="execution-checkpoint">{JSON.stringify(progress(job).checkpoint, null, 2)}</pre></details></> },
        ]} />
      {data.next_cursor && <div className="execution-more"><Button loading={paging} onClick={() => void more()}>加载后续规则（已显示 {data.jobs.length} / {data.total_fields}）</Button></div>}
    </>}
  </section>
}
