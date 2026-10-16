import { useCallback, useEffect, useState } from 'react'
import { Alert, Button, Empty, Input, Select, Space, Table, Tabs, Tag, Upload, message } from 'antd'
import { UploadOutlined } from '@ant-design/icons'
import { useSearchParams } from 'react-router-dom'
import { apiError } from '@/services/api'
import { getChecklistTypes } from '@/services/rules'
import type { ChecklistType } from '@/types/api'
import { ImportBatch, ImportRow, attachmentStates, confirmImport, controlImport, downloadArtifact, getImport, getImportGroups, listImports, saveMappings, uploadExcel } from '@/services/attachments'

type Group = { file_name_id: string; file_name: string; count: number; checklist_type?: string }
export default function ExcelImport() {
  const [params, setParams] = useSearchParams()
  const id = params.get('id') || ''
  const [batches, setBatches] = useState<ImportBatch[]>([])
  const [batch, setBatch] = useState<ImportBatch>()
  const [groups, setGroups] = useState<Group[]>([])
  const [groupTotal, setGroupTotal] = useState(0)
  const [groupPage, setGroupPage] = useState(1)
  const [types, setTypes] = useState<ChecklistType[]>([])
  const [mapping, setMapping] = useState<Record<string, string>>({})
  const [source, setSource] = useState('upstream')
  const [page, setPage] = useState(1)
  const [state, setState] = useState<string>()
  const [groupFilter, setGroupFilter] = useState('')
  const [selected, setSelected] = useState<string[]>([])
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const load = useCallback(async () => {
    setLoading(true)
    try {
      if (id) {
        const data = await getImport(id, { page, page_size: 50, state, file_name_id: groupFilter || undefined })
        setBatch(data)
      }
      const data = await listImports(); setBatches(data.items); setError('')
    } catch (e) { setError(apiError(e, '导入数据加载失败，请刷新重试。')) }
    finally { setLoading(false) }
  }, [id, page, state, groupFilter])
  useEffect(() => { void load() }, [load])
  useEffect(() => { getChecklistTypes().then(setTypes).catch(() => setTypes([])) }, [])
  const batchState = batch?.state
  useEffect(() => {
    if (!id || !batchState || ['PREVIEW_QUEUED', 'PREVIEW_FAILED'].includes(batchState)) return
    let active = true
    getImportGroups(id, groupPage).then(data => { if (active) { setGroups(data.items); setGroupTotal(data.total) } })
      .catch(e => { if (active) setError(apiError(e, '清单分组加载失败。')) })
    return () => { active = false }
  }, [id, groupPage, batchState])
  useEffect(() => {
    if (!id) return
    const timer = setInterval(() => { void load() }, 5000)
    return () => clearInterval(timer)
  }, [id, load])
  const act = async (action: () => Promise<unknown>, success: string) => {
    setBusy(true)
    try { await action(); message.success(success); await load() }
    catch (e) { setError(apiError(e, '操作失败，请重试。')) }
    finally { setBusy(false) }
  }
  const canConfirm = ['PREVIEW_READY', 'ACCEPTED'].includes(batch?.state || '')
  return <div className="page-stack">
    <div className="page-heading"><div><h1>Excel 附件导入</h1><p>先校验和映射清单，再确认登记。PDF 留存并解析，其他格式只登记。</p></div></div>
    <div className="filter-panel"><Space wrap>
      <Input aria-label="来源系统" value={source} onChange={e => setSource(e.target.value)} placeholder="来源系统" />
      <Upload accept=".xlsx" showUploadList={false} disabled={busy} beforeUpload={file => {
        void act(async () => { const data = await uploadExcel(file, source); setParams({ id: data.id }); setPage(1); setMapping({}); setSelected([]) }, '已上传，后台正在校验')
        return false
      }}><Button loading={busy} icon={<UploadOutlined />}>上传 Excel</Button></Upload>
      <Select aria-label="选择导入批次" style={{ minWidth: 280 }} placeholder="选择导入批次" value={id || undefined}
        options={batches.map(b => ({ value: b.id, label: `${b.file_name} · ${attachmentStates[b.state] || b.state}` }))}
        onChange={value => { setParams({ id: value }); setPage(1); setGroupPage(1); setMapping({}); setSelected([]); setBatch(undefined) }} />
      <Button onClick={() => void load()}>刷新</Button>
    </Space></div>
    {error && <Alert showIcon type="error" message={error} />}
    {batch?.last_error && <Alert showIcon type="error" message={batch.last_error} />}
    {!batch ? <Empty description="上传模板或选择一个导入批次" /> : <>
      <Space wrap><Tag>{attachmentStates[batch.state] || batch.state}</Tag><span>校验有效 {batch.counts.VALID || 0} 行 / 无效 {batch.counts.INVALID || 0} 行</span>
        <Button onClick={() => void act(() => downloadArtifact(`/v1/imports/${id}/errors`, '导入错误.csv'), '错误清单已导出')}>导出错误清单</Button>
        {canConfirm && <><Button type="primary" loading={busy} onClick={() => void act(() => confirmImport(id), '有效行已受理')}>确认全部有效行</Button>
          <Button disabled={!selected.length} loading={busy} onClick={() => void act(() => confirmImport(id, selected), '选中行已受理')}>确认选中行 ({selected.length})</Button></>}
      </Space>
      {batch.summary && <div className="import-summary">{Object.entries(batch.summary).map(([key, counts]) => <div key={key}><strong>{{ registration: '登记', download: '获取', parsing: '最近提取', result: '附件状态' }[key] || key}</strong><p>{Object.entries(counts).map(([s, n]) => `${attachmentStates[s] || s} ${n}`).join(' · ') || '暂无'}</p></div>)}</div>}
      {['REGISTERING', 'ACCEPTED', 'PAUSED'].includes(batch.state) && <Space wrap>
        {['pause', 'resume', 'retry', 'cancel'].map(action => <Button key={action} disabled={busy} onClick={() => void act(() => controlImport(id, action, groupFilter || undefined), '批次操作已受理')}>{{ pause: '暂停', resume: '继续', retry: '仅重试失败项', cancel: '取消' }[action]}{groupFilter ? '当前清单' : '批次'}</Button>)}
      </Space>}
      <Tabs items={[
        { key: 'rows', label: '附件预览 / 登记记录', children: <>
          <Space wrap className="attachment-toolbar"><Select allowClear aria-label="行状态" placeholder="行状态" value={state} onChange={v => { setState(v); setPage(1) }} options={['VALID', 'INVALID', 'SELECTED', 'REGISTERED', 'REUSED', 'CONFLICT'].map(v => ({ value: v, label: attachmentStates[v] }))} />
            <Input.Search allowClear aria-label="按上游清单 ID 筛选" placeholder="上游清单 ID" onSearch={v => { setGroupFilter(v); setPage(1) }} /></Space>
          <Table<ImportRow> rowKey="id" loading={loading} dataSource={batch.items || []} scroll={{ x: 1050 }}
            rowSelection={canConfirm ? { selectedRowKeys: selected, preserveSelectedRowKeys: true, onChange: keys => setSelected(keys.map(String)), getCheckboxProps: row => ({ disabled: row.state !== 'VALID' }) } : undefined}
            pagination={{ current: page, total: batch.total, pageSize: 50, showSizeChanger: false, onChange: setPage }} columns={[
              { title: 'Excel 位置', key: 'row', width: 160, render: (_, r) => `${r.sheet} · ${r.row_number}` },
              { title: '附件', key: 'name', width: 240, render: (_, r) => <>{r.data.attachment_name}<small className="table-subtitle">ID {r.data.attachment_id}</small></> },
              { title: '煤矿 / 清单', key: 'scope', width: 240, render: (_, r) => <>{r.data.mine_name}<small className="table-subtitle">{r.data.file_name}</small></> },
              { title: '所属期', key: 'period', render: (_, r) => r.data.report_time },
              { title: '状态 / 原因', key: 'state', width: 270, render: (_, r) => <>{attachmentStates[r.state] || r.state}<small className="table-subtitle">{r.errors.join('；') || (r.data.format !== 'pdf' ? '只登记，暂不支持解析' : batch.mappings[String(r.data.file_name_id)] ? 'PDF，可按映射清单解析' : '待清单映射，不启动提取')}</small></> },
            ]} />
        </> },
        { key: 'mappings', label: '按上游清单映射', children: <>
          <p>一个清单只需映射一次。未映射的 PDF 可先登记，完成映射后自动进入解析。</p>
          <Table<Group> rowKey="file_name_id" dataSource={groups} pagination={{ current: groupPage, pageSize: 50, total: groupTotal, showSizeChanger: false, onChange: setGroupPage }} columns={[
            { title: '上游清单 ID', dataIndex: 'file_name_id' }, { title: '上游清单名称', dataIndex: 'file_name' }, { title: '附件数', dataIndex: 'count' },
            { title: '平台清单', key: 'mapping', render: (_, g) => <Select aria-label={`映射 ${g.file_name}`} showSearch optionFilterProp="label" style={{ minWidth: 230 }}
              disabled={!['PREVIEW_READY', 'ACCEPTED'].includes(batch.state)} value={mapping[g.file_name_id] || batch.mappings[g.file_name_id] || undefined} placeholder="选择平台清单"
              options={types.map(t => ({ value: t.checklist_type, label: t.checklist_name }))} onChange={v => setMapping({ ...mapping, [g.file_name_id]: v })} /> },
          ]} />
          <Button type="primary" loading={busy} disabled={!Object.keys(mapping).length} onClick={() => void act(() => saveMappings(id, mapping), '映射已保存')}>保存清单映射</Button>
        </> },
      ]} />
    </>}
  </div>
}
