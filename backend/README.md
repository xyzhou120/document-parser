# Document Parser Backend

智能文档解析平台 - 后端服务

## 技术栈

- **Python 3.12**
- **FastAPI** - 异步Web框架
- **SQLAlchemy 2.0** - ORM
- **PostgreSQL 15** - 数据库
- **Redis** - 缓存 & Celery Broker
- **Celery** - 异步任务队列
- **MinIO** - 对象存储
- **Poetry** - 依赖管理

## 项目结构

```
backend/
├── app/
│   ├── api/              # API路由
│   ├── models/           # 数据模型
│   │   ├── contracts.py  # Pydantic数据契约（从D组迁移）
│   │   └── database.py   # SQLAlchemy数据库模型
│   ├── schemas/          # API Schema
│   ├── services/         # 业务逻辑
│   ├── parsers/          # 解析器（PDF、OCR）
│   ├── extractors/       # 提取器（关键词、LLM）
│   ├── storage/          # 存储层（数据库、MinIO）
│   ├── config.py         # 配置管理
│   ├── celery_app.py     # Celery配置
│   ├── tasks.py          # Celery任务
│   └── main.py           # FastAPI应用入口
├── config/               # 配置文件
│   ├── default.yaml      # 默认配置
│   └── local.yaml        # 本地配置（不提交git）
├── migrations/           # 数据库迁移
├── tests/                # 测试
├── docker-compose.dev.yml   # 开发环境
├── docker-compose.prod.yml  # 生产环境
├── Dockerfile
├── pyproject.toml
└── README.md
```

## 快速开始

### 1. 安装依赖

```bash
# 安装Poetry（如果还没有）
pip install poetry

# 安装依赖
poetry install
```

### 2. 配置服务

复制配置示例：
```bash
cp .env.example .env
```

编辑 `.env` 文件，填入实际的服务地址和密钥。

或者编辑 `config/local.yaml`（此文件不会提交到git）。

### 3. 启动依赖服务（开发环境）

```bash
# 启动PostgreSQL、Redis、MinIO
docker-compose -f docker-compose.dev.yml up -d postgres redis minio
```

### 4. 初始化数据库

```bash
# 创建数据库表
poetry run python -c "from app.storage.database import init_db; import asyncio; asyncio.run(init_db())"

# 或者运行迁移脚本
poetry run alembic upgrade head
```

### 5. 启动后端服务

```bash
# 启动FastAPI服务
poetry run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

### 6. 启动Celery Worker

在另一个终端：
```bash
poetry run celery -A app.celery_app worker --loglevel=info --concurrency=2
```

### 7. 访问API文档

打开浏览器：
- Swagger UI: http://localhost:8000/docs
- ReDoc: http://localhost:8000/redoc

## 配置说明

### 配置文件优先级

1. 环境变量（最高优先级）
2. `config/local.yaml`（本地覆盖，不提交git）
3. `config/default.yaml`（默认配置）

### 模型服务配置

在 `config/local.yaml` 或环境变量中配置：

```yaml
models:
  ocr:
    base_url: "http://your-ocr-server:port"
    api_key: "your-api-key"  # 如果不需要，留空
    timeout: 30

  text_llm:
    base_url: "http://your-llm-server:port/v1"
    api_key: "your-api-key"
    model_name: "qwen3.7-27b"
    timeout: 60

  vision_llm:
    base_url: "http://your-vision-llm-server:port/v1"
    api_key: "your-api-key"
    model_name: "qwen3.6-27b"
    timeout: 60
```

## Docker部署

### 开发环境（包含所有依赖服务）

```bash
docker-compose -f docker-compose.dev.yml up -d
```

访问：
- 后端API: http://localhost:8000
- MinIO控制台: http://localhost:9001 (minioadmin/minioadmin123)

### 生产环境（连接外部服务）

1. 配置环境变量：
```bash
cp .env.example .env
# 编辑 .env 文件
```

2. 启动服务：
```bash
docker-compose -f docker-compose.prod.yml up -d
```

3. 查看日志：
```bash
docker-compose -f docker-compose.prod.yml logs -f
```

## API接口

### 1. 上传并解析文档

```bash
POST /api/v1/parse/upload-and-parse

# 表单数据：
- file: PDF文件
- mine_id: 煤矿ID
- mine_name: 煤矿名称
- checklist_type: 清单类型
- reporting_period: 上报周期（YYYY-MM）

# 响应：
{
  "parse_run_id": "uuid",
  "document_id": "uuid",
  "attachment_id": "uuid",
  "status": "PENDING",
  "message": "解析任务已提交"
}
```

### 2. 查询解析状态

```bash
GET /api/v1/parse/runs/{parse_run_id}

# 响应：
{
  "parse_run_id": "uuid",
  "status": "COMPLETED",
  "fields_extracted": 5,
  "mean_confidence": 0.92,
  "fields": [
    {
      "field_key": "mine_name",
      "field_name": "煤矿名称",
      "field_value": "XX煤矿",
      "confidence": 0.95,
      "extraction_method": "keyword"
    }
  ]
}
```

### 3. 查询解析结果

```bash
GET /api/v1/parse/results?mine_id=xxx&checklist_type=xxx

# 响应：
{
  "total": 10,
  "items": [...]
}
```

## 开发指南

### 添加新字段

1. 在 `config/default.yaml` 的 `fields` 部分添加字段配置：

```yaml
fields:
  default:
    - field_key: "new_field"
      field_name: "新字段名称"
      field_type: "STRING"
      pattern: "字段名称[:：]\\s*(?P<value>[^\\n]+)"
```

2. 重启服务，字段会自动提取

### 添加新的提取方法

在 `app/extractors/` 目录下创建新的提取器类。

### 数据库迁移

使用Alembic管理数据库迁移：

```bash
# 创建迁移
poetry run alembic revision --autogenerate -m "description"

# 应用迁移
poetry run alembic upgrade head

# 回滚迁移
poetry run alembic downgrade -1
```

## 测试

```bash
# 运行所有测试
poetry run pytest

# 运行特定测试
poetry run pytest tests/test_parse.py

# 生成覆盖率报告
poetry run pytest --cov=app --cov-report=html
```

## 故障排查

### 1. 数据库连接失败

检查 `config/local.yaml` 中的数据库配置，确保PostgreSQL服务已启动。

### 2. MinIO上传失败

检查MinIO配置，确保bucket已创建。可以通过MinIO控制台手动创建。

### 3. Celery任务不执行

检查Redis连接，确保Celery Worker已启动。

### 4. OCR识别失败

检查OCR服务配置，确认服务地址和API Key正确。

## 性能优化

### Phase 0阶段

- 单文档处理时间：目标 < 10秒
- 并发处理能力：2个Celery Worker
- 日处理量：目标 > 1000份文档

### 优化建议

1. 增加Celery Worker数量
2. 使用Redis缓存重复解析
3. 批量处理相似文档
4. 优化数据库查询索引

## 许可证

内部项目
