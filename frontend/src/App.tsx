import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { ConfigProvider } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import MainLayout from './layouts/MainLayout'
import Dashboard from './pages/Dashboard'
import DocumentUpload from './pages/DocumentUpload'
import Documents from './pages/Documents'
import BatchQuery from './pages/BatchQuery'
import ParseTaskList from './pages/ParseTaskList'
import ParseResult from './pages/ParseResult'
import RuleManagement from './pages/RuleManagement'
import ServiceManagement from './pages/ServiceManagement'
import Analytics from './pages/Analytics'
import BatchManagement from './pages/BatchManagement'
import DocumentWorkspace from './pages/DocumentWorkspace'
import RuleReleases from './pages/RuleReleases'
import SourceArtifacts from './pages/SourceArtifacts'
import ChecklistManagement from './pages/ChecklistManagement'
import ReviewWorkspace from './pages/ReviewWorkspace'
import ContentPreparation from './pages/ContentPreparation'

function App() {
  return (
    <ConfigProvider
      locale={zhCN}
      theme={{ token: { colorPrimary: '#087bff', borderRadius: 5, fontFamily: '"Microsoft YaHei","PingFang SC",sans-serif' } }}
    >
      <BrowserRouter>
        <Routes>
          <Route path="/" element={<MainLayout />}>
            <Route index element={<Navigate to="/dashboard" replace />} />
            <Route path="dashboard" element={<Dashboard />} />
            <Route path="upload" element={<DocumentUpload />} />
            <Route path="documents" element={<Documents />} />
            <Route path="query" element={<BatchQuery />} />
            <Route path="tasks" element={<ParseTaskList />} />
            <Route path="result/:parseRunId" element={<ParseResult />} />
            <Route path="documents/:documentId/content-build/:buildId" element={<ContentPreparation />} />
            <Route path="rules" element={<RuleManagement />} />
            <Route path="services" element={<ServiceManagement />} />
            <Route path="analytics" element={<Analytics />} />
            <Route path="batches" element={<BatchManagement />} />
            <Route path="rule-releases" element={<RuleReleases />} />
            <Route path="document-workspace" element={<DocumentWorkspace />} />
            <Route path="source-artifacts" element={<SourceArtifacts />} />
            <Route path="checklists" element={<ChecklistManagement />} />
            <Route path="review-workspace" element={<ReviewWorkspace />} />
            <Route path="*" element={<Navigate to="/dashboard" replace />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </ConfigProvider>
  )
}

export default App
