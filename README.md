# 智能文档解析平台

2026-10-09操作入口：[平台操作手册](docs/平台操作手册-2026-10-09.md)、[建设交付与验证状态](docs/平台建设交付与验证状态-2026-10-09.md)。以下较早功能说明保留参考，新增批次、规则发布、统一事实及源文订正以这两份文档为准。

煤矿上报 PDF 的异步解析与字段提取平台（本地验证版）。当前 API、Celery Worker、React/Vite 前端在本机启动；PostgreSQL、Redis、MinIO 和 OCR/模型服务运行于内网。认证、权限和 CORS 按当前阶段要求暂缓调整。

## 当前能力

单个 PDF 上传、MinIO 对象保存、Celery 异步任务、PDF 文本提取、扫描页 OCR 调用、规则/表格/LLM 提取、清单/规则配置与版本日志、结果查询、JSON 结果导出、服务连通性探测和统计页面。

多路径融合和真实字段准确率需要按错误任务和人工标注集验收。旧方案中 95–98% 准确率、2.5秒/文档、日处理量10,000份和GPU占用数字均未获得当前代码与实测支持，不应作为交付结果。

## 本地启动

前置：Python 3.12、Poetry 2.x、Node.js 20、npm，以及内网依赖的访问权限。

1. 从项目根目录复制 config/local.yaml.example 到 config/local.yaml，填写现场服务地址。已有文件不要直接覆盖。
2. 在 backend 目录执行 poetry install，然后启动 API：

```powershell
poetry run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

3. 在新终端的 backend 目录启动 Worker（Windows 使用 solo）：

```powershell
poetry run celery -A app.celery_app worker --loglevel=info --pool=solo
```

4. 在新终端的 frontend 目录启动前端：

```powershell
npm ci
npm run dev
```

访问 http://localhost:3000；API 文档 http://localhost:8000/docs，健康检查 http://localhost:8000/health。Vite 会将 /api 代理到本机8000端口。详细配置、首启和故障排查见 [快速启动指南.md](快速启动指南.md)。

## 数据库初始化与升级

新库可执行 backend/migrations/init.sql。内网依赖 Compose 首次创建 PostgreSQL 数据卷时执行同一份 canonical schema。脚本只创建缺少的 ORM 表/索引并做有限补列，不删除或重命名旧表、不自动迁移旧数据。

若使用过 document、parse_run、extracted_field、checklist_config 等单数表，先备份，由开发编写单次事务化 INSERT ... SELECT 映射，在副本验证后升级。详情见 [修复说明.md](修复说明.md)。应用启动会执行 init_db() 和缺失规则种子；已有规则不会被普通种子流程覆盖。

## Docker 交付

backend/docker-compose.dev.yml 供需要本机依赖服务的开发环境使用，不是当前本地前后端+内网依赖的默认启动路径。backend/docker-compose.prod.yml 仅启动 API/Worker，连接外部依赖。两者从项目根目录构建后端，并把 ../config 只读挂载至 /app/config；前端镜像使用 package-lock.json 和 npm ci。

在 backend 目录执行：

```powershell
docker compose -f docker-compose.dev.yml up --build
```

此 Compose 不包含前端。前端容器的 Nginx 代理默认指向同网络的 backend:8000，可在实际部署时调整；当前本地开发前端走 Vite 代理。

## 后续验收

- 多路径候选独立保留，按类型/单位规范化并校验；冲突记录证据、转人工复核。
- 人工接受/修改/拒绝落库、精确 bbox 高亮、批量上传和完整筛选导出。
- 正式 Alembic 迁移、监控告警、备份恢复、依赖故障演练和并发压测。
- 用真实文本/扫描/混合 PDF 标注集计算准确率、召回率和错误接受率，不能把模型置信度当作准确率。

## 检查命令

在 frontend 目录执行 npx tsc --noEmit、npm run lint、npm run build；在 backend 目录执行 poetry run pytest。实际检查结果见 [修复说明.md](修复说明.md)。Docker 镜像构建、服务器数据库升级和真实字段准确率必须单独在现场验收。
