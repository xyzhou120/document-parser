import { useCallback, useEffect, useRef, useState } from 'react'
import axios from 'axios'
import { Alert, Button, Card, Checkbox, Descriptions, Empty, Input, Select, Space, Spin, Table, Tabs, Tag, Typography, message } from 'antd'
import { ExperimentOutlined, PlusOutlined, ReloadOutlined, RollbackOutlined } from '@ant-design/icons'
import { Link } from 'react-router-dom'
import { apiError } from '@/services/api'
import { getChecklistTypes, getRules } from '@/services/rules'
import { getParseRun } from '@/services/parse'
import {
  compareRuns, createDraft, getActiveBundle, listBundles, listReleaseHistory, previewDraft, publishBundle,
  rollbackBundle, runBundleExtraction, validateDraft,
  type ReleaseEvent, type ReleasePreview, type ReleaseValidation, type RuleBundle,
} from '@/services/releases'

const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
const pretty = (value: unknown) => JSON.stringify(value, null, 2)
const jsonStyle = { margin: 0, padding: 14, background: '#f6f8fa', borderRadius: 5, overflow: 'auto', maxHeight: 440, fontSize: 12 } as const

function objectJson(text: string): Record<string, unknown> {
  const value: unknown = JSON.parse(text)
  if (value === null || typeof value !== 'object' || Array.isArray(value)) throw new Error('请填写 JSON 对象。')
  return value as Record<string, unknown>
}

export default function RuleReleases() {
  const [types, setTypes] = useState<Array<{ value: string; label: string }>>([])
  const [checklist, setChecklist] = useState('')
  const [bundles, setBundles] = useState<RuleBundle[]>([])
  const [history, setHistory] = useState<ReleaseEvent[]>([])
  const [selectedId, setSelectedId] = useState<string>()
  const [baselineId, setBaselineId] = useState<string>()
  const [rulesJson, setRulesJson] = useState('[]')
  const [operator, setOperator] = useState('')
  const [note, setNote] = useState('')
  const [reason, setReason] = useState('')
  const [sampleIds, setSampleIds] = useState('')
  const [sampleReport, setSampleReport] = useState('')
  const [documentId, setDocumentId] = useState('')
  const [refreshArtifact, setRefreshArtifact] = useState(false)
  const [trialRunId, setTrialRunId] = useState('')
  const [trialStatus, setTrialStatus] = useState('')
  const [beforeRunId, setBeforeRunId] = useState('')
  const [afterRunId, setAfterRunId] = useState('')
  const [comparison, setComparison] = useState<Record<string, unknown>>()
  const [validation, setValidation] = useState<ReleaseValidation>()
  const [preview, setPreview] = useState<ReleasePreview>()
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [snapshotLoading, setSnapshotLoading] = useState(false)
  const [busy, setBusy] = useState('')
  const [tab, setTab] = useState('snapshot')
  const requestVersion = useRef(0)
  const snapshotVersion = useRef(0)
  const contextVersion = useRef(0)
  const operationVersion = useRef(0)
  const selectedRef = useRef(selectedId)
  selectedRef.current = selectedId
  const selected = bundles.find((bundle) => bundle.bundle_id === selectedId)
  const active = bundles.find((bundle) => bundle.is_active)
  const generation = bundles[0]?.current_generation ?? 0
  const disabled = !!busy
  const invalidateRequests = useCallback(() => {
    requestVersion.current++; snapshotVersion.current++; contextVersion.current++; operationVersion.current++
  }, [])

  const refresh = useCallback(async (type: string) => {
    const version = ++requestVersion.current
    setLoading(true)
    try {
      const [rows, events, current] = await Promise.all([listBundles(type), listReleaseHistory(type), getActiveBundle(type).catch((error) => {
        if (axios.isAxiosError(error) && error.response?.status === 404) return undefined
        throw error
      })])
      if (version !== requestVersion.current) return
      const available = current && !rows.some((bundle) => bundle.bundle_id === current.bundle_id) ? [...rows, current] : rows
      setBundles(available); setHistory(events)
      setSelectedId((id) => available.some((bundle) => bundle.bundle_id === id) ? id : available[0]?.bundle_id)
    } catch (e) {
      if (version === requestVersion.current) setError(apiError(e, '规则版本加载失败，请重试。'))
    } finally {
      if (version === requestVersion.current) setLoading(false)
    }
  }, [])

  const captureEditingRules = useCallback(async (type: string) => {
    const version = ++snapshotVersion.current
    setSnapshotLoading(true)
    try {
      const rows = await getRules(type)
      if (version !== snapshotVersion.current) return
      setRulesJson(pretty(rows))
    } catch (e) {
      if (version === snapshotVersion.current) setError(apiError(e, '读取编辑规则失败，请重试。'))
    } finally {
      if (version === snapshotVersion.current) setSnapshotLoading(false)
    }
  }, [])

  useEffect(() => {
    let disposed = false
    void getChecklistTypes().then((rows) => {
      if (disposed) return
      const options = rows.filter((row) => row.is_active).map((row) => ({ value: row.checklist_type, label: row.checklist_name }))
      setTypes(options); setChecklist((current) => current || options[0]?.value || '')
    }).catch((e) => { if (!disposed) setError(apiError(e, '清单加载失败，请刷新页面。')) })
    return () => { disposed = true; invalidateRequests() }
  }, [invalidateRequests])

  useEffect(() => {
    contextVersion.current++; operationVersion.current++
    setBundles([]); setHistory([]); setSelectedId(undefined); setBaselineId(undefined)
    setValidation(undefined); setPreview(undefined); setComparison(undefined); setRulesJson('[]')
    setTrialRunId(''); setTrialStatus(''); setBeforeRunId(''); setAfterRunId(''); setSampleIds(''); setSampleReport(''); setReason(''); setError(''); setBusy('')
    if (checklist) { void refresh(checklist); void captureEditingRules(checklist) }
    return invalidateRequests
  }, [checklist, refresh, captureEditingRules, invalidateRequests])

  const selectBundle = (id: string) => {
    operationVersion.current++
    setSelectedId(id); setValidation(undefined); setPreview(undefined); setComparison(undefined)
    setTrialRunId(''); setTrialStatus(''); setSampleIds(''); setSampleReport(''); setReason('')
  }

  const perform = async (key: string, action: () => Promise<void>) => {
    const token = ++operationVersion.current
    const context = contextVersion.current
    setBusy(key); setError('')
    try { await action() } catch (e) {
      if (context !== contextVersion.current || token !== operationVersion.current) return
      setError(e instanceof SyntaxError ? 'JSON 格式有误，请检查后重试。' : e instanceof Error && !axios.isAxiosError(e) ? e.message : apiError(e, '操作失败，请检查输入后重试。'))
      if (axios.isAxiosError(e) && e.response?.status === 409) {
        setValidation(undefined); setPreview(undefined)
        await refresh(checklist)
      }
    } finally { if (token === operationVersion.current) setBusy('') }
  }

  const create = () => perform('create', async () => {
    if (!operator.trim()) throw new Error('请填写操作人。')
    const parsed: unknown = JSON.parse(rulesJson)
    if (!Array.isArray(parsed) || !parsed.length || parsed.some((rule) => !rule || typeof rule !== 'object' || Array.isArray(rule))) throw new Error('规则快照须为非空 JSON 对象数组。')
    const bundle = await createDraft({ checklist_type: checklist, rules: parsed as Record<string, unknown>[], operator: operator.trim(), note: note.trim() })
    await refresh(checklist); setSelectedId(bundle.bundle_id); setValidation(undefined); setPreview(undefined); setTab('preview')
    message.success(`已固定规则快照 v${bundle.version}，尚未发布`)
  })

  const inspect = (kind: 'validate' | 'preview') => perform(kind, async () => {
    if (!selected) return
    const id = selected.bundle_id
    if (kind === 'validate') {
      const result = await validateDraft(id)
      if (selectedRef.current === id) setValidation(result)
    } else {
      const result = await previewDraft(id, baselineId)
      if (selectedRef.current === id) { setPreview(result); setValidation(result.validation) }
    }
  })

  const activate = (rollback: boolean) => perform(rollback ? 'rollback' : 'publish', async () => {
    if (!selected) return
    if (!operator.trim() || !reason.trim()) throw new Error('发布或回滚必须填写操作人和原因。')
    const ids = sampleIds.split(/[\s,，;；]+/).filter(Boolean)
    if (ids.some((id) => !uuidPattern.test(id))) throw new Error('样本运行 ID 须为 UUID，多个 ID 用换行或逗号分隔。')
    const command = { expected_generation: generation, operator: operator.trim(), reason: reason.trim(), sample_run_ids: [...new Set(ids)], ...(sampleReport.trim() ? { sample_report: objectJson(sampleReport) } : {}) }
    await (rollback ? rollbackBundle : publishBundle)(selected.bundle_id, command)
    await refresh(checklist); setPreview(undefined); setValidation(undefined); setReason('')
    message.success(rollback ? '已回滚到所选历史版本' : '所选规则版本已发布')
  })

  const startTrial = () => perform('trial', async () => {
    if (!selected) return
    if (!uuidPattern.test(documentId.trim())) throw new Error('请填写已有文档的 UUID。')
    const result = await runBundleExtraction(documentId.trim(), selected.bundle_id, refreshArtifact)
    if (typeof result.parse_run_id !== 'string') throw new Error('试跑返回缺少运行 ID，请刷新任务列表核查。')
    setTrialRunId(result.parse_run_id); setTrialStatus(String(result.status || 'PENDING')); setAfterRunId(result.parse_run_id)
    message.success('试跑任务已提交，使用固定版本且不改变当前发布规则')
  })

  const loadTrial = () => perform('trialStatus', async () => {
    const result = await getParseRun(trialRunId)
    setTrialStatus(result.status)
  })

  const compare = () => perform('compare', async () => {
    if (!uuidPattern.test(beforeRunId.trim()) || !uuidPattern.test(afterRunId.trim())) throw new Error('请填写对比前后的运行 UUID。')
    setComparison(await compareRuns(afterRunId.trim(), beforeRunId.trim()))
  })

  const restoreSnapshot = () => {
    if (!selected) return
    setRulesJson(pretty(selected.rules)); setNote(`基于规则快照 v${selected.version}`); setTab('snapshot')
  }

  return <div className="list-page">
    <Space direction="vertical" size={20} style={{ width: '100%' }}>
      <div className="page-title-row"><div><h1>规则发布</h1><p>固定规则快照，查看配置差异，在已有文档上试跑后显式发布或回滚。</p></div>
        <Space wrap><Select aria-label="清单类型" value={checklist || undefined} options={types} disabled={disabled} placeholder="选择清单" style={{ minWidth: 200 }} onChange={setChecklist} /><Button icon={<ReloadOutlined />} disabled={!checklist || disabled} loading={loading} onClick={() => void refresh(checklist)}>刷新版本</Button></Space>
      </div>
      {error && <Alert type="error" showIcon closable onClose={() => setError('')} message={error} />}
      <Alert type="info" showIcon message="规则发布与效果验收分别记录" description="配置校验及差异预览不调用模型。样本试跑会提交解析任务；发布只切换规则版本，当前未启用人工必审或概率阈值放行门禁。" />
      {!checklist ? <Empty description="暂无启用清单，请先在规则管理中配置" /> : <>
        <Card title="版本列表" extra={<Space wrap><Tag color={active ? 'success' : 'default'}>{active ? `当前发布 v${active.version}` : '尚无发布快照，使用编辑规则'}</Tag><span>发布代数 {generation}</span></Space>}>
          <Table<RuleBundle> rowKey="bundle_id" dataSource={bundles} loading={loading} scroll={{ x: 800 }} pagination={{ pageSize: 8 }} rowSelection={{ type: 'radio', selectedRowKeys: selectedId ? [selectedId] : [], onChange: (keys) => selectBundle(String(keys[0])), getCheckboxProps: () => ({ disabled }) }} columns={[
            { title: '版本', dataIndex: 'version', render: (value: number, bundle) => <Button type="link" disabled={disabled} onClick={() => selectBundle(bundle.bundle_id)}>v{value}</Button> },
            { title: '状态', render: (_, bundle) => <Tag color={bundle.is_active ? 'success' : 'default'}>{bundle.is_active ? '当前发布' : bundle.has_been_published ? '历史发布' : '草稿'}</Tag> },
            { title: '基础代数', dataIndex: 'base_generation' }, { title: '字段数', render: (_, bundle) => bundle.rules.length },
            { title: '操作人', dataIndex: 'operator' }, { title: '说明', dataIndex: 'note', ellipsis: true },
            { title: '创建时间', dataIndex: 'created_at', render: (value: string) => new Date(value).toLocaleString('zh-CN') },
          ]} locale={{ emptyText: '尚无固定规则版本，请从当前编辑规则创建草稿。' }} />
        </Card>
        <Tabs activeKey={tab} onChange={setTab} items={[
          { key: 'snapshot', label: '创建固定草稿', children: <Card title="规则快照" extra={<Button disabled={disabled || snapshotLoading} loading={snapshotLoading} onClick={() => void captureEditingRules(checklist)}>载入当前编辑规则</Button>}>
            <Space direction="vertical" size={14} style={{ width: '100%' }}>
              <Typography.Paragraph type="secondary">读取编辑规则后，可修改下面的 JSON。创建后内容不可变；后续修改需新建快照。发布后，规则管理中的编辑不会直接改变已发布版本。</Typography.Paragraph>
              <Input aria-label="草稿操作人" maxLength={100} placeholder="操作人（必填）" value={operator} disabled={disabled} onChange={(e) => setOperator(e.target.value)} />
              <Input aria-label="草稿说明" maxLength={4000} placeholder="草稿说明" value={note} disabled={disabled} onChange={(e) => setNote(e.target.value)} />
              <Spin spinning={snapshotLoading}><Input.TextArea aria-label="固定规则 JSON 快照" value={rulesJson} disabled={disabled || snapshotLoading} onChange={(e) => setRulesJson(e.target.value)} autoSize={{ minRows: 12, maxRows: 24 }} spellCheck={false} style={{ fontFamily: 'monospace', fontSize: 12 }} /></Spin>
              <Button type="primary" icon={<PlusOutlined />} disabled={disabled || snapshotLoading || !operator.trim()} loading={busy === 'create'} onClick={() => void create()}>创建草稿快照</Button>
            </Space>
          </Card> },
          { key: 'preview', label: '校验与配置差异', children: !selected ? <Empty description="请选择规则版本" /> : <Card title={`v${selected.version} 配置预览`}>
            <Space direction="vertical" size={16} style={{ width: '100%' }}>
              <Descriptions size="small" column={1} items={[{ key: 'id', label: '快照 ID', children: <Typography.Text copyable>{selected.bundle_id}</Typography.Text> }, { key: 'hash', label: '知识 hash', children: <Typography.Text copyable style={{ wordBreak: 'break-all' }}>{selected.knowledge_hash}</Typography.Text> }]} />
              <Space wrap><Button disabled={disabled} loading={busy === 'validate'} onClick={() => void inspect('validate')}>校验配置</Button><Select aria-label="差异对比基准" allowClear placeholder="默认与当前发布版本比较" value={baselineId} disabled={disabled} style={{ minWidth: 260 }} options={bundles.filter((bundle) => bundle.bundle_id !== selectedId).map((bundle) => ({ value: bundle.bundle_id, label: `v${bundle.version}${bundle.is_active ? '（当前发布）' : ''}` }))} onChange={(id) => { setBaselineId(id); setPreview(undefined) }} /><Button disabled={disabled} loading={busy === 'preview'} onClick={() => void inspect('preview')}>预览配置差异</Button><Button disabled={disabled} onClick={restoreSnapshot}>以此建立新草稿</Button></Space>
              {validation && <Alert type={validation.valid ? 'success' : 'error'} showIcon message={validation.valid ? `配置校验通过：${validation.enabled_rule_count} 个启用字段` : '配置校验失败'} description={[...(validation.errors || []), ...(validation.warnings || [])].join('；')} />}
              {preview && <><Typography.Text>新增 {preview.diff.added.length} · 移除 {preview.diff.removed.length} · 变更 {preview.diff.changed.length}；未调用模型。</Typography.Text><pre style={jsonStyle}>{pretty(preview.diff)}</pre></>}
            </Space>
          </Card> },
          { key: 'trial', label: '已有文档试跑', children: !selected ? <Empty description="请选择规则版本" /> : <Card title={`使用 v${selected.version} 试跑`}>
            <Space direction="vertical" size={14} style={{ width: '100%' }}>
              <Typography.Paragraph type="secondary">复用已登记文档，不重新上传。文档所属清单须与所选快照一致。试跑不切换正式发布版本，提取效果需结合原文和标注另行验证。</Typography.Paragraph>
              <Input aria-label="试跑文档 ID" placeholder="已有文档 ID（UUID）" value={documentId} disabled={disabled} onChange={(e) => setDocumentId(e.target.value)} />
              <Checkbox checked={refreshArtifact} disabled={disabled} onChange={(e) => setRefreshArtifact(e.target.checked)}>强制重建前处理（耗时更长，默认复用现有产物）</Checkbox>
              <Space wrap><Button type="primary" icon={<ExperimentOutlined />} disabled={disabled || !documentId.trim()} loading={busy === 'trial'} onClick={() => void startTrial()}>提交此版本试跑</Button>{trialRunId && <><Tag>{trialStatus}</Tag><Button disabled={disabled} loading={busy === 'trialStatus'} onClick={() => void loadTrial()}>刷新试跑状态</Button><Link to={`/result/${trialRunId}`}>查看试跑结果</Link></>}</Space>
              {trialRunId && <Typography.Text copyable>{trialRunId}</Typography.Text>}
              <Typography.Title level={5}>运行事实差异</Typography.Title>
              <Space.Compact style={{ width: '100%' }}><Input aria-label="对比前运行 ID" placeholder="对比前运行 ID" value={beforeRunId} disabled={disabled} onChange={(e) => { setBeforeRunId(e.target.value); setComparison(undefined) }} /><Input aria-label="对比后运行 ID" placeholder="对比后运行 ID" value={afterRunId} disabled={disabled} onChange={(e) => { setAfterRunId(e.target.value); setComparison(undefined) }} /></Space.Compact>
              <Button disabled={disabled || !beforeRunId.trim() || !afterRunId.trim()} loading={busy === 'compare'} onClick={() => void compare()}>比较完整事实</Button>
              {comparison && <pre style={jsonStyle}>{pretty(comparison)}</pre>}
            </Space>
          </Card> },
          { key: 'publish', label: '发布与回滚', children: !selected ? <Empty description="请选择规则版本" /> : <Card title={`操作目标：v${selected.version}`}>
            <Space direction="vertical" size={14} style={{ width: '100%' }}>
              {selected.base_generation !== generation && !selected.has_been_published && <Alert type="warning" showIcon message="草稿基版本已过期，请先以当前内容建立新草稿，再重新预览。" />}
              <Typography.Paragraph type="secondary">当前发布代数 {generation}，所选快照基础代数 {selected.base_generation}。发布影响后续解析任务，历史运行保持原始规则快照；回滚只能选择曾发布的版本。</Typography.Paragraph>
              <Input aria-label="发布操作人" placeholder="操作人（必填）" maxLength={100} value={operator} disabled={disabled} onChange={(e) => setOperator(e.target.value)} />
              <Input.TextArea aria-label="发布或回滚原因" placeholder="发布或回滚原因（必填）" maxLength={4000} autoSize={{ minRows: 2, maxRows: 4 }} value={reason} disabled={disabled} onChange={(e) => setReason(e.target.value)} />
              <Input.TextArea aria-label="样本运行 ID" placeholder="已完成样本运行 ID（可选，换行或逗号分隔，须对应此清单和知识 hash）" autoSize={{ minRows: 2, maxRows: 5 }} value={sampleIds} disabled={disabled} onChange={(e) => setSampleIds(e.target.value)} />
              <Input.TextArea aria-label="样本报告 JSON" placeholder="样本报告 JSON 对象（可选，记录为人工提供，平台不自动认证）" autoSize={{ minRows: 3, maxRows: 8 }} value={sampleReport} disabled={disabled} onChange={(e) => setSampleReport(e.target.value)} spellCheck={false} />
              <Space wrap><Button type="primary" disabled={disabled || selected.is_active || selected.base_generation !== generation || !operator.trim() || !reason.trim()} loading={busy === 'publish'} onClick={() => void activate(false)}>发布所选版本</Button><Button danger icon={<RollbackOutlined />} disabled={disabled || selected.is_active || !selected.has_been_published || !operator.trim() || !reason.trim()} loading={busy === 'rollback'} onClick={() => void activate(true)}>回滚至所选历史版本</Button></Space>
            </Space>
          </Card> },
          { key: 'history', label: '发布审计', children: <Card title="版本操作记录"><Table<ReleaseEvent> rowKey="event_id" dataSource={history} scroll={{ x: 850 }} pagination={{ pageSize: 10 }} columns={[
            { title: '动作', dataIndex: 'action', render: (value: string) => ({ DRAFT: '创建草稿', PUBLISH: '发布', ROLLBACK: '回滚' }[value] || value) },
            { title: '代数', dataIndex: 'generation' }, { title: '版本 ID', dataIndex: 'bundle_id', ellipsis: true },
            { title: '操作人', dataIndex: 'operator' }, { title: '原因', dataIndex: 'reason', ellipsis: true },
            { title: '样本数', render: (_, event) => event.sample_runs.length }, { title: '时间', dataIndex: 'created_at', render: (value: string) => new Date(value).toLocaleString('zh-CN') },
          ]} expandable={{ expandedRowRender: (event) => <pre style={jsonStyle}>{pretty(event)}</pre> }} /></Card> },
        ]} />
      </>}
    </Space>
  </div>
}
