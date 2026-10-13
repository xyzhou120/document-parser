# Document Parser Frontend

智能文档解析平台 - 前端应用

## 技术栈

- **React 18** - UI框架
- **TypeScript** - 类型安全
- **Vite** - 构建工具
- **Ant Design 5** - UI组件库
- **React Router 6** - 路由管理
- **Axios** - HTTP客户端
- **Zustand** - 状态管理（可选）

## 功能特性

- 📤 **文档上传** - 支持拖拽上传PDF文档
- 📊 **解析任务列表** - 实时查看解析任务状态
- 📝 **解析结果展示** - 详细展示提取字段和置信度
- ⚙️ **清单配置管理** - 配置不同类型清单的字段规则
- 🎨 **深色主题** - 专业的深色界面设计
- 📱 **响应式布局** - 适配不同屏幕尺寸

## 快速开始

### 1. 安装依赖

推荐使用 pnpm（更快、更节省空间）：

```bash
# 安装 pnpm（如果还没有）
npm install -g pnpm

# 安装依赖
pnpm install
```

或使用 npm：

```bash
npm install
```

### 2. 启动开发服务器

```bash
pnpm dev
# 或
npm run dev
```

访问：http://localhost:3000

### 3. 构建生产版本

```bash
pnpm build
# 或
npm run build
```

构建产物在 `dist` 目录。

### 4. 预览生产构建

```bash
pnpm preview
# 或
npm run preview
```

## 项目结构

```
frontend/
├── src/
│   ├── pages/              # 页面组件
│   │   ├── DocumentUpload.tsx      # 文档上传页
│   │   ├── ParseTaskList.tsx       # 任务列表页
│   │   ├── ParseResult.tsx         # 结果详情页
│   │   └── ChecklistConfig.tsx     # 配置管理页
│   ├── layouts/            # 布局组件
│   │   └── MainLayout.tsx          # 主布局
│   ├── services/           # API服务
│   │   ├── api.ts                  # API基础配置
│   │   └── parse.ts                # 解析相关API
│   ├── types/              # TypeScript类型
│   │   └── api.ts                  # API类型定义
│   ├── styles/             # 样式文件
│   │   └── global.css              # 全局样式
│   ├── App.tsx             # 应用入口
│   └── main.tsx            # React入口
├── public/                 # 静态资源
├── index.html              # HTML模板
├── vite.config.ts          # Vite配置
├── tsconfig.json           # TypeScript配置
├── package.json
├── Dockerfile              # Docker构建
└── nginx.conf              # Nginx配置
```

## 配置说明

### 环境变量

创建 `.env.local` 文件（不提交到git）：

```env
# API基础URL
VITE_API_BASE_URL=http://localhost:8000/api
```

### API代理

开发环境通过Vite代理转发API请求，配置在 `vite.config.ts`：

```typescript
server: {
  port: 3000,
  proxy: {
    '/api': {
      target: 'http://localhost:8000',
      changeOrigin: true,
    },
  },
}
```

## Docker部署

### 构建镜像

```bash
docker build -t document-parser-frontend:latest .
```

### 运行容器

```bash
docker run -d \
  -p 80:80 \
  --name frontend \
  document-parser-frontend:latest
```

### 与后端一起部署

使用根目录的 `docker-compose.yml`：

```bash
cd ..
docker-compose up -d
```

访问：http://localhost

## 开发指南

### 添加新页面

1. 在 `src/pages/` 创建页面组件
2. 在 `src/App.tsx` 添加路由
3. 在 `src/layouts/MainLayout.tsx` 添加菜单项

### 调用API

在 `src/services/` 目录下添加API函数：

```typescript
import api from './api'

export const getMyData = async () => {
  return api.get('/v1/my-endpoint')
}
```

在组件中使用：

```typescript
import { getMyData } from '@/services/parse'

const MyComponent = () => {
  useEffect(() => {
    const fetchData = async () => {
      const data = await getMyData()
      console.log(data)
    }
    fetchData()
  }, [])
}
```

### 样式定制

全局样式在 `src/styles/global.css`。

Ant Design主题配置在 `src/main.tsx`：

```typescript
<ConfigProvider
  theme={{
    token: {
      colorPrimary: '#3B6DFF',  // 主色
      borderRadius: 6,           // 圆角
    },
  }}
>
```

## UI设计说明

### 配色方案

- **主色调**：`#3B6DFF` (蓝色) - 用于主要操作按钮、链接
- **背景层级**：
  - Level 0: `#05070C` (最深，页面底色)
  - Level 1: `#0A0D12` (顶栏)
  - Level 2: `#0F131C` (卡片、侧边栏)
  - Level 3: `#161D2B` (悬停状态)
  - Level 4: `#1E2636` (边框)

- **状态色**：
  - 成功：`#6EE7B7` (绿色)
  - 警告：`#E9A568` (橙色)
  - 错误：`#F87171` (红色)
  - 信息：`#38BDF8` (青色)

### 设计原则

1. **深色主题优先** - 减少眼睛疲劳，适合长时间使用
2. **高对比度** - 文字清晰可读
3. **圆角设计** - 按钮、卡片使用圆角，柔和视觉
4. **状态可视化** - 使用颜色和徽章清晰展示状态
5. **响应式** - 适配不同屏幕尺寸

## 性能优化

- ✅ 使用Vite进行快速HMR
- ✅ 代码分割（React.lazy）
- ✅ 生产构建压缩
- ✅ Nginx Gzip压缩
- ✅ 静态资源缓存

## 浏览器支持

- Chrome >= 90
- Firefox >= 88
- Safari >= 14
- Edge >= 90

## 常见问题

### 1. API请求失败

检查后端服务是否启动：
```bash
curl http://localhost:8000/docs
```

检查代理配置是否正确。

### 2. 样式不生效

清除缓存：
```bash
rm -rf node_modules/.vite
pnpm dev
```

### 3. 构建失败

检查Node.js版本：
```bash
node -v  # 应该是 18+
```

重新安装依赖：
```bash
rm -rf node_modules
pnpm install
```

## 许可证

内部项目
