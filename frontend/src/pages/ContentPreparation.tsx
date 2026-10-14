import { useEffect, useState } from 'react'
import { Alert, Button, Descriptions, Empty, Result, Space, Spin, Tag } from 'antd'
import { ArrowLeftOutlined, ReloadOutlined } from '@ant-design/icons'
import axios from 'axios'
import { useNavigate, useParams } from 'react-router-dom'
import { apiError } from '@/services/api'
import { getContentBuildStatus, resumeContentBuild, type ContentBuildStatus } from '@/services/execution'

const terminal = new Set(['failed_terminal', 'cancelled', 'superseded'])
const recoverable = new Set(['failed', 'partial'])

export default function ContentPreparation() {
  const { documentId, buildId } = useParams()
  const navigate = useNavigate()
  const [state, setState] = useState<ContentBuildStatus>()
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [unavailable, setUnavailable] = useState(false)
  const [refresh, setRefresh] = useState(0)

  useEffect(() => {
    if (!documentId || !buildId) return
    let disposed = false
    let timer: ReturnType<typeof setTimeout> | undefined
    let failures = 0
    setLoading(true); setUnavailable(false); setError('')
    const load = async () => {
      try {
        const value = await getContentBuildStatus(documentId, buildId)
        if (disposed) return
        failures = 0; setState(value); setError('')
        if (value.parse_run_id) { navigate(`/result/${value.parse_run_id}`, { replace: true }); return }
        if (!terminal.has(value.dispatch_state) && !recoverable.has(value.dispatch_state)) timer = setTimeout(load, value.dispatch_state === 'waiting_service' ? 15000 : 5000)
      } catch (failure) {
        if (disposed) return
        const status = axios.isAxiosError(failure) ? failure.response?.status : undefined
        if (status === 404) { setUnavailable(true); setError('当前服务未启用内容准备状态查询，已停止自动轮询。') }
        else {
          setError(apiError(failure, '内容准备状态读取失败。'))
          if ((status == null || status >= 500 || status === 429) && ++failures <= 3) timer = setTimeout(load, Math.min(5000 * 2 ** failures, 30000))
        }
      } finally { if (!disposed) setLoading(false) }
    }
    void load()
    return () => { disposed = true; clearTimeout(timer) }
  }, [documentId, buildId, navigate, refresh])

  const resume = async () => {
    if (!documentId || !buildId) return
    setBusy(true); setError('')
    try {
      const value = await resumeContentBuild(documentId, buildId)
      if (value.parse_run_id) navigate(`/result/${value.parse_run_id}`, { replace: true })
      else setRefresh((current) => current + 1)
    } catch (failure) {
      setError(apiError(failure, '内容准备恢复失败；请检查原文件版本或服务状态。'))
    } finally { setBusy(false) }
  }

  if (!documentId || !buildId) return <Result status="error" title="内容准备参数缺失" extra={<Button onClick={() => navigate('/upload')}>返回上传</Button>} />

  const dispatch = state?.dispatch_state || 'unknown'
  const isTerminal = terminal.has(dispatch)
  const canResume = recoverable.has(dispatch)
  const waitingText = state?.last_wait_reason === 'ocr_capacity' ? '排队等待' : state?.last_wait_reason === 'vision_fallback_unavailable' ? '等待视觉识别' : '等待服务恢复'
  return <div className="parser-workspace content-preparation-page">
    <div className="page-title-row"><div><h1>准备可靠提取</h1><p>系统正在建立当前文档的固定内容版本。完成后会自动进入可靠提取运行。</p></div><Space><Button icon={<ArrowLeftOutlined />} onClick={() => navigate('/upload')}>返回上传</Button><Button icon={<ReloadOutlined />} loading={loading} onClick={() => setRefresh((current) => current + 1)}>刷新</Button></Space></div>
    {error && <Alert type={unavailable ? 'info' : 'error'} showIcon message={error} />}
    {!state && loading && <div className="loading-state"><Spin size="large" /><p>正在读取内容准备状态…</p></div>}
    {!state && !loading && !error && <Empty description="尚未取得内容准备状态" />}
    {state && <section className="panel content-preparation-card"><div className="panel-heading"><strong className="panel-title">内容版本准备</strong><Tag color={state.status === 'CONTENT_READY' ? 'blue' : isTerminal ? 'red' : canResume ? 'gold' : 'processing'}>{state.status === 'CONTENT_READY' ? '内容已准备' : isTerminal ? '已终止' : canResume ? '可恢复' : state.dispatch_state === 'waiting_service' ? waitingText : '处理中'}</Tag></div><div className="panel-body"><Descriptions size="small" column={1} bordered><Descriptions.Item label="构建编号">{state.build_id}</Descriptions.Item><Descriptions.Item label="调度状态">{state.dispatch_state}</Descriptions.Item><Descriptions.Item label="处理阶段">{state.phase}</Descriptions.Item><Descriptions.Item label="内容版本">{state.content_revision_id || '发布后生成'}</Descriptions.Item><Descriptions.Item label="运行编号">{state.parse_run_id || '内容发布后自动生成'}</Descriptions.Item></Descriptions>{state.last_error && <Alert style={{ marginTop: 14 }} type={isTerminal ? 'error' : 'warning'} showIcon message={state.last_error} />}{state.status === 'CONTENT_READY' && !state.parse_run_id && <Alert style={{ marginTop: 14 }} type="warning" showIcon message="内容已准备，但运行编号尚未返回，系统会继续检查。" />}{canResume && <div className="content-preparation-actions"><Button type="primary" loading={busy} disabled={unavailable} onClick={() => void resume()}>恢复内容准备</Button><span>已处理区域会保留，仅重新处理失败或不可读区域。</span></div>}{isTerminal && <p className="muted">当前构建不能原地恢复，请返回上传页重新提交可靠提取。</p>}</div></section>}
  </div>
}
