# 内网依赖服务部署包

这套配置只部署 `PostgreSQL 15`、`Redis 7` 和 `MinIO`，供本地运行的后端 API/Celery Worker 使用。前端只需要连接本地后端。

## 1. 服务器部署

把 `deploy/dependencies` 目录复制到内网服务器，例如 `/opt/document-parser-dependencies`：

```bash
cd /opt/document-parser-dependencies
cp .env.example .env
vi .env
docker compose config
docker compose up -d
docker compose ps
```

要求服务器安装 Docker Engine 和 Docker Compose v2。必须修改 `.env` 中的密码；当前后端数据库 URL 未编码密码，建议此次使用较长的字母、数字、连字符、下划线组合，避免 `@:/?#` 等 URL 特殊字符。`.env` 中的值可用单引号包住，避免 `$` 被 Compose 插值。

`DATA_ROOT` 可以改成服务器数据盘目录，例如 `/data/document-parser`。首次部署需要创建数据目录的权限。服务器防火墙只开放给本地开发机或后端访问所需的端口：

- PostgreSQL：`${POSTGRES_PORT}`，默认 `5432`
- Redis：`${REDIS_PORT}`，默认 `6379`
- MinIO API：`${MINIO_API_PORT}`，默认 `9000`
- MinIO 控制台：`${MINIO_CONSOLE_PORT}`，默认 `9001`，建议只允许管理员访问

## 2. 初始化行为

- PostgreSQL 数据库由 `POSTGRES_DB` 自动创建，默认是 `document_parser`。
- PostgreSQL 表由 `postgres/init/001_schema.sql` 在空数据目录第一次启动时自动创建。
- Redis 使用 AOF 持久化，不需要建表或初始化 SQL。
- MinIO 不再依赖 `minio/mc` 初始化镜像；请在 MinIO 控制台手动创建私有 bucket `documents`。
- 后端启动时还会调用 `Base.metadata.create_all`，可以补建缺少的 ORM 表，但不会迁移既有表结构。

MinIO 控制台地址是 `http://服务器IP:9001`。登录后进入 **Buckets → Create Bucket**，创建名称为 `documents` 的 bucket，并保持访问策略为 **Private**。当前代码会把上传文件写入 `documents/uploads/...`。

PostgreSQL 检查数据库和表：

```bash
docker compose exec postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT current_database();"'
docker compose exec postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "\\dt"'
```

应能看到 `documents`、`parse_runs`、`extracted_fields`、`parse_logs` 四表。UUID、状态、计数、时间以及 `updated_at` 的更新行为保持当前 ORM 的 Python 端赋值方式，没有额外添加默认值、外键或触发器。手工 INSERT 时需要自行提供这些必填字段。当前业务清单规则来自根目录 `config/default.yaml`，无额外清单种子数据需要导入。

已有数据库需要补建缺失表时，可手动运行同一脚本：

```bash
docker compose exec -T postgres sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < postgres/init/001_schema.sql
```

这是补建操作，不是数据库结构迁移。修改 `.env` 的数据库名、账号、密码，或更新初始化 SQL，不会重新初始化已有数据目录；数据库凭据需要在数据库中显式变更，不能通过清空数据目录来保留已有数据。

## 3. 不要执行旧脚本

不要把 `backend/migrations/init.sql` 挂载到这套 PostgreSQL 中。它对应旧版本模型，使用 `document`、`parse_run`、`extracted_field` 等单数表；当前代码使用 `documents`、`parse_runs`、`extracted_fields`、`parse_logs`。本目录的 `001_schema.sql` 已按当前 ORM 对齐。

这套脚本面向全新数据库。`CREATE TABLE IF NOT EXISTS` 只能补建缺失对象，不能把旧表自动升级成当前结构。

## 4. 本地后端配置

复制或参考仓库中的 `config/dependencies.local.yaml.example`（部署 ZIP 内也附带 `local-backend.yaml.example`），把数据库、Redis、MinIO 和 Celery 地址合并到仓库根目录的 `config/local.yaml`，保留已有模型配置。例如服务器地址为 `192.168.1.100`：

```yaml
database:
  host: "192.168.1.100"
  port: 5432
  name: "document_parser"
  user: "postgres"
  password: "服务器 PostgreSQL 密码"

redis:
  host: "192.168.1.100"
  port: 6379
  db: 0
  password: "服务器 Redis 密码"

minio:
  endpoint: "192.168.1.100:9000"
  access_key: "服务器 MinIO 用户"
  secret_key: "服务器 MinIO 密码"
  bucket: "documents"
  secure: false

celery:
  broker_url: "redis://:服务器 Redis 密码@192.168.1.100:6379/0"
  result_backend: "redis://:服务器 Redis 密码@192.168.1.100:6379/1"
```

如果 Redis 密码包含 `@`、`:`、`/`、`#` 等 URL 特殊字符，需要先 URL 编码，否则 Celery URL 会解析失败。

`redis.host` 的变更不会自动修改 `celery.broker_url` 和 `celery.result_backend`，这三个配置必须同步。MinIO `endpoint` 不要加 `http://`。服务器 `.env` 只供 Docker Compose 使用，当前后端应使用根目录 YAML 配置；其配置加载器没有正确映射 `DATABASE_HOST` 等前缀环境变量。

此外，配置 OCR/文本模型的真实地址；`models.ocr.model_name` 在当前配置类中也是必填，可在 `config/local.yaml` 添加：

```yaml
models:
  ocr:
    model_name: "paddleocr"
```

API 和 Worker 都从本地读取相同 YAML。在三个依赖服务就绪后，本地启动命令为：

```powershell
# 终端一，仓库根目录
cd backend
poetry install
poetry run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

# 终端二，backend 目录，Windows 使用 solo worker
poetry run celery -A app.celery_app worker --loglevel=info --pool=solo

# 终端三，frontend 目录
npm ci
npm run dev
```

前端访问 `http://localhost:3000`，通过 Vite 把 `/api` 代理到本地 `http://localhost:8000`。API 文档 `http://localhost:8000/docs`，健康检查 `http://localhost:8000/health`。

部署本套依赖不会修复应用的已有启动问题：当前 API 导入缺失的 `AttachmentMetadata` 和 `app.schemas.parse`，上传写入与 ORM 不一致，上传页仍为演示交互，前端生产构建存在 TypeScript 错误。因此“依赖部署成功”不等于“端到端解析可用”，业务服务启动前仍需修复这些问题。

## 5. 连通性检查

在本地开发机执行：

```powershell
Test-NetConnection 192.168.1.100 -Port 5432
Test-NetConnection 192.168.1.100 -Port 6379
Test-NetConnection 192.168.1.100 -Port 9000
```

服务器上执行：

```bash
docker compose exec postgres sh -c 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
docker compose exec redis redis-cli ping
curl http://127.0.0.1:9000/minio/health/live
```

## 6. 备份和停止

停止容器不会删除数据：

```bash
docker compose stop
```

删除容器但保留数据目录：

```bash
docker compose down
```

本套配置使用宿主机数据目录，`docker compose down` 不会删除这些目录。不要删除 `${DATA_ROOT}`，否则会丢失 PostgreSQL、Redis 和 MinIO 数据。

PostgreSQL 备份示例：

```bash
docker compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' > backup-$(date +%F).sql
```

## 7. 内网无镜像仓库访问时

如果服务器能访问你们的内网镜像仓库，可以在 `.env` 中直接替换 `POSTGRES_IMAGE`、`REDIS_IMAGE`、`MINIO_IMAGE`，然后执行 `docker compose pull`。如果没有可用的镜像仓库，在一台能访问公共镜像仓库且 CPU 架构与目标服务器相同的机器上预先拉取并打包：

```bash
docker pull --platform linux/amd64 postgres:15-alpine
docker pull --platform linux/amd64 redis:7-alpine
docker pull --platform linux/amd64 minio/minio:latest
docker save -o document-parser-images-amd64.tar postgres:15-alpine redis:7-alpine minio/minio:latest
sha256sum document-parser-images-amd64.tar
```

把 tar 和本目录一起复制到服务器后执行：

```bash
docker load -i document-parser-images-amd64.tar
docker image ls | grep -E 'postgres|redis|minio'
docker compose up -d --pull never
```

如果目标服务器不是 amd64，把示例中的 `linux/amd64` 改成服务器架构，例如 `linux/arm64`。建议保存 tar 的 SHA256 和镜像 digest，避免后续重新拉取 `latest` 导致版本变化。

## 10. 诊断镜像仓库访问

在服务器上分别执行，确认是否所有镜像都失败：

```bash
docker pull postgres:15-alpine
docker pull redis:7-alpine
docker pull minio/minio:latest
docker info | sed -n '/Registry Mirrors/,+8p'
docker context show
```

如果三个 `docker pull` 都返回 401/`pull access denied`，问题在 Docker daemon 的 registry 访问、镜像加速器或内网出口策略，和 Compose 配置无关。不要通过 `docker login` 把个人凭据写入服务器，优先使用内网镜像仓库或离线导入。

## 8. 当前需要创建的资源

只需要创建一个 MinIO bucket：

| 服务 | 资源 | 用途 |
| --- | --- | --- |
| MinIO | `documents` | 存放上传的 PDF，建议保持 Private |

PostgreSQL 由 Compose 自动创建数据库和四张表；Redis 不需要创建数据库、表或 bucket。

## 9. 本次验证范围

已校验 Compose 解析、模板 YAML、初始化 SQL 与当前 ORM 的表/列/可空性/索引一致性。编写环境的 Docker daemon 未启动，未实际启动容器；请在目标服务器按上面的数据库、Redis 和 MinIO 检查命令验收。
