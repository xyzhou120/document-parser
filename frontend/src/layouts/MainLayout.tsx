import { useEffect, useState } from 'react'
import { Layout, Menu, Button, Tag } from 'antd'
import { Outlet, useNavigate, useLocation } from 'react-router-dom'
import {
  CloudUploadOutlined,
  DashboardOutlined,
  DatabaseOutlined,
  FileSearchOutlined,
  FilterOutlined,
  NodeIndexOutlined,
  SlidersOutlined,
  UnorderedListOutlined,
} from '@ant-design/icons'

const { Header, Sider, Content } = Layout

const titles: Record<string, { title: string; subtitle: string }> = {
  '/dashboard': { title: '工作台', subtitle: '解析任务与质量概览' },
  '/upload': { title: '文档上传', subtitle: '上传 PDF 并启动解析' },
  '/documents': { title: '文档管理', subtitle: '全部解析记录' },
  '/attachments': { title: '附件列表', subtitle: '按业务范围与当前结果查询' },
  '/imports': { title: 'Excel 附件导入', subtitle: '批量登记、校验与分组映射' },
  '/query': { title: '批量查询', subtitle: '组合筛选与导出' },
  '/batches': { title: '批次管理', subtitle: '批量登记、恢复与事实导出' },
  '/rule-releases': { title: '规则发布', subtitle: '固定版本、试跑、差异与回滚' },
  '/document-workspace': { title: '文档工作区', subtitle: '运行历史、影响预览与来源关联' },
  '/source-artifacts': { title: '源文订正', subtitle: '区域版本、影响与重算' },
  '/checklists': { title: '清单管理', subtitle: '清单信息与规则包配置' },
  '/review-workspace': { title: '复核工作台', subtitle: '按风险和审核状态进入核对' },
  '/tasks': { title: '解析队列', subtitle: '任务状态跟踪' },
  '/rules': { title: '规则管理', subtitle: '清单与字段提取规则' },
  '/services': { title: '服务管理', subtitle: '依赖服务连通性' },
  '/analytics': { title: '数据分析', subtitle: '效率与质量分布' },
}

export default function MainLayout() {
  const navigate = useNavigate()
  const location = useLocation()
  const [health, setHealth] = useState<'checking' | 'online' | 'offline'>('checking')

  useEffect(() => {
    let disposed = false
    const check = async () => {
      try {
        const base = import.meta.env.VITE_API_BASE_URL || '/api'
        const response = await fetch(`${base}/health`, { signal: AbortSignal.timeout(5000) })
        if (!disposed) setHealth(response.ok ? 'online' : 'offline')
      } catch {
        if (!disposed) setHealth('offline')
      }
    }
    void check()
    const timer = setInterval(check, 30000)
    return () => {
      disposed = true
      clearInterval(timer)
    }
  }, [])

  const items = [
    {
      key: 'workspace',
      type: 'group' as const,
      label: '工作空间',
      children: [
        { key: '/dashboard', icon: <DashboardOutlined />, label: '工作台' },
        { key: '/upload', icon: <CloudUploadOutlined />, label: '文档上传' },
        { key: '/documents', icon: <DatabaseOutlined />, label: '文档管理' },
        { key: '/attachments', icon: <DatabaseOutlined />, label: '附件列表' },
        { key: '/imports', icon: <CloudUploadOutlined />, label: 'Excel 导入' },
        { key: '/query', icon: <FilterOutlined />, label: '批量查询' },
        { key: '/batches', icon: <DatabaseOutlined />, label: '批次管理' },
        { key: '/document-workspace', icon: <FileSearchOutlined />, label: '文档工作区' },
      ],
    },
    {
      key: 'management',
      type: 'group' as const,
      label: '管理与分析',
      children: [
        { key: '/rules', icon: <SlidersOutlined />, label: '规则管理' },
        { key: '/rule-releases', icon: <SlidersOutlined />, label: '规则发布' },
        { key: '/source-artifacts', icon: <FileSearchOutlined />, label: '源文订正' },
        { key: '/checklists', icon: <SlidersOutlined />, label: '清单管理' },
        { key: '/review-workspace', icon: <FileSearchOutlined />, label: '复核工作台' },
        { key: '/services', icon: <NodeIndexOutlined />, label: '服务管理' },
        { key: '/analytics', icon: <UnorderedListOutlined />, label: '数据分析' },
      ],
    },
  ]

  const currentKey = location.pathname.startsWith('/result/') ? '/documents' : location.pathname
  const heading = titles[currentKey] || { title: '智能文档解析', subtitle: '' }

  return (
    <Layout className="app-shell">
      <Sider width={216} className="app-sider">
        <div className="side-brand">
          <svg className="brand-symbol" viewBox="0 0 50 50" aria-hidden="true">
            <path
              d="M4 28 15 11 23 15 30 5 45 23M2 35Q13 28 24 35T48 33M3 42Q14 37 25 41T48 40"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinejoin="round"
            />
          </svg>
          <div>
            <div className="side-brand-name">智能文档解析</div>
            <div className="side-brand-caption">DOCUMENT INTELLIGENCE</div>
          </div>
        </div>

        <div className="side-workspace">煤矿安全监察工作空间</div>

        <Menu
          mode="inline"
          selectedKeys={[currentKey]}
          items={items}
          onClick={({ key }) => navigate(key)}
          className="app-menu"
        />

        <div className="side-footer">
          <div className="health-line">
            <span
              className={`health-dot ${
                health === 'offline' ? 'offline' : health === 'checking' ? 'checking' : ''
              }`}
            />
            {health === 'online' ? 'API 已连接' : health === 'offline' ? 'API 未连接' : '正在连接 API'}
          </div>
          <div className="side-footer-meta">规则驱动解析 · v1.1</div>
        </div>
      </Sider>

      <Layout className="content-layout">
        <Header className="app-header">
          <div className="header-context">
            <div>
              <strong>{heading.title}</strong>
              {heading.subtitle && (
                <>
                  <span className="header-divider" />
                  <b>{heading.subtitle}</b>
                </>
              )}
            </div>
            <small>按清单配置提取字段，用原文核验结果</small>
          </div>
          <div className="header-actions">
            <Button type="primary" icon={<CloudUploadOutlined />} onClick={() => navigate('/upload')}>
              上传文档
            </Button>
            <Button icon={<FileSearchOutlined />} onClick={() => navigate('/documents')}>
              文档管理
            </Button>
            <Tag color={health === 'online' ? 'green' : 'default'}>
              {health === 'online' ? 'API 在线' : '等待 API'}
            </Tag>
          </div>
        </Header>
        <Content className="app-content">
          <Outlet />
        </Content>
      </Layout>
    </Layout>
  )
}
