"""
配置管理模块
从 YAML 文件和环境变量加载配置
"""

import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings


class DatabaseSettings(BaseSettings):
    """数据库配置"""

    host: str = "localhost"
    port: int = 5432
    name: str = "document_parser"
    user: str = "postgres"
    password: str = "postgres"
    pool_size: int = 10
    max_overflow: int = 20

    @property
    def url(self) -> str:
        """获取数据库连接URL"""
        return f"postgresql+asyncpg://{quote(self.user, safe='')}:{quote(self.password, safe='')}@{self.host}:{self.port}/{quote(self.name, safe='')}"


class RedisSettings(BaseSettings):
    """Redis配置"""

    host: str = "localhost"
    port: int = 6379
    db: int = 0
    password: str | None = None
    max_connections: int = 50

    @property
    def url(self) -> str:
        """获取Redis连接URL"""
        if self.password:
            return f"redis://:{quote(self.password, safe='')}@{self.host}:{self.port}/{self.db}"
        return f"redis://{self.host}:{self.port}/{self.db}"


class MinIOSettings(BaseSettings):
    """MinIO配置"""

    endpoint: str = "localhost:9000"
    access_key: str = "minioadmin"
    secret_key: str = "minioadmin"
    bucket: str = "documents"
    secure: bool = False


class ModelSettings(BaseSettings):
    """模型服务配置"""

    base_url: str
    api_key: str | None = None
    model_name: str
    # 远程模型升级时必须更新此部署版本；OCR 服务未提供可供解析器自动校验的模型摘要。
    model_version: str = "unknown"
    timeout: int = 60
    max_retries: int = 3
    # 同一服务的共享请求上限。文本/视觉同部署共池，OCR 使用独立容量池。
    max_concurrency: int = 4
    trust_env: bool = True
    route: str | None = None
    dimensions: int | None = None

    @field_validator("dimensions")
    @classmethod
    def positive_dimensions(cls, value):
        if value is not None and value <= 0:
            raise ValueError("dimensions must be positive")
        return value

    @field_validator("max_concurrency")
    @classmethod
    def positive_concurrency(cls, value):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("max_concurrency must be a positive integer")
        return value

    @field_validator("route")
    @classmethod
    def relative_route(cls, value):
        if value is not None and (not value.strip() or any(token in value for token in ("://", "..", "?", "#"))):
            raise ValueError("route must be a relative endpoint path")
        return value


class CelerySettings(BaseSettings):
    """Celery配置"""

    broker_url: str = "redis://localhost:6379/0"
    result_backend: str = "redis://localhost:6379/0"
    task_serializer: str = "json"
    result_serializer: str = "json"
    accept_content: list = ["json"]
    timezone: str = "Asia/Shanghai"
    enable_utc: bool = True
    task_default_queue: str = "celery"


class AppSettings(BaseSettings):
    """应用配置"""

    name: str = "document-parser"
    version: str = "1.0.0"
    debug: bool = False
    host: str = "0.0.0.0"
    port: int = 8000
    # 本地验证在此显式启用；启用后上传默认使用可靠提取 v2。
    reliable_enabled: bool = False
    assistant_enabled: bool = False


class LoggingSettings(BaseSettings):
    """日志配置"""

    level: str = "INFO"
    format: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    file: str = "logs/app.log"
    max_bytes: int = 10485760  # 10MB
    backup_count: int = 5


class UploadSettings(BaseSettings):
    """文件上传配置"""

    max_file_size: int = 104857600  # 100MB
    allowed_extensions: list = [".pdf"]
    temp_dir: str = "/tmp/document-parser"


class AgentSettings(BaseSettings):
    """限制清单代理的预算；分数不能证明结果正确。"""

    max_rounds: int = 40
    max_rounds_per_batch: int = 6
    max_actions_per_round: int = 4
    max_view_pages: int = 20
    max_tokens: int = 6000
    max_context_chars: int = 24000
    max_total_tokens: int = 200000
    field_batch_size: int = 1
    max_region_views: int = 24
    max_search_results: int = 8
    # 方案 B：整页图像使用 120 DPI（约 1400 个 token），区域裁剪使用 180 DPI。
    render_dpi: int = 120
    region_render_dpi: int = 180
    # 方案 B：每次调用保留的页面图像数（原先固定为 3）。
    history_images: int = 2
    # 方案 B：推理输出开关；默认值在 A/B 结果明确前保持不变。
    # 可靠字段提取使用结构化 JSON 决策，本地默认运行关闭推理文本，避免占用输出预算。
    thinking_enabled: bool = False
    # 方案 B：调用前估算的实测初始值，随后按每次实际用量校正。
    token_per_char: float = 0.55
    image_tokens_per_kpx: float = 1.0
    # 方案 B 缺陷修复：答案长度下限；剩余预算不足时拒绝调用。
    min_output_tokens: int = 2500
    # 方案 C：retrieval 为确定性候选检索与核验，legacy 为工具循环。
    # A/B 验收门槛（文档 10.4 节）通过并确认前，默认仍为 legacy。
    agent_mode: str = "legacy"
    # 新的可靠运行按页共享全部字段。整篇不再用墙钟或 token 预算中止。
    reliable_strategy: str = "page_batch_v1"
    reliable_max_seconds: int | None = None
    reliable_token_limit: int | None = None
    # 提取按整页发送。只有单页正文超过这个长度才预切；模型回答过长时再拆这一页。
    page_batch_chars: int = 100000
    # 可靠按页提取的上下文预算，对应模型约 26 万窗口中的 13 万。
    reliable_context_tokens: int = 131072
    # 单页原图估算上限。整页图进入同一次提取，不再被旧的 4000 上限拆开。
    reliable_max_image_tokens: int = 16000
    # 一份文档同时发出的页请求数。每次请求仍然只包含一页。
    reliable_page_parallelism: int = 4
    # 单次唤醒的公平性预算；只在量子之间检查，不取消正在完成的页调用。
    reliable_wakeup_seconds: int = Field(default=120, ge=1, le=1800)
    reliable_wakeup_quantums: int = Field(default=20, ge=1, le=1000)
    field_token_budget: int = 16000
    max_calls_per_field: int = 3
    candidates_per_call: int = 12
    max_candidates_per_field: int = 24
    max_candidates_multi: int = 36
    max_images_per_call: int = 12
    max_image_tokens_per_call: int = 4000
    candidate_window_chars: int = 300
    retrieval_image_policy: str = "auto"


class AttachmentImportSettings(BaseSettings):
    """开发期 Excel 辅助入口；来源策略可选，不配置即可按模板链接下载。"""
    enabled: bool = True
    max_upload_bytes: int = 20 * 1024 * 1024
    max_expanded_bytes: int = 200 * 1024 * 1024
    max_rows: int = 100000
    max_download_bytes: int = 100 * 1024 * 1024
    download_timeout: int = 120
    download_concurrency: int = 3
    parse_concurrency: int = 3
    max_attempts: int = 3
    lease_seconds: int = 180
    # 开发期默认不限制来源；如另行需要，才配置 origin/CIDR 与请求头。
    # {"https://files.example:443": {"networks": ["10.0.0.0/24"], "headers": {...}}}
    sources: dict[str, dict] = {}

    @field_validator("sources", mode="before")
    @classmethod
    def json_sources(cls, value):
        if isinstance(value, str):
            import json
            return json.loads(value)
        return value

    @field_validator("max_upload_bytes", "max_expanded_bytes", "max_rows", "max_download_bytes",
                     "download_timeout", "download_concurrency", "parse_concurrency", "max_attempts", "lease_seconds")
    @classmethod
    def positive_limits(cls, value):
        if value <= 0:
            raise ValueError("attachment import limits must be positive")
        return value


class Settings:
    """全局配置类"""

    def __init__(self):
        self.app: AppSettings
        self.database: DatabaseSettings
        self.redis: RedisSettings
        self.minio: MinIOSettings
        self.celery: CelerySettings
        self.logging: LoggingSettings
        self.upload: UploadSettings
        self.agent: AgentSettings
        self.models: dict[str, ModelSettings] = {}
        self.fields: dict[str, Any] = {}

        # 加载配置
        self._load_config()

    def _load_config(self):
        """加载配置文件"""
        # 获取配置目录
        config_dir = Path(__file__).parent.parent.parent / "config"

        # 加载默认配置
        config_data = self._load_yaml(config_dir / "default.yaml")

        # 根据环境加载配置
        env = os.getenv("ENV", "development")
        env_config_path = config_dir / f"{env}.yaml"
        if env_config_path.exists():
            env_config = self._load_yaml(env_config_path)
            config_data = self._deep_merge(config_data, env_config)

        # 加载本地配置（优先级最高）
        local_config_path = config_dir / "local.yaml"
        if local_config_path.exists():
            local_config = self._load_yaml(local_config_path)
            config_data = self._deep_merge(config_data, local_config)

        # 进程环境变量优先；YAML 默认值不能修改进程环境。
        environment = {
            k: str(v) for k, v in (config_data.get("environment") or {}).items() if v is not None
        }
        environment.update(os.environ)

        def env_value(key):
            value = environment.get(key)
            return value if value not in (None, "") else None

        def section(name, model):
            values = dict(config_data.get(name, {}))
            for field in model.model_fields:
                key = f"{name}_{field}".upper()
                if env_value(key) is not None:
                    values[field] = env_value(key)
            return model(**values)

        # 解析配置
        self.app = section("app", AppSettings)
        if env_value("RELIABLE_ENABLED") is not None:
            self.app.reliable_enabled = str(env_value("RELIABLE_ENABLED")).lower() in {"1", "true", "yes", "on"}
        if env_value("ASSISTANT_ENABLED") is not None:
            self.app.assistant_enabled = str(env_value("ASSISTANT_ENABLED")).lower() in {"1", "true", "yes", "on"}
        self.database = section("database", DatabaseSettings)
        self.redis = section("redis", RedisSettings)
        self.minio = section("minio", MinIOSettings)
        self.celery = section("celery", CelerySettings)
        self.logging = section("logging", LoggingSettings)
        self.upload = section("upload", UploadSettings)
        self.agent = section("agent", AgentSettings)
        self.attachment_import = section("attachment_import", AttachmentImportSettings)
        if any(env_value(f"REDIS_{key}") is not None for key in ("HOST", "PORT", "DB", "PASSWORD")):
            if env_value("CELERY_BROKER_URL") is None:
                self.celery.broker_url = self.redis.url
            if env_value("CELERY_RESULT_BACKEND") is None:
                self.celery.result_backend = self.redis.url

        # 加载模型配置：补全缺失的密钥与地址
        models_config = config_data.get("models", {})
        for key, value in models_config.items():
            settings_value = dict(value)
            for field in ModelSettings.model_fields:
                env_key = f"{key}_{field}".upper()
                if env_value(env_key) is not None:
                    settings_value[field] = env_value(env_key)
            if key in ("text_llm", "vision_llm"):
                settings_value["api_key"] = settings_value.get("api_key") or env_value(
                    "OPENAI_API_KEY"
                )
                settings_value["base_url"] = (
                    env_value(f"{key}_BASE_URL".upper())
                    or env_value("OPENAI_BASE_URL")
                    or env_value("OPENAI_API_BASE")
                    or settings_value.get("base_url", "")
                )
            if not settings_value.get("model_name"):
                settings_value["model_name"] = environment.get("OPENAI_MODEL", "gpt-6.1-sol")
            self.models[key] = ModelSettings(**settings_value)

        # 加载字段配置
        self.fields = config_data.get("fields", {})

    def _load_yaml(self, path: Path) -> dict[str, Any]:
        """加载YAML文件"""
        if not path.exists():
            return {}
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    def _deep_merge(self, base: dict, override: dict) -> dict:
        """深度合并字典"""
        result = base.copy()
        for key, value in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = self._deep_merge(result[key], value)
            else:
                result[key] = value
        return result


# 全局配置实例
settings = Settings()
