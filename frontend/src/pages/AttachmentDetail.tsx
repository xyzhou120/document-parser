import { useCallback, useEffect, useRef, useState } from 'react'
import { Alert, Button, Collapse, Descriptions, Empty, Input, Space, Table, Tag } from 'antd'
import { useNavigate, useParams } from 'react-router-dom'
import { apiError } from '@/services/api'
import { createRequestId } from '@/utils/requestId'
import { Attachment, attachmentStates, controlAttachment, downloadArtifact, getAttachment, getVersion, getVersions, operationNames, ResultField, ResultVersion, startOperation } from '@/services/attachments'

const semanticNames: Record<string, string> = { completed: '有结果', not_found: '未找到', review_required: '待复核', incomplete: '不完整', partial_candidates: '搜索未完成' }
export default function AttachmentDetail() {
  const { attachmentId } = useParams(); const navigate = useNavigate()
  const [attachment, setAttachment] = useState<Attachment>()
  const [versions, setVersions] = useState<ResultVersion[]>([])
  const [total, setTotal] = useState(0); const [page, setPage] = useState(1)
  const [selected, setSelected] = useState<ResultVersion>()
  const [error, setError] = useState(''); const [loading, setLoading] = useState(false); const [busy, setBusy] = useState(false)
  const [reason, setReason] = useState('')
  const pendingRequest = useRef<Record<string, string>>({})
  const load = useCallback(async () => {
    if (!attachmentId) return
    setLoading(true)
    try {
      const [a, v] = await Promise.all([getAttachment(attachmentId), getVersions(attachmentId, page)])
      setAttachment(a); setVersions(v.items); setTotal(v.total); setError('')
    } catch (e) { setError(apiError(e, '附件详情加载失败，请刷新重试。')) }
    finally { setLoading(false) }
  }, [attachmentId, page])
  useEffect(() => { setSelected(undefined); void load() }, [load])
  const active = !!attachment?.latest && ['QUEUED', 'PREPARING_CONTENT', 'RUNNING', 'PAUSED'].includes(attachment.latest.state)
  useEffect(() => {
    if (!active) return
    const timer = setInterval(() => void load(), 5000)
    return () => clearInterval(timer)
  }, [active, load])
  const showVersion = async (id: string) => {
    if (!attachmentId) return
    setBusy(true)
    try { setSelected(await getVersion(attachmentId, id)); setError('') }
    catch (e) { setError(apiError(e, '版本详情加载失败。')) }
    finally { setBusy(false) }
  }
  const act = async (operation: string) => {
    if (!attachmentId || !reason.trim()) return
    setBusy(true)
    // 网络响应丢失时保留同一请求 ID，重试不会创建另一个版本。
    pendingRequest.current[operation] ||= createRequestId()
    try {
      await startOperation(attachmentId, operation, reason.trim(), pendingRequest.current[operation])
      delete pendingRequest.current[operation]; setSelected(undefined); await load()
    } catch (e) { setError(apiError(e, '提取操作未受理，请重试。')) }
    finally { setBusy(false) }
  }
  const control = async (action: string) => {
    if (!attachmentId || !reason.trim()) return
    setBusy(true)
    try { await controlAttachment(attachmentId, action, reason); await load() }
    catch (e) { setError(apiError(e, '任务操作失败。')) }
    finally { setBusy(false) }
  }
  const exportSnapshot = async () => {
    if (!attachmentId || !selected) return
    try { await downloadArtifact(`/v1/attachments/${attachmentId}/versions/${selected.id}/export`, `附件-V${selected.number}.json`) }
    catch (e) { setError(apiError(e, '快照导出失败。')) }
  }
  if (!attachmentId) return <Empty description="附件不存在" />
  return <div className="page-stack">
    {error && <Alert type="error" showIcon message={error} action={<Button onClick={() => void load()}>重试</Button>} />}
    {!attachment ? <Empty description={loading ? '正在加载附件…' : '暂无附件详情'} /> : <>
      <div className="page-heading"><div><h1>{attachment.attachment_name}</h1><p>{attachment.mine_name} · {attachment.file_name} · 所属期 {attachment.report_time_raw}</p></div>
        <Space wrap><Button onClick={() => navigate('/documents')}>返回列表</Button><Button onClick={() => void load()}>刷新</Button></Space></div>
      {attachment.current && attachment.current_validity !== 'VALID' && <Alert type="warning" showIcon message="当前来源或事实有效性已变化。历史快照保留定版内容，当前事实按各自失效范围重新判断。" />}
      <Descriptions bordered size="small" column={{ xs: 1, sm: 2, lg: 3 }} items={[
        { key: 'source', label: '上游附件 ID', children: attachment.source_id },
        { key: 'format', label: '文件格式', children: attachment.format.toUpperCase() },
        { key: 'download', label: '获取状态', children: attachmentStates[attachment.download_state] || attachment.download_state },
        { key: 'mine', label: '煤矿', children: `${attachment.mine_code} / ${attachment.mine_name}` },
        { key: 'checklist', label: '上游清单', children: `${attachment.file_name_id} / ${attachment.file_name}` },
        { key: 'period', label: '所属周期', children: `${attachment.report_time_raw} (${attachment.period_key})` },
        { key: 'current', label: '当前结果', children: attachment.current ? `V${attachment.current.number} · 流程完整` : '尚无完整结果' },
        { key: 'created', label: '首次登记', children: attachment.created_at },
        { key: 'error', label: '获取/登记原因', children: attachment.last_error || '—' },
      ]} />
      <Space wrap>
        {attachment.current && <Button type="primary" loading={busy} onClick={() => void showVersion(attachment.current!.id)}>查看当前结果</Button>}
        {attachment.latest?.run_id && <Button onClick={() => navigate(`/result/${attachment.latest!.run_id}`)}>最近执行记录</Button>}
        {attachment.latest?.build_id && attachment.document_id && <Button onClick={() => navigate(`/documents/${attachment.document_id}/content-build/${attachment.latest!.build_id}`)}>内容准备进度</Button>}
      </Space>
      <div className="filter-panel attachment-business-grid">
        <Input aria-label="操作原因" placeholder="填写本次补提、重提或重试的原因" value={reason} maxLength={2000} onChange={e => setReason(e.target.value)} />
        <Space wrap>
          <Button disabled={active || busy || !reason.trim() || attachment.download_state !== 'SUCCEEDED'} onClick={() => void act('FULL_REEXTRACT')}>全量重提</Button>
          <Button disabled={active || busy || !reason.trim() || !attachment.current} onClick={() => void act('INCREMENTAL_EXTRACT')}>新增目标补提</Button>
          {attachment.latest?.state === 'FAILED' || ['FAILED_TERMINAL', 'FAILED_RETRYABLE'].includes(attachment.download_state) || attachment.status === 'START_BLOCKED' ? <Button disabled={busy || !reason.trim()} onClick={() => void control('retry')}>重试失败操作</Button> : null}
          {active && attachment.latest?.state !== 'PAUSED' && <Button disabled={busy || !reason.trim()} onClick={() => void control('pause')}>暂停</Button>}
          {(attachment.latest?.state === 'PAUSED' || attachment.download_state === 'PAUSED') && <Button disabled={busy || !reason.trim()} onClick={() => void control('resume')}>继续</Button>}
          {active && <Button disabled={busy || !reason.trim()} onClick={() => void control('cancel')}>取消本轮</Button>}
        </Space>
      </div>
      <Table<ResultVersion> rowKey="id" loading={loading} dataSource={versions} scroll={{ x: 900 }}
        pagination={{ current: page, total, pageSize: 20, showSizeChanger: false, onChange: setPage }}
        locale={{ emptyText: <Empty description="尚无版本；PDF 获取成功且完成清单映射后自动初提" /> }} columns={[
          { title: '版本', dataIndex: 'number', render: (v, r) => <>V{v} {r.id === attachment.current?.id && <Tag color="green">当前</Tag>}</> },
          { title: '操作', dataIndex: 'operation', render: v => operationNames[v] || v },
          { title: '状态', dataIndex: 'state', render: (v, r) => <>{attachmentStates[v] || v}<small className="table-subtitle">{r.last_error || ''}</small></> },
          { title: '字段统计', key: 'stats', render: (_, r) => r.stats ? `可用 ${r.stats.available}/${r.stats.targets} · 待复核 ${r.stats.review_required}` : '—' },
          { title: '操作时间', dataIndex: 'created_at' },
          { title: '操作', key: 'actions', render: (_, r) => <Button type="link" disabled={r.state !== 'COMPLETED' || busy} onClick={() => void showVersion(r.id)}>查看快照</Button> },
        ]} />
      {selected?.snapshot && <section className="snapshot-panel">
        <div className="page-heading"><div><h2>V{selected.number} 结果快照</h2><p>{operationNames[selected.operation]} · {selected.reason} · 定版时共 {selected.snapshot.stats.targets} 个目标</p></div><Button onClick={() => void exportSnapshot()}>导出此版本</Button></div>
        <Table<ResultField> rowKey="field_key" dataSource={selected.snapshot.fields} pagination={{ pageSize: 20 }} scroll={{ x: 720 }} columns={[
          { title: '提取目标', key: 'name', render: (_, f) => <>{f.field?.field_name || f.field_key}<small className="table-subtitle">{f.field_key}</small></> },
          { title: '状态', dataIndex: 'semantic_state', render: v => semanticNames[v] || v },
          { title: '提取内容', key: 'value', render: (_, f) => f.field?.field_value || f.facts.automatic.map(fact => fact.value).join('；') || '—' },
          { title: '来源', key: 'source', render: (_, f) => f.inherited_from_version_id ? '继承历史字段' : '本轮提取' },
        ]} expandable={{ expandedRowRender: f => <div>{f.facts.automatic.length ? f.facts.automatic.map(fact => <div key={fact.fact_id}><p><strong>{fact.value}</strong> · {fact.available_for_downstream ? '可用于下游核验' : '需核对'}</p>{fact.evidence.map((e, i) => <p key={i}>第 {e.page_number || '—'} 页：{e.quote || '证据定位已保存'}</p>)}</div>) : <p>该目标没有可展示事实。状态：{semanticNames[f.semantic_state] || f.semantic_state}。</p>}</div> }} />
        <Collapse items={[{ key: 'config', label: '查看当时业务属性与配置', children: <pre className="attachment-json">{JSON.stringify({ metadata: selected.snapshot.metadata, configuration: selected.snapshot.configuration }, null, 2)}</pre> }]} />
      </section>}
    </>}
  </div>
}
