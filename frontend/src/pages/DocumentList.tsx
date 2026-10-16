import { useCallback, useEffect, useState } from 'react'
import { Alert, Button, Empty, Input, Select, Table, Tag, Tabs } from 'antd'
import { EyeOutlined, ReloadOutlined, SearchOutlined, ExportOutlined } from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import type { ColumnsType } from 'antd/es/table'
import dayjs from 'dayjs'
import { queryParseResults } from '@/services/parse'
import { getChecklistTypes } from '@/services/rules'
import { apiError } from '@/services/api'
import type { ChecklistType, ParseRun } from '@/types/api'
import { executionStatusNames } from '@/services/execution'
import AttachmentList from './AttachmentList'

export const statusLabels: Record<string, string> = {
  ...executionStatusNames,
  PENDING: '等待处理',
  RUNNING: '解析中',
  PROCESSING: '解析中',
  RETRYING: '等待重试',
  COMPLETED: '解析结束',
  FAILED: '失败',
}

export function exportRunsCsv(rows: ParseRun[]) {
  if (!rows.length) return
  const header = ['文档编号', '文档名称', '煤矿', '清单类型', '上报周期', '状态', '字段数', '平均方法匹配分(未经正确率校准)', '耗时(秒)', '创建时间']
  const escape = (value: unknown) => {
    let text = String(value ?? '')
    if (/^\s*[=+\-@]/.test(text)) text = `'${text}`
    return `"${text.replace(/"/g, '""')}"`
  }
  const lines = [
    header,
    ...rows.map((row) => [
      row.document_id,
      row.file_name,
      row.mine_name || '',
      row.checklist_type || '',
      row.reporting_period || '',
      statusLabels[row.status] || row.status,
      row.fields_extracted ?? 0,
      row.mean_confidence == null ? '' : row.mean_confidence.toFixed(3),
      row.duration_seconds == null ? '' : row.duration_seconds.toFixed(1),
      dayjs(`${row.created_at}Z`).format('YYYY-MM-DD HH:mm:ss'),
    ]),
  ]
  const blob = new Blob(['﻿' + lines.map((line) => line.map(escape).join(',')).join('\r\n')], {
    type: 'text/csv;charset=utf-8',
  })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = `解析记录-${dayjs().format('YYYYMMDD-HHmm')}.csv`
  link.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

interface Props {
  /** 页面标题，文档管理与批量查询复用同一实现 */
  title: string
  subtitle: string
}

function LegacyDocumentList({ title, subtitle }: Props) {
  const navigate = useNavigate()
  const [rows, setRows] = useState<ParseRun[]>([])
  const [types, setTypes] = useState<ChecklistType[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const [mine, setMine] = useState('')
  const [status, setStatus] = useState<string>()
  const [checklist, setChecklist] = useState<string>()
  const [period, setPeriod] = useState('')

  const [query, setQuery] = useState<Record<string, string | undefined>>({})
  const [pagination, setPagination] = useState({ page: 1, page_size: 20 })
  const [total, setTotal] = useState(0)
  const [revision, setRevision] = useState(0)

  useEffect(() => {
    getChecklistTypes()
      .then(setTypes)
      .catch(() => setTypes([]))
  }, [])

  useEffect(() => {
    let disposed = false
    setLoading(true)
    setError('')
    queryParseResults({
      ...query,
      page: pagination.page,
      page_size: pagination.page_size,
    } as never)
      .then((data) => {
        if (disposed) return
        setRows(data.items)
        setTotal(data.total)
      })
      .catch((failure) => {
        if (disposed) return
        setError(apiError(failure, '加载解析记录失败。'))
        setRows([])
        setTotal(0)
      })
      .finally(() => {
        if (!disposed) setLoading(false)
      })
    return () => {
      disposed = true
    }
  }, [query, pagination, revision])

  const applyQuery = useCallback(() => {
    setQuery({
      mine_name: mine.trim() || undefined,
      status: status || undefined,
      checklist_type: checklist || undefined,
      reporting_period: period.trim() || undefined,
    })
    setPagination((current) => ({ ...current, page: 1 }))
  }, [mine, status, checklist, period])

  const reset = () => {
    setMine('')
    setStatus(undefined)
    setChecklist(undefined)
    setPeriod('')
    setQuery({})
    setPagination({ page: 1, page_size: pagination.page_size })
  }

  const columns: ColumnsType<ParseRun> = [
    {
      title: '文档',
      dataIndex: 'file_name',
      ellipsis: true,
      render: (value: string, row) => (
        <div>
          <strong>{value}</strong>
          <small className="table-subtitle">
            {row.mine_name || '未填写煤矿'} · {row.reporting_period || '未填写周期'}
          </small>
        </div>
      ),
    },
    {
      title: '清单类型',
      dataIndex: 'checklist_type',
      width: 170,
      render: (value?: string) => {
        if (!value) return '—'
        const match = types.find((t) => t.checklist_type === value)
        return match ? match.checklist_name : value
      },
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 100,
      render: (value: string) => (
        <Tag
          color={
            value === 'COMPLETED' ? 'green' : value === 'FAILED' ? 'red' : value ? 'blue' : 'default'
          }
        >
          {statusLabels[value] || value}
        </Tag>
      ),
    },
    { title: '字段数', dataIndex: 'fields_extracted', width: 80, render: (v?: number) => v ?? 0 },
    {
      title: '平均方法匹配分',
      dataIndex: 'mean_confidence',
      width: 110,
      render: (value?: number | null) =>
        value == null ? '—' : <span title="方法匹配分，未经正确率校准">{value.toFixed(3)}</span>,
    },
    {
      title: '耗时',
      dataIndex: 'duration_seconds',
      width: 90,
      render: (value?: number | null) => (value == null ? '—' : `${value.toFixed(1)} 秒`),
    },
    {
      title: '创建时间',
      dataIndex: 'created_at',
      width: 155,
      render: (value: string) => dayjs(`${value}Z`).format('YYYY-MM-DD HH:mm'),
    },
    {
      title: '操作',
      width: 80,
      render: (_, row) => (
        <Button type="link" size="small" icon={<EyeOutlined />} onClick={() => navigate(`/result/${row.parse_run_id}`)}>
          查看
        </Button>
      ),
    },
  ]

  return (
    <div className="list-page">
      <div className="page-title-row">
        <div>
          <h1>{title}</h1>
          <p>{subtitle}</p>
        </div>
        <div className="page-actions">
          <Button icon={<ExportOutlined />} disabled={!rows.length} onClick={() => exportRunsCsv(rows)}>
            导出当前页
          </Button>
          <Button icon={<ReloadOutlined />} loading={loading} onClick={() => setRevision((v) => v + 1)}>
            刷新
          </Button>
        </div>
      </div>

      <div className="list-toolbar">
        <Input
          value={mine}
          onChange={(e) => setMine(e.target.value)}
          onPressEnter={applyQuery}
          placeholder="煤矿名称"
          allowClear
        />
        <Select
          value={checklist}
          onChange={setChecklist}
          allowClear
          placeholder="清单类型"
          options={types.map((t) => ({ value: t.checklist_type, label: t.checklist_name }))}
        />
        <Select
          value={status}
          onChange={setStatus}
          allowClear
          placeholder="任务状态"
          options={Object.entries(statusLabels).map(([value, label]) => ({ value, label }))}
        />
        <Input
          value={period}
          onChange={(e) => setPeriod(e.target.value)}
          onPressEnter={applyQuery}
          placeholder="上报周期 YYYY-MM"
          allowClear
        />
        <Button type="primary" icon={<SearchOutlined />} onClick={applyQuery}>
          查询
        </Button>
        <Button onClick={reset}>重置</Button>
      </div>

      {error && <Alert className="run-alert" type="error" showIcon message={error} />}

      <div className="list-surface">
        <Table
          rowKey="parse_run_id"
          loading={loading}
          columns={columns}
          dataSource={rows}
          scroll={{ x: 980 }}
          pagination={{
            current: pagination.page,
            pageSize: pagination.page_size,
            total,
            showSizeChanger: true,
            showTotal: (count) => `共 ${count} 条`,
            onChange: (page, page_size) => setPagination({ page, page_size }),
          }}
          locale={{
            emptyText: (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description={error ? '未能加载记录' : '暂无解析记录，先上传一份 PDF 文档'}
              />
            ),
          }}
        />
      </div>
    </div>
  )
}

export default function DocumentList(props: Props) {
  return <Tabs defaultActiveKey="attachments" items={[
    { key: 'attachments', label: '附件与当前结果', children: <AttachmentList /> },
    { key: 'legacy', label: '旧版解析记录', children: <LegacyDocumentList {...props} /> },
  ]} />
}
