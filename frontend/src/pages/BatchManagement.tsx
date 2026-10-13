import { useCallback, useEffect, useState } from 'react'
import { Alert, Button, Card, Empty, Input, Progress, Select, Space, Table, Tag, Upload, message } from 'antd'
import { DownloadOutlined, ReloadOutlined, CloudUploadOutlined, RollbackOutlined } from '@ant-design/icons'
import type { UploadFile } from 'antd'
import { apiError } from '@/services/api'
import { batchExportUrl, cancelBatchItem, createBatch, getBatch, listBatches, retryBatchItem, uploadBatchItem, type BatchItem, type BatchItemInput, type BatchSummary } from '@/services/batches'
import { getChecklistTypes } from '@/services/rules'

interface Draft extends BatchItemInput { file: File }

const labels: Record<string, string> = { PENDING: '待上传', UPLOADING: '上传中', PROCESSING: '解析中', RUNNING: '解析中', COMPLETED: '已完成', PARSE_FAILED: '解析失败', UPLOAD_FAILED: '上传失败', CANCELLED: '已取消', PARTIAL_FAILED: '部分失败' }
const statusColor: Record<string, string> = { COMPLETED: 'success', PARSE_FAILED: 'error', UPLOAD_FAILED: 'error', CANCELLED: 'default', PROCESSING: 'processing', RUNNING: 'processing' }

export default function BatchManagement() {
  const [batches, setBatches] = useState<BatchSummary[]>([])
  const [selected, setSelected] = useState<BatchSummary | null>(null)
  const [name, setName] = useState('')
  const [drafts, setDrafts] = useState<Draft[]>([])
  const [types, setTypes] = useState<Array<{ value: string; label: string }>>([])
  const [creating, setCreating] = useState(false)
  const [page, setPage] = useState(1)
  const [total, setTotal] = useState(0)
  const [uploading, setUploading] = useState<string>()
  const [error, setError] = useState('')
  const refresh = useCallback(async () => {
    try { const data = await listBatches({ page, page_size: 10 }); setBatches(data.items); setTotal(data.total) } catch (e) { setError(apiError(e, '批次加载失败。')) }
  }, [page])
  useEffect(() => { void refresh() }, [refresh])
  useEffect(() => { void getChecklistTypes().then((rows) => setTypes(rows.filter((row) => row.is_active).map((row) => ({ value: row.checklist_type, label: row.checklist_name })))).catch(() => setError('清单类型加载失败，请刷新页面。')) }, [])
  const selectedId = selected?.batch_id
  useEffect(() => {
    if (!selectedId) return
    let disposed = false
    const timer = window.setInterval(() => { void getBatch(selectedId).then((batch) => { if (!disposed) setSelected(batch) }).catch(() => undefined) }, 3000)
    return () => { disposed = true; window.clearInterval(timer) }
  }, [selectedId])
  const open = async (id: string) => { try { setSelected(await getBatch(id)) } catch (e) { setError(apiError(e, '批次详情加载失败。')) } }
  const create = async () => {
    if (!name.trim()) return message.warning('请先输入批次名称')
    if (!drafts.length) return message.warning('请先选择PDF，并填写每份文件的煤矿、清单和周期')
    setCreating(true); setError('')
    try {
      const items: BatchItemInput[] = []
      for (const { file, ...item } of drafts) {
        const hash = crypto.subtle ? Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', await file.arrayBuffer()))).map((v) => v.toString(16).padStart(2, '0')).join('') : undefined
        items.push({ ...item, file_hash: hash })
      }
      const created: BatchSummary = await createBatch({ name: name.trim(), items })
      setSelected(created); setName(''); setDrafts([])
      const failures: string[] = []
      for (const item of created.items || []) {
        const file = drafts.find((draft) => draft.client_key === item.client_key)?.file
        if (!file) continue
        try { await uploadBatchItem(created.batch_id, item, file) } catch (e) { failures.push(`${item.file_name}：${apiError(e, '上传失败')}`) }
      }
      if (failures.length) setError(failures.join('；'))
      await refresh(); await open(created.batch_id)
    } catch (e) { setError(apiError(e, '批次创建失败，请检查每份文件的元数据。')) } finally { setCreating(false) }
  }
  const updateDraft = (key: string, column: keyof BatchItemInput, value: string) => setDrafts((rows) => rows.map((row) => row.client_key === key ? { ...row, [column]: value } : row))
  const handleUpload = async (item: BatchItem, file: UploadFile) => {
    if (!file.originFileObj || !selected) return
    setUploading(item.item_id); setError('')
    try { await uploadBatchItem(selected.batch_id, item, file.originFileObj); await open(selected.batch_id); await refresh() } catch (e) { setError(apiError(e, '文件上传失败。')) } finally { setUploading(undefined) }
  }
  const retry = async (item: BatchItem) => { if (!selected) return; try { await retryBatchItem(selected.batch_id, item); await open(selected.batch_id); await refresh() } catch (e) { setError(apiError(e, '重试失败。')) } }
  const cancel = async (item: BatchItem) => { if (!selected) return; try { await cancelBatchItem(selected.batch_id, item); await open(selected.batch_id); await refresh() } catch (e) { setError(apiError(e, '取消失败。')) } }
  const items = selected?.items || []
  return <div style={{ padding: 24 }}>
    <Space direction="vertical" size={18} style={{ width: '100%' }}>
      <div><h1 style={{ marginBottom: 6 }}>批次管理</h1><div style={{ color: '#667085' }}>跨刷新保留批次进度，支持失败文件恢复和全量事实导出。</div></div>
      {error && <Alert type="error" showIcon closable onClose={() => setError('')} message={error} />}
      <Card title="新建批次" extra={<Button type="primary" loading={creating} icon={<CloudUploadOutlined />} onClick={() => void create()}>登记并上传</Button>}>
        <Space direction="vertical" style={{ width: '100%' }}>
        <Input value={name} disabled={creating} onChange={(e) => setName(e.target.value)} placeholder="批次名称，例如：2026年10月安全生产月报" />
        <Upload accept=".pdf" multiple showUploadList={false} disabled={creating} beforeUpload={(file) => { setDrafts((rows) => [...rows, { client_key: crypto.randomUUID(), file_name: file.name, file, mine_id: '', mine_name: '', checklist_type: '', reporting_period: '' }]); return false }}><Button>选择PDF文件</Button></Upload>
        {drafts.length > 0 && <Table rowKey="client_key" dataSource={drafts} scroll={{ x: 980 }} pagination={false} columns={[
          { title: '文件', dataIndex: 'file_name', width: 220 },
          { title: '煤矿编号', render: (_: unknown, row: Draft) => <Input aria-label={`${row.file_name} 煤矿编号`} disabled={creating} value={row.mine_id} onChange={(e) => updateDraft(row.client_key, 'mine_id', e.target.value)} /> },
          { title: '煤矿名称', render: (_: unknown, row: Draft) => <Input aria-label={`${row.file_name} 煤矿名称`} disabled={creating} value={row.mine_name} onChange={(e) => updateDraft(row.client_key, 'mine_name', e.target.value)} /> },
          { title: '清单', width: 190, render: (_: unknown, row: Draft) => <Select aria-label={`${row.file_name} 清单`} disabled={creating} style={{ width: '100%' }} value={row.checklist_type || undefined} options={types} onChange={(value) => updateDraft(row.client_key, 'checklist_type', value)} /> },
          { title: '周期', width: 140, render: (_: unknown, row: Draft) => <Input aria-label={`${row.file_name} 周期`} disabled={creating} placeholder="YYYY-MM" value={row.reporting_period} onChange={(e) => updateDraft(row.client_key, 'reporting_period', e.target.value)} /> },
          { title: '操作', width: 80, render: (_: unknown, row: Draft) => <Button disabled={creating} onClick={() => setDrafts((rows) => rows.filter((draft) => draft.client_key !== row.client_key))}>移除</Button> },
        ]} />}
        </Space>
      </Card>
      <Card title="批次列表" extra={<Button icon={<ReloadOutlined />} onClick={() => void refresh()}>刷新</Button>}>
        <Table rowKey="batch_id" dataSource={batches} scroll={{ x: 650 }} pagination={{ current: page, pageSize: 10, total, onChange: setPage }} columns={[
          { title: '批次', dataIndex: 'name', render: (v: string, row: BatchSummary) => <Button type="link" onClick={() => void open(row.batch_id)}>{v}</Button> }, { title: '状态', dataIndex: 'status', render: (v: string) => <Tag color={statusColor[v]}>{labels[v] || v}</Tag> },
          { title: '进度', dataIndex: 'progress_percent', render: (v: number, row: BatchSummary) => <Progress percent={v} size="small" format={() => `${row.completed}/${row.total}`} /> },
          { title: '失败', dataIndex: 'failed', render: (v: number) => v ? <Tag color="error">{v}</Tag> : '0' },
        ]} />
      </Card>
      {selected && <Card title={<Space><span>{selected.name}</span><Tag color={statusColor[selected.status]}>{labels[selected.status] || selected.status}</Tag></Space>} extra={<Button icon={<DownloadOutlined />} href={batchExportUrl({ batch_id: selected.batch_id })} target="_blank">导出全部事实</Button>}>
        <p style={{ color: '#667085' }}>已登记的文件与进度会保留。刷新后待上传文件需重新选择原PDF；解析失败直接重试，无需重复上传。取消仅适用于尚未提交解析的文件。</p>
        <Table rowKey="item_id" dataSource={items} scroll={{ x: 750 }} pagination={{ pageSize: 10 }} columns={[
          { title: '文件', dataIndex: 'file_name', ellipsis: true }, { title: '状态', dataIndex: 'status', render: (v: string, item: BatchItem) => <Tag color={statusColor[v]}>{v === 'PENDING' && item.document_id ? '等待解析' : labels[v] || v}</Tag> },
          { title: '煤矿 / 清单 / 周期', render: (_: unknown, item: BatchItem) => `${item.metadata.mine_name} / ${item.metadata.checklist_type} / ${item.metadata.reporting_period}` },
          { title: '尝试次数', dataIndex: 'attempts' }, { title: '错误', dataIndex: 'error_message', ellipsis: true },
          { title: '操作', render: (_: unknown, item: BatchItem) => <Space>{!item.document_id && ['PENDING', 'UPLOAD_FAILED'].includes(item.status) && <><Upload accept=".pdf" maxCount={1} showUploadList={false} beforeUpload={() => false} onChange={(info) => { const file = info.fileList[0]; if (file) void handleUpload(item, file) }}><Button loading={uploading === item.item_id}>选择原PDF</Button></Upload><Button disabled={uploading === item.item_id} onClick={() => void cancel(item)}>取消</Button></>}{item.status === 'PARSE_FAILED' && <Button icon={<RollbackOutlined />} onClick={() => void retry(item)}>重试解析</Button>}{item.parse_run_id && <Button type="link" href={`/result/${item.parse_run_id}`}>结果</Button>}</Space> },
        ]} locale={{ emptyText: <Empty description="批次中还没有文件" /> }} />
      </Card>}
    </Space>
  </div>
}
