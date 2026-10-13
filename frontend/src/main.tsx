import React from 'react'
import ReactDOM from 'react-dom/client'
import { ConfigProvider } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import dayjs from 'dayjs'
import 'dayjs/locale/zh-cn'
import App from './App'
import './styles/global.css'
import './styles/pages.css'
import './styles/execution-assistant.css'
dayjs.locale('zh-cn')
ReactDOM.createRoot(document.getElementById('root')!).render(<React.StrictMode><ConfigProvider locale={zhCN} theme={{token:{colorPrimary:'#087bff',borderRadius:5,fontFamily:'"Microsoft YaHei", "PingFang SC", sans-serif',colorText:'#142540',colorBgLayout:'#f1f5fa'},components:{Layout:{headerBg:'#f9fbfe',bodyBg:'#f1f5fa',siderBg:'#0c213f'},Button:{controlHeight:36},Table:{headerBg:'#f5f8fc'}}}}><App/></ConfigProvider></React.StrictMode>)
