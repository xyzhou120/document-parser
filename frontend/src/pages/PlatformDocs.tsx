import { useState } from 'react'
import { Button, Tag } from 'antd'
import { BookOutlined, FileTextOutlined, ArrowRightOutlined, ExperimentOutlined } from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'

const docs = [
  { name: '项目交接文档.md', desc: '系统边界、启动方式、交接注意事项', href: '/platform-docs/项目交接文档.md' },
  { name: '技术设计文档.md', desc: '数据模型、解析链路、服务分层与接口设计', href: '/platform-docs/技术设计文档.md' },
  { name: 'Phase0任务拆解.md', desc: '原始验证方案、阶段任务与验收标准', href: '/platform-docs/Phase0任务拆解.md' },
  { name: '项目进度与后续规划.md', desc: '当前进度、剩余风险与下一阶段推进内容', href: '/platform-docs/项目进度与后续规划.md' },
  { name: '快速启动指南.md', desc: '本地 API、Web、Celery Worker 启动命令', href: '/platform-docs/快速启动指南.md' },
]

function PlatformDocs() {
  const navigate = useNavigate()
  const [tab, setTab] = useState<'features' | 'docs' | 'roadmap'>('features')
  return <div className="docs-page">
    <div className="docs-heading"><div><div className="docs-kicker">KNOWLEDGE BASE / PLATFORM</div><h1>平台说明</h1><p>把系统能做什么、依据什么方案、接下来做什么，放在同一个可检索入口。</p></div><Button type="primary" onClick={() => navigate('/upload')} icon={<ExperimentOutlined />}>开始功能验证</Button></div>
    <div className="docs-tabs"><button className={tab === 'features' ? 'active' : ''} onClick={() => setTab('features')}>当前功能</button><button className={tab === 'docs' ? 'active' : ''} onClick={() => setTab('docs')}>原方案文档</button><button className={tab === 'roadmap' ? 'active' : ''} onClick={() => setTab('roadmap')}>后续推进</button></div>
    {tab === 'features' && <div className="docs-grid"><section className="docs-main-section"><div className="section-title"><BookOutlined /><h2>当前已具备的能力</h2></div><div className="feature-list">
      <article><span className="feature-index">01</span><div><h3>文档接入</h3><p>支持 PDF 单文件上传、大小校验、煤矿与上报周期标记，并将文件写入 MinIO。</p><Tag color="green">已接入</Tag></div></article>
      <article><span className="feature-index">02</span><div><h3>解析流水线</h3><p>API 创建解析任务，Celery Worker 异步执行，前端按状态轮询并展示任务进度。</p><Tag color="green">已接入</Tag></div></article>
      <article><span className="feature-index">03</span><div><h3>PDF + OCR</h3><p>先提取可搜索文本，对扫描页识别后补充 OCR 文本，再进入字段提取环节。</p><Tag color="green">已接入</Tag></div></article>
      <article><span className="feature-index">04</span><div><h3>字段提取与置信度</h3><p>当前默认覆盖煤矿名称、残余瓦斯临界值、报告日期、钻孔数量、抽采量五类字段，支持规则优先与 LLM 兜底。</p><Tag color="green">已接入</Tag></div></article>
      <article><span className="feature-index">05</span><div><h3>结果查询与清单配置</h3><p>可按煤矿、清单类型、状态查看解析运行，查看字段值、提取方法、证据页码，并查看当前实际生效的清单规则。</p><Tag color="gold">可验证</Tag></div></article>
    </div></section><aside className="docs-aside"><div className="aside-title">当前验证路径</div><ol><li><b>上传文档</b><span>选择 PDF，填写基础信息</span></li><li><b>观察队列</b><span>确认 PENDING → PROCESSING</span></li><li><b>核对结果</b><span>检查字段、置信度与证据</span></li><li><b>记录问题</b><span>把失败样本沉淀到验收清单</span></li></ol><Button block onClick={() => navigate('/tasks')} icon={<ArrowRightOutlined />}>查看解析队列</Button></aside></div>}
    {tab === 'docs' && <div className="doc-library">{docs.map((doc) => <a className="doc-link" href={doc.href} target="_blank" rel="noreferrer" key={doc.name}><FileTextOutlined /><div><strong>{doc.name}</strong><span>{doc.desc}</span></div><ArrowRightOutlined /></a>)}</div>}
    {tab === 'roadmap' && <div className="roadmap"><div className="roadmap-track" />{[['现在', '完成首屏工作台改版', '把上传、解析队列、字段结果、证据详情放进一条可操作链路。', '进行中'], ['下一步', '建立可重复的验证集', '准备标准 PDF、扫描件、混合文档三组样本，记录准确率、耗时和失败原因。', '待推进'], ['Phase 1', '证据定位与人工复核', '补齐原文预览坐标高亮、字段审核、修改/接受/拒绝和导出。', '规划中'], ['Phase 2', '规则与模型优化', '引入多路径融合、字段配置版本化、性能指标和批量处理。', '规划中']].map(([phase, title, desc, status]) => <article className="roadmap-item" key={phase}><div className="roadmap-dot" /><div className="roadmap-phase">{phase}</div><div><h3>{title}<Tag>{status}</Tag></h3><p>{desc}</p></div></article>)}</div>}
  </div>
}
export default PlatformDocs


