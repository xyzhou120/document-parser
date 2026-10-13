import { useEffect, useState } from 'react'
import { Alert, Button, Checkbox, InputNumber, Modal, Select, Space, Spin, message } from 'antd'
import axios from 'axios'
import { ArrowLeftOutlined, ReloadOutlined } from '@ant-design/icons'
import { useParams, useNavigate } from 'react-router-dom'
import ParseWorkspace from '@/components/ParseWorkspace'
import { getParseRun, getDocumentFile, extractDocument, resumeDocument } from '@/services/parse'
import { getRules } from '@/services/rules'
import { apiError } from '@/services/api'
import type { ExtractionRule, ParseResultResponse } from '@/types/api'

export default function ParseResult() {
  const { parseRunId } = useParams()
  const navigate = useNavigate()
  const [result, setResult] = useState<ParseResultResponse>()
  const [error, setError] = useState('')
  const [refresh, setRefresh] = useState(0)
  const [loading, setLoading] = useState(true)
  const [supplementOpen, setSupplementOpen] = useState(false)
  const [rulesLoading, setRulesLoading] = useState(false)
  const [supplementRules, setSupplementRules] = useState<ExtractionRule[]>([])
  const [fieldKeys, setFieldKeys] = useState<string[]>([])
  const [forceRefresh, setForceRefresh] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [agentLimits, setAgentLimits] = useState<Record<string, number>>({})

  const openSupplement = async () => {
    if (!result?.checklist_type) return
    setRulesLoading(true)
    setSupplementRules([])
    setFieldKeys([])
    setForceRefresh(false)
    setAgentLimits({})
    setSupplementOpen(true)
    try { setSupplementRules((await getRules(result.checklist_type)).filter((rule) => rule.enabled)) }
    catch (failure) { message.error(apiError(failure, '读取当前提取规则失败。')); setSupplementOpen(false) }
    finally { setRulesLoading(false) }
  }

  const handleSupplement = async () => {
    if (!result) return
    setSubmitting(true)
    try {
      const response = await extractDocument(result.document_id, { field_keys: fieldKeys, force_refresh: forceRefresh, agent_limits: Object.keys(agentLimits).length ? agentLimits : undefined })
      message.success(response.message || '补提取任务已提交')
      setSupplementOpen(false)
      navigate(`/result/${response.parse_run_id}`)
    } catch (failure) { message.error(apiError(failure, '提交补提取失败。')) }
    finally { setSubmitting(false) }
  }
  const handleResume = async () => {
    if (!result) return
    setSubmitting(true)
    try {
      const response = await resumeDocument(result.document_id)
      message.success('未完成字段补跑已提交，复用兼容前处理')
      navigate(`/result/${response.parse_run_id}`)
    } catch (failure) { message.error(apiError(failure, '提交续跑失败，请核对前处理版本和取证预算。')) }
    finally { setSubmitting(false) }
  }
  useEffect(() => {
    let disposed = false
    let timer: ReturnType<typeof setTimeout>
    let failures = 0
    setLoading(true); setError(''); setResult(undefined)
    const fetchResult = async () => {
      try {
        const data = await getParseRun(parseRunId!)
        if (disposed) return
        failures = 0
        setResult(data); setError('')
        if (['PLANNED', 'PENDING', 'QUEUED', 'RUNNING', 'YIELDED', 'PARTIAL', 'PROCESSING', 'RETRYING', 'WAITING_SERVICE', 'WAITING_EXTERNAL_QUOTA'].includes(data.status)) timer = setTimeout(fetchResult, data.status.startsWith('WAITING_') ? 15000 : 3000)
      } catch (failure) {
        if (disposed) return
        const status = axios.isAxiosError(failure) ? failure.response?.status : undefined
        const retryable = status == null || status === 408 || status === 429 || status >= 500
        if (retryable) {
          failures += 1
          const delay = Math.min(3000 * 2 ** Math.min(failures - 1, 4), 30000)
          setError(`${apiError(failure, '加载解析结果失败。')} 将在 ${delay / 1000} 秒后自动重试。`)
          timer = setTimeout(fetchResult, delay)
        } else {
          setError(apiError(failure, '加载解析结果失败。'))
        }
      }
      finally { if (!disposed) setLoading(false) }
    }
    void fetchResult()
    return () => { disposed = true; clearTimeout(timer) }
  }, [parseRunId, refresh])
  return <div className="parser-workspace"><div className="page-title-row"><div><h1>解析结果</h1><p>字段与全部出现位置共同呈现；方法匹配分不代表取值正确概率。</p></div><div className="page-actions"><Button icon={<ArrowLeftOutlined />} onClick={() => navigate('/tasks')}>返回队列</Button><Button disabled={!result?.checklist_type || !['COMPLETED', 'FAILED'].includes(result?.status || '')} onClick={() => void openSupplement()}>补提取</Button><Button icon={<ReloadOutlined />} loading={loading} onClick={() => setRefresh((value) => value + 1)}>刷新</Button></div></div>
    {result?.contract_version !== 'reliable-extraction-v2' && result?.status === 'COMPLETED' && result.extraction_profile?.agent_runtime && <div style={{ marginBottom: 12 }}><Button loading={submitting} onClick={() => void handleResume()}>补跑未完成取证字段（旧引擎）</Button><span className="muted" style={{ marginLeft: 12 }}>保留原运行，复用前处理；取证覆盖与正确性仍分别验证。</span></div>}
    {error && <Alert className="run-alert" type="error" showIcon message={error} action={<Button onClick={() => setRefresh((value) => value + 1)}>重试</Button>} />}
    {loading && !result ? <div className="loading-state"><Spin size="large" /></div> : result && <ParseWorkspace fileName={result.document_name} fileUrl={getDocumentFile(result.document_id)} result={result} />}
    <Modal open={supplementOpen} title="补充提取历史文档" onCancel={() => setSupplementOpen(false)} onOk={() => void handleSupplement()} confirmLoading={submitting} okButtonProps={{ disabled: rulesLoading }} okText="提交补提取" cancelText="取消">
      <p>默认对比已完成任务的规则快照，仅执行新增或变更规则；无需再次上传文件。</p>
      <Select mode="multiple" style={{ width: '100%' }} loading={rulesLoading} value={fieldKeys} onChange={setFieldKeys}
        placeholder="留空自动补充；也可指定字段"
        options={supplementRules.map((rule) => ({ value: rule.field_key, label: `${rule.field_name_cn} · v${rule.version}` }))} />
      <p>已有前处理结果可兼容时将复用。旧结果保留，新任务记录规则版本及父任务关系。</p>
      {result?.extraction_profile?.extraction_engine === 'checklist_agent' && <><p>可选的本次取证预算（留空沿用平台配置；预算扩大仍可能增加模型耗时）。</p><Space wrap>{[
        { key: 'max_rounds', label: '总轮数', max: 500 },
        { key: 'max_view_pages', label: '看图页数', max: 1000 },
        { key: 'max_total_tokens', label: '总Token', max: 2000000 },
        // Plan C (2026-10-10): per-field allowance and call cap of the retrieval mode.
        { key: 'field_token_budget', label: '单字段Token', max: 200000 },
        { key: 'max_calls_per_field', label: '每字段调用次数', max: 10 },
      ].map((item) => <label key={item.key}>{item.label} <InputNumber min={1} max={item.max} value={agentLimits[item.key]} onChange={(value) => setAgentLimits((current) => {
        const next = { ...current }; if (value == null) delete next[item.key]; else next[item.key] = value; return next
      })} /></label>)}</Space></>}
      <Checkbox checked={forceRefresh} onChange={(event) => setForceRefresh(event.target.checked)}>强制全量更新（重新读取 PDF、OCR、表格及全部启用规则）</Checkbox>
    </Modal>
  </div>
}
