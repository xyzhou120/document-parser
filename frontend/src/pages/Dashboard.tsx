import { useEffect, useMemo, useState } from 'react'
import { Alert, Button, Empty, Spin, Table, Tag } from 'antd'
import {
  CloudUploadOutlined,
  FileSearchOutlined,
  ReloadOutlined,
  SettingOutlined,
} from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import type { ColumnsType } from 'antd/es/table'
import dayjs from 'dayjs'
import Chart, { baseTooltip, baseValueAxis, chartTheme } from '@/components/Chart'
import { getOverview, getRecentRuns, getTrend } from '@/services/stats'
import { apiError } from '@/services/api'
import type { ParseRun, StatsOverview, TrendPoint } from '@/types/api'

const statusMeta: Record<string, { label: string; color: string }> = {
  PENDING: { label: '等待处理', color: 'default' },
  RUNNING: { label: '解析中', color: 'blue' },
  PROCESSING: { label: '解析中', color: 'blue' },
  RETRYING: { label: '等待重试', color: 'orange' },
  COMPLETED: { label: '已完成', color: 'green' },
  FAILED: { label: '失败', color: 'red' },
}

export default function Dashboard() {
  const navigate = useNavigate()
  const [overview, setOverview] = useState<StatsOverview>()
  const [trend, setTrend] = useState<TrendPoint[]>([])
  const [recent, setRecent] = useState<ParseRun[]>([])
  const [days, setDays] = useState(7)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const load = async () => {
    setLoading(true)
    setError('')
    try {
      const [summary, trendData, recentData] = await Promise.all([
        getOverview(),
        getTrend(days),
        getRecentRuns(8),
      ])
      setOverview(summary)
      setTrend(trendData.series)
      setRecent(recentData.items)
    } catch (failure) {
      setError(apiError(failure, '加载工作台数据失败，请确认后端服务已启动。'))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [days])

  const trendOption = useMemo(
    () => ({
      tooltip: baseTooltip,
      legend: {
        data: ['完成', '失败', '处理中'],
        right: 0,
        top: 0,
        textStyle: { color: chartTheme.text, fontSize: 11 },
      },
      grid: { left: 8, right: 8, top: 36, bottom: 4, containLabel: true },
      xAxis: {
        type: 'category' as const,
        data: trend.map((item) => dayjs(item.date).format('MM-DD')),
        axisLine: { lineStyle: { color: chartTheme.axisLine } },
        axisLabel: { color: chartTheme.text, fontSize: 11 },
        axisTick: { show: false },
      },
      yAxis: baseValueAxis,
      series: [
        {
          name: '完成',
          type: 'line' as const,
          smooth: true,
          data: trend.map((item) => item.completed),
          itemStyle: { color: chartTheme.success },
          areaStyle: { opacity: 0.08 },
        },
        {
          name: '失败',
          type: 'line' as const,
          smooth: true,
          data: trend.map((item) => item.failed),
          itemStyle: { color: chartTheme.danger },
        },
        {
          name: '处理中',
          type: 'line' as const,
          smooth: true,
          data: trend.map((item) => item.pending),
          itemStyle: { color: chartTheme.warning },
        },
      ],
    }),
    [trend],
  )

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
      title: '状态',
      dataIndex: 'status',
      width: 100,
      render: (value: string) => {
        const meta = statusMeta[value] || { label: value, color: 'default' }
        return <Tag color={meta.color}>{meta.label}</Tag>
      },
    },
    { title: '字段数', dataIndex: 'fields_extracted', width: 80, render: (v?: number) => v ?? 0 },
    {
      title: '平均方法匹配分',
      dataIndex: 'mean_confidence',
      width: 110,
      render: (value?: number | null) => value == null ? '—' : value.toFixed(3),
    },
    {
      title: '耗时',
      dataIndex: 'duration_seconds',
      width: 90,
      render: (value?: number | null) => (value == null ? '—' : `${value.toFixed(1)} 秒`),
    },
    {
      title: '时间',
      dataIndex: 'created_at',
      width: 150,
      render: (value: string) => dayjs(`${value}Z`).format('MM-DD HH:mm'),
    },
    {
      title: '操作',
      width: 80,
      render: (_, row) => (
        <Button type="link" size="small" onClick={() => navigate(`/result/${row.parse_run_id}`)}>
          查看
        </Button>
      ),
    },
  ]

  const hasData = (overview?.total_runs ?? 0) > 0

  return (
    <div className="list-page">
      <div className="page-title-row">
        <div>
          <h1>工作台</h1>
          <p>解析任务、字段质量与服务状态的实时概览，数据来自真实解析记录。</p>
        </div>
        <div className="page-actions">
          <Button icon={<ReloadOutlined />} loading={loading} onClick={() => void load()}>
            刷新
          </Button>
          <Button type="primary" icon={<CloudUploadOutlined />} onClick={() => navigate('/upload')}>
            上传文档
          </Button>
        </div>
      </div>

      {error && <Alert className="run-alert" type="error" showIcon message={error} />}

      {loading && !overview ? (
        <div className="loading-state">
          <Spin size="large" />
        </div>
      ) : (
        <>
          <div className="stat-grid">
            <article className="stat-card">
              <span className="stat-label">今日解析</span>
              <strong className="stat-value">{overview?.today_count ?? 0}</strong>
              <span className="stat-meta">
                {overview?.today_trend_percent == null
                  ? `昨日 ${overview?.yesterday_count ?? 0} 份`
                  : `较昨日 ${overview.today_trend_percent > 0 ? '+' : ''}${overview.today_trend_percent}%`}
              </span>
            </article>
            <article className="stat-card">
              <span className="stat-label">待处理任务</span>
              <strong className="stat-value">{overview?.pending_count ?? 0}</strong>
              <span className="stat-meta">已完成 {overview?.completed_count ?? 0} · 失败 {overview?.failed_count ?? 0}</span>
            </article>
            <article className="stat-card">
              <span className="stat-label">平均耗时</span>
              <strong className="stat-value">
                {overview?.avg_duration_seconds ?? '—'}
                {overview?.avg_duration_seconds != null && <em>秒</em>}
              </strong>
              <span className="stat-meta">累计 {overview?.total_runs ?? 0} 次解析</span>
            </article>
            <article className="stat-card">
              <span className="stat-label">平均方法匹配分</span>
              <strong className="stat-value">
                {overview?.avg_confidence == null
                  ? '—'
                  : overview.avg_confidence.toFixed(3)}
              </strong>
              <span className="stat-meta">
                成功率 {overview?.success_rate == null ? '—' : `${overview.success_rate}%`} · 待复核{' '}
                {overview?.review_count ?? 0}
              </span>
            </article>
          </div>

          <div className="dashboard-grid">
            <section className="panel">
              <div className="panel-heading">
                <h2 className="panel-title">解析趋势</h2>
                <div className="range-switch">
                  {[7, 30].map((value) => (
                    <button
                      key={value}
                      className={days === value ? 'active' : ''}
                      onClick={() => setDays(value)}
                    >
                      近 {value} 天
                    </button>
                  ))}
                </div>
              </div>
              {trend.length ? (
                <Chart option={trendOption} height={260} />
              ) : (
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无解析记录" />
              )}
            </section>

            <section className="panel">
              <div className="panel-heading">
                <h2 className="panel-title">快速操作</h2>
              </div>
              <div className="quick-actions">
                <button className="quick-action" onClick={() => navigate('/upload')}>
                  <CloudUploadOutlined />
                  <span>上传文档</span>
                  <small>选择 PDF 并启动解析</small>
                </button>
                <button className="quick-action" onClick={() => navigate('/documents')}>
                  <FileSearchOutlined />
                  <span>文档管理</span>
                  <small>筛选、查看解析结果</small>
                </button>
                <button className="quick-action" onClick={() => navigate('/rules')}>
                  <SettingOutlined />
                  <span>规则管理</span>
                  <small>配置清单与提取字段</small>
                </button>
              </div>
            </section>
          </div>

          <section className="panel section-gap">
            <div className="panel-heading">
              <h2 className="panel-title">最近解析任务</h2>
              <Button type="link" size="small" onClick={() => navigate('/documents')}>
                查看全部
              </Button>
            </div>
            <Table
              rowKey="parse_run_id"
              size="small"
              columns={columns}
              dataSource={recent}
              pagination={false}
              scroll={{ x: 820 }}
              locale={{
                emptyText: (
                  <Empty
                    image={Empty.PRESENTED_IMAGE_SIMPLE}
                    description={hasData ? '暂无最近任务' : '还没有解析记录，先上传一份 PDF 文档'}
                  />
                ),
              }}
            />
          </section>
        </>
      )}
    </div>
  )
}
