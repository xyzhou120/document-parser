import { useEffect, useState } from 'react'
import { Alert, Button, Empty, Input, Select, Space, Table, Tag } from 'antd'
import { useNavigate } from 'react-router-dom'
import dayjs from 'dayjs'
import { apiError } from '@/services/api'
import { Attachment, attachmentStates, downloadArtifact, listAttachments } from '@/services/attachments'

const periods = { day: '日', week: '周', ten_day: '旬', month: '月', quarter: '季', half_year: '半年', year: '年' }
export default function AttachmentList() {
  const navigate = useNavigate()
  const [draft, setDraft] = useState<Record<string, string>>({})
  const [query, setQuery] = useState<Record<string, unknown>>({})
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [rows, setRows] = useState<Attachment[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [exporting, setExporting] = useState(false)
  const [refresh, setRefresh] = useState(0)
  useEffect(() => {
    let active = true; setLoading(true); setError('')
    listAttachments({ ...query, page, page_size: pageSize }).then(data => {
      if (active) { setRows(data.items); setTotal(data.total) }
    }).catch(e => { if (active) setError(apiError(e, '附件加载失败，请重试。')) })
      .finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [page, pageSize, query, refresh])
  const input = (key: string, label: string) => <Input aria-label={label} placeholder={label} value={draft[key] || ''} onChange={e => setDraft({ ...draft, [key]: e.target.value })} />
  return <>
    <div className="filter-panel attachment-filters">
      {input('mine', '煤矿编码或名称')}{input('file_name_id', '上游清单 ID')}{input('name', '附件名称')}
      <Select aria-label="周期类型" allowClear placeholder="周期类型" value={draft.period_type || undefined} onChange={value => setDraft({ ...draft, period_type: value || '' })} options={Object.entries(periods).map(([value, label]) => ({ value, label }))} />
      {input('period_key', '所属周期键 / 原值')}{input('import_id', '导入批次 ID')}
      <Select aria-label="当前结果状态" allowClear placeholder="当前结果状态" value={draft.current_state || undefined} onChange={value => setDraft({ ...draft, current_state: value || '' })} options={[{ value: 'COMPLETED', label: '已有完整结果' }, { value: 'NONE', label: '尚无完整结果' }]} />
      <Select aria-label="最近任务状态" allowClear placeholder="最近任务状态" value={draft.latest_state || undefined} onChange={value => setDraft({ ...draft, latest_state: value || '' })} options={['RUNNING', 'PREPARING_CONTENT', 'COMPLETED', 'FAILED', 'PAUSED', 'INCOMPLETE'].map(value => ({ value, label: attachmentStates[value] }))} />
      <Select aria-label="文件格式" allowClear placeholder="文件格式" value={draft.format || undefined} onChange={value => setDraft({ ...draft, format: value || '' })} options={['pdf', 'doc', 'docx', 'xls', 'xlsx', 'dwg'].map(value => ({ value, label: value.toUpperCase() }))} />
      <Space wrap>
        <Button type="primary" onClick={() => { setQuery(Object.fromEntries(Object.entries(draft).filter(([, v]) => v.trim()))); setPage(1) }}>查询</Button>
        <Button onClick={() => { setDraft({}); setQuery({}); setPage(1) }}>重置</Button>
        <Button onClick={() => setRefresh(v => v + 1)}>刷新</Button>
        <Button onClick={() => navigate('/imports')}>Excel 导入</Button>
        <Button loading={exporting} disabled={!total} onClick={async () => {
          setExporting(true)
          try { await downloadArtifact(`/v1/attachments/export?${new URLSearchParams(Object.entries(query).map(([k, v]) => [k, String(v)]))}`, '附件当前结果.csv') }
          catch (e) { setError(apiError(e, '导出失败，请重试。')) }
          finally { setExporting(false) }
        }}>导出筛选结果</Button>
      </Space>
    </div>
    {error && <Alert type="error" showIcon message={error} action={<Button onClick={() => setRefresh(v => v + 1)}>重试</Button>} />}
    <Table<Attachment> rowKey="id" loading={loading} dataSource={rows} scroll={{ x: 1450 }} locale={{ emptyText: <Empty description="暂无附件，可上传文档或导入 Excel" /> }}
      pagination={{ current: page, pageSize, total, showSizeChanger: true, onChange: (p, size) => { setPage(p); setPageSize(size) } }} columns={[
        { title: '附件 / 煤矿', key: 'name', width: 260, render: (_, a) => <><strong>{a.attachment_name}</strong><small className="table-subtitle">{a.mine_name || a.mine_code} · {a.format.toUpperCase()}</small></> },
        { title: '清单 / 所属周期', key: 'scope', width: 220, render: (_, a) => <>{a.file_name || a.file_name_id}<small className="table-subtitle">{a.report_time_raw} · {periods[a.period_type as keyof typeof periods] || '未知周期'}</small></> },
        { title: '当前结果', key: 'current', width: 140, render: (_, a) => a.current ? <><Tag color={a.current_validity === 'VALID' ? 'green' : 'orange'}>V{a.current.number} · {a.current_validity === 'VALID' ? '完整结果' : a.current_validity === 'RECHECK_REQUIRED' ? '部分事实需核对' : '内容已变更'}</Tag></> : '尚无完整结果' },
        { title: '最近任务 / 获取状态', key: 'task', width: 190, render: (_, a) => <>{a.latest ? `V${a.latest.number} · ${attachmentStates[a.latest.state] || a.latest.state}` : attachmentStates[a.status] || a.status}<small className="table-subtitle">{attachmentStates[a.download_state] || a.download_state}{a.last_error ? ` · ${a.last_error}` : ''}</small></> },
        { title: '当前字段统计', key: 'stats', width: 160, render: (_, a) => a.current?.stats ? <>可用 {a.current.stats.available}/{a.current.stats.targets}<small className="table-subtitle">待复核 {a.current.stats.review_required} · 未找到 {a.current.stats.not_found}</small></> : '—' },
        { title: '匹配分', key: 'score', width: 90, render: (_, a) => <span title="同方法非空分数均值，未经正确率校准">{a.current?.stats?.mean_confidence?.toFixed(3) ?? '—'}</span> },
        { title: '当前结果耗时', key: 'duration', width: 110, render: (_, a) => a.current?.duration_seconds == null ? '—' : `${a.current.duration_seconds.toFixed(1)} 秒` },
        { title: '最近结果时间', key: 'completed', width: 150, render: (_, a) => a.current?.completed_at ? dayjs(`${a.current.completed_at}Z`).format('YYYY-MM-DD HH:mm') : '—' },
        { title: '首次登记', dataIndex: 'created_at', width: 150, render: v => dayjs(`${v}Z`).format('YYYY-MM-DD HH:mm') },
        { title: '操作', key: 'actions', fixed: 'right', width: 120, render: (_, a) => <Button type="link" onClick={() => navigate(`/attachments/${a.id}`)}>查看 / 历史</Button> },
      ]} />
  </>
}
