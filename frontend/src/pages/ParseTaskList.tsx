import { useEffect, useState } from 'react'
import { Alert, Button, Input, Select, Table, Tag } from 'antd'
import { EyeOutlined, ReloadOutlined, SearchOutlined } from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import type { ColumnsType } from 'antd/es/table'
import dayjs from 'dayjs'
import { queryParseResults } from '@/services/parse'
import { getChecklistTypes } from '@/services/rules'
import { apiError } from '@/services/api'
import type { ChecklistType, ParseRun } from '@/types/api'

const statuses: Record<string, string> = { PENDING: '等待处理', RUNNING: '解析中', PROCESSING: '解析中', RETRYING: '等待重试', COMPLETED: '已完成', FAILED: '失败' }
export default function ParseTaskList() {
  const navigate = useNavigate()
  const [rows, setRows] = useState<ParseRun[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [mine, setMine] = useState('')
  const [status, setStatus] = useState<string>()
  const [checklist, setChecklist] = useState<string>()
  const [checklistTypes, setChecklistTypes] = useState<ChecklistType[]>([])
  const [query, setQuery] = useState<{ mine_name?: string; status?: string; checklist_type?: string }>({})
  const [pagination, setPagination] = useState({ page: 1, page_size: 20 })
  const [total, setTotal] = useState(0)
  const [revision, setRevision] = useState(0)
  useEffect(() => {
    getChecklistTypes().then(setChecklistTypes).catch(() => setChecklistTypes([]))
  }, [])
  useEffect(() => {
    let disposed = false
    setLoading(true)
    setError('')
    queryParseResults({ ...query, ...pagination }).then((data) => {
      if (!disposed) { setRows(data.items); setTotal(data.total) }
    }).catch((failure) => {
      if (!disposed) { setError(apiError(failure, '加载解析队列失败。')); setRows([]); setTotal(0) }
    }).finally(() => { if (!disposed) setLoading(false) })
    return () => { disposed = true }
  }, [query, pagination, revision])
  const columns: ColumnsType<ParseRun> = [
    { title: '文档', dataIndex: 'file_name', key: 'file_name', ellipsis: true, render: (value: string, row) => <div><strong>{value}</strong><small className="table-subtitle">{row.mine_name || '未填写煤矿'} · {row.reporting_period || '未填写周期'}</small></div> },
    { title: '状态', dataIndex: 'status', key: 'status', width: 100, render: (value: string) => <Tag color={value === 'COMPLETED' ? 'green' : value === 'FAILED' ? 'red' : 'blue'}>{statuses[value] || value}</Tag> },
    { title: '字段数', dataIndex: 'fields_extracted', key: 'fields_extracted', width: 90, render: (value?: number) => value ?? 0 },
    { title: '平均方法匹配分', dataIndex: 'mean_confidence', key: 'mean_confidence', width: 135, render: (value?: number | null) => value == null ? '—' : value.toFixed(3) },
    { title: '耗时', dataIndex: 'duration_seconds', key: 'duration_seconds', width: 90, render: (value?: number | null) => value == null ? '—' : `${value.toFixed(1)} 秒` },
    { title: '创建时间', dataIndex: 'created_at', key: 'created_at', width: 165, render: (value: string) => dayjs(`${value}Z`).format('YYYY-MM-DD HH:mm') },
    { title: '操作', key: 'action', width: 85, render: (_, row) => <Button type="link" icon={<EyeOutlined />} onClick={() => navigate(`/result/${row.parse_run_id}`)}>查看</Button> },
  ]
  return <div className="list-page"><div className="page-title-row"><div><h1>解析队列</h1><p>查看解析任务的真实状态，进入结果页核对字段与证据。</p></div><Button icon={<ReloadOutlined />} loading={loading} onClick={() => setRevision((value) => value + 1)}>刷新</Button></div>
    <div className="list-toolbar"><Input value={mine} onChange={(event) => setMine(event.target.value)} placeholder="按煤矿名称筛选" allowClear />
      <Select value={status} onChange={setStatus} allowClear placeholder="任务状态" options={Object.entries(statuses).map(([value, label]) => ({ value, label }))} />
      <Select value={checklist} onChange={setChecklist} allowClear placeholder="清单类型" options={checklistTypes.map((item) => ({ value: item.checklist_type, label: item.checklist_name }))} />
      <Button type="primary" icon={<SearchOutlined />} onClick={() => { setQuery({ mine_name: mine.trim() || undefined, status, checklist_type: checklist }); setPagination({ ...pagination, page: 1 }) }}>查询</Button>
    </div>{error && <Alert className="run-alert" type="error" showIcon message={error} />}
    <div className="list-surface"><Table rowKey="parse_run_id" loading={loading} columns={columns} dataSource={rows} scroll={{ x: 850 }} pagination={{ current: pagination.page, pageSize: pagination.page_size, total, showSizeChanger: true, showTotal: (count) => `共 ${count} 条`, onChange: (page, page_size) => setPagination({ page, page_size }) }} locale={{ emptyText: error ? '未能加载解析任务' : '暂无解析任务，先上传一份 PDF 文档' }} /></div>
  </div>
}
