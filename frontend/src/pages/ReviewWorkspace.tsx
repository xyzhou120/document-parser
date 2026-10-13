import { useEffect, useState } from 'react'
import { Alert, Button, Input, Select, Space, Table, Tag, message } from 'antd'
import { DownloadOutlined, ReloadOutlined } from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import type { ChecklistType } from '@/types/api'
import { apiError } from '@/services/api'
import { getChecklistTypes } from '@/services/rules'
import { getReviewAudit, getReviewWorkspace } from '@/services/reviewWorkspace'
import type { ReviewState, ReviewWorkspaceItem } from '@/services/reviewWorkspace'

const states = [{ value: 'UNREVIEWED', label: '未复核' }, { value: 'HUMAN_REVIEWED', label: '已有人工修订' }, { value: 'REJECTED', label: '最新修订已拒绝' }]

export default function ReviewWorkspace() {
  const navigate = useNavigate()
  const [state, setState] = useState<ReviewState>('UNREVIEWED')
  const [checklist, setChecklist] = useState<string>()
  const [mine, setMine] = useState('')
  const [mineQuery, setMineQuery] = useState('')
  const [checklists, setChecklists] = useState<ChecklistType[]>([])
  const [rows, setRows] = useState<ReviewWorkspaceItem[]>([])
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [reload, setReload] = useState(0)
  useEffect(() => { void getChecklistTypes().then(setChecklists).catch((failure) => setError(apiError(failure, '读取清单类型失败'))) }, [])
  useEffect(() => {
    let disposed = false
    setLoading(true); setError('')
    void getReviewWorkspace({ state, checklist_type: checklist, mine_name: mineQuery || undefined, page, page_size: pageSize })
      .then((result) => { if (!disposed) { setRows(result.items); setTotal(result.total) } })
      .catch((failure) => { if (!disposed) setError(apiError(failure, '读取核对工作台失败')) })
      .finally(() => { if (!disposed) setLoading(false) })
    return () => { disposed = true }
  }, [state, checklist, mineQuery, page, pageSize, reload])
  const exportAudit = async (row: ReviewWorkspaceItem) => {
    try {
      const audit = await getReviewAudit(row.parse_run_id)
      const url = URL.createObjectURL(new Blob([JSON.stringify(audit, null, 2)], { type: 'application/json' }))
      const link = document.createElement('a'); link.href = url; link.download = `复核审计-${row.parse_run_id}.json`; link.click(); URL.revokeObjectURL(url)
    } catch (failure) { message.error(apiError(failure, '导出审计失败')) }
  }
  return <div className="list-page">
    <div className="page-title-row"><div><h1>人工核对工作台</h1><p>按风险与业务需要选择字段核对，自动事实和人工修订分别保留。</p></div><Button icon={<ReloadOutlined />} loading={loading} onClick={() => setReload((value) => value + 1)}>刷新</Button></div>
    <Alert type="info" showIcon message="这是可选的核对入口，不要求全部字段人工审核，也不会将人工修订自动认定为准确率或 VERIFIED。下游仍可依据事实、证据、风险与置信度自行判断。" style={{ marginBottom: 16 }} />
    <Space wrap style={{ marginBottom: 16 }}>
      <Select aria-label="人工复核状态" style={{ width: 190 }} value={state} options={states} onChange={(value) => { setState(value); setPage(1) }} />
      <Select aria-label="清单类型" allowClear placeholder="全部清单" style={{ width: 210 }} value={checklist} options={checklists.map((item) => ({ value: item.checklist_type, label: item.checklist_name }))} onChange={(value) => { setChecklist(value); setPage(1) }} />
      <Input.Search aria-label="煤矿名称" placeholder="煤矿名称" allowClear style={{ width: 240 }} value={mine} onChange={(event) => setMine(event.target.value)} onSearch={(value) => { setMineQuery(value.trim()); setPage(1) }} />
    </Space>
    {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 16 }} />}
    <div className="list-surface"><Table rowKey={(row) => `${row.parse_run_id}-${row.field_key}`} dataSource={rows} loading={loading} scroll={{ x: 1300 }} pagination={{ current: page, pageSize, total, showSizeChanger: true, pageSizeOptions: [20, 50, 100], onChange: (next, size) => { setPage(next); setPageSize(size) } }} columns={[
      { title: '文档 / 字段', key: 'field', width: 240, render: (_, row: ReviewWorkspaceItem) => <><strong>{row.field_name}</strong>{row.required && <Tag color="red" style={{ marginLeft: 6 }}>必填</Tag>}<span className="table-subtitle">{row.field_key}</span><span className="table-subtitle">{row.file_name}</span></> },
      { title: '归属', key: 'context', width: 190, render: (_, row: ReviewWorkspaceItem) => <>{row.mine_name || '未填写煤矿'}<span className="table-subtitle">{row.checklist_type || '未标注清单'} · {row.reporting_period || '未填写周期'}</span></> },
      { title: '自动候选', key: 'candidate', width: 130, render: (_, row: ReviewWorkspaceItem) => <>{row.fact_count} 条事实<span className="table-subtitle">{row.field_value || (row.fact_count ? '多事实详情见结果' : '未发现候选')}</span></> },
      { title: '风险提示', key: 'risk', width: 220, render: (_, row: ReviewWorkspaceItem) => row.risk_codes.length ? <Space wrap>{row.risk_codes.map((code) => <Tag color="orange" key={code}>{code}</Tag>)}</Space> : <span className="muted">未记录风险（不代表已核验）</span> },
      { title: '最新人工审计', key: 'audit', width: 220, render: (_, row: ReviewWorkspaceItem) => row.latest_review ? <><Tag color={row.latest_review.review_status === 'REJECTED' ? 'red' : 'blue'}>{row.latest_review.action} · v{row.latest_review.revision}</Tag><span className="table-subtitle">{row.latest_review.reviewer} · {row.latest_review.created_at}</span><span className="table-subtitle">{row.latest_review.reason || '未填写原因'}</span></> : <Tag>未复核</Tag> },
      { title: '操作', key: 'actions', width: 190, render: (_, row: ReviewWorkspaceItem) => <Space wrap><Button size="small" type="primary" onClick={() => navigate(row.result_url)}>原文与事实核对</Button><Button size="small" icon={<DownloadOutlined />} onClick={() => void exportAudit(row)}>运行审计 JSON</Button></Space> },
    ]} locale={{ emptyText: '当前筛选下没有字段记录' }} /></div>
  </div>
}
