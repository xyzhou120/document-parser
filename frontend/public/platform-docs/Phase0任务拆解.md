# Phase 0 任务拆解与实施计划

> **项目**: document-parser  
> **Phase**: Phase 0 快速验证（2周）  
> **目标**: 验证技术可行性，准确率>90%，处理速度<30秒/文档  
> **日期**: 2026-10-04 开始

---

## 📋 总览

### Phase 0 目标

```
核心目标：
✅ 验证方案D的技术可行性
✅ 实现1个清单的完整解析（5个字段）
✅ 准确率达到90%以上
✅ 处理速度<30秒/文档
✅ GPU占用<0.1个

交付物：
📄 技术可行性报告
📊 准确率测试数据
⏱️ 性能测试数据
💻 可运行的Demo系统
```

### 时间安排

```
Week 1 (Day 1-5): 基础架构 + 最小Demo
Week 2 (Day 6-10): 多路径验证 + 性能优化
```

---

## Week 1: 基础架构 + 最小Demo

### Day 1: 环境搭建 + 数据库初始化

#### 上午（4小时）：项目初始化

**任务清单**:
```bash
✅ 1. 创建项目目录结构
   - mkdir -p document-parser/{backend,frontend,config,docs}
   - 初始化git仓库

✅ 2. 后端初始化
   cd backend
   poetry init
   poetry add fastapi uvicorn[standard] sqlalchemy asyncpg alembic
   poetry add pydantic pydantic-settings redis celery
   poetry add pymupdf pillow openai httpx minio
   
✅ 3. 创建基础目录结构
   mkdir -p app/{api/{v1},models,services,extractors,parsers,storage,tasks,utils}
   touch app/{__init__.py,main.py,config.py}
   touch app/api/__init__.py app/api/v1/__init__.py

✅ 4. 配置文件
   mkdir -p config
   创建 config/default.yaml（参考技术设计文档）
   创建 .env.example
```

**预期产出**:
- ✅ 项目目录结构完整
- ✅ Poetry环境配置完成
- ✅ 基础配置文件就绪

#### 下午（4小时）：数据库 + Docker

**任务清单**:
```bash
✅ 1. Docker Compose配置
   创建 docker-compose.yml
   - PostgreSQL 15
   - Redis 7
   - MinIO
   
✅ 2. 启动依赖服务
   docker-compose up -d postgres redis minio
   验证服务正常运行
   
✅ 3. 数据库初始化
   创建 migrations/init.sql（4张表）
   - documents
   - parse_runs
   - extracted_fields
   - parse_logs
   
✅ 4. Alembic配置
   alembic init migrations
   配置 alembic.ini
   创建初始迁移
   alembic upgrade head
```

**验证点**:
```bash
# 验证PostgreSQL
psql -h localhost -U postgres -d document_parser -c "\dt"
# 应该看到4张表

# 验证Redis
redis-cli ping
# 应该返回PONG

# 验证MinIO
curl http://localhost:9001
# 应该可以访问控制台
```

**预期产出**:
- ✅ Docker服务运行正常
- ✅ 数据库表创建完成
- ✅ MinIO bucket创建完成

---

### Day 2: D组代码迁移 + 核心模型

#### 上午（4小时）：D组代码迁移

**任务清单**:
```python
✅ 1. 迁移 contracts_v1.py → app/models/contracts.py
   - Contract基类
   - Box, Locator, EvidenceRef
   - ParseRequest, ParseResponse
   - RunStatus枚举
   
✅ 2. 迁移 quality.py → app/services/quality_service.py
   - PageQuality, QualityIssue
   - assess_page_quality()
   
✅ 3. 迁移 inspectors.py → app/utils/page_inspector.py
   - inspect_pdf()
   
✅ 4. 单元测试验证
   pytest tests/test_contracts.py
   pytest tests/test_quality.py
```

**代码示例**:
```python
# app/models/contracts.py
from pydantic import BaseModel, Field
from typing import Literal, Optional
from enum import Enum

class CoordinateSystem(str, Enum):
    PDF_POINT = "PDF_POINT"
    NORMALIZED = "NORMALIZED"

class Box(BaseModel):
    x0: float
    y0: float
    x1: float
    y1: float
    
    def __post_init__(self):
        assert self.x0 < self.x1
        assert self.y0 < self.y1

class Locator(BaseModel):
    coordinate_system: CoordinateSystem
    bbox: Box
    page_number: Optional[int] = None

class EvidenceRef(BaseModel):
    page_number: int
    locator: Locator
    quote: str
    confidence: float = Field(ge=0, le=1)

# ... 更多模型
```

**预期产出**:
- ✅ D组核心模型迁移完成
- ✅ 单元测试通过
- ✅ 类型检查无错误

#### 下午（4小时）：数据库模型 + 配置管理

**任务清单**:
```python
✅ 1. SQLAlchemy模型
   app/models/database.py
   - Document模型
   - ParseRun模型
   - ExtractedField模型
   - ParseLog模型
   
✅ 2. Pydantic Schemas
   app/models/schemas.py
   - DocumentCreate, DocumentResponse
   - ParseRunCreate, ParseRunResponse
   - ExtractedFieldResponse
   
✅ 3. 配置管理实现
   app/config.py
   - Settings类
   - load_from_yaml()
   - 环境变量支持
   
✅ 4. 数据库连接
   app/storage/database.py
   - AsyncEngine
   - AsyncSession
   - get_db() 依赖注入
```

**代码示例**:
```python
# app/models/database.py
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import String, BigInteger, Integer, Float, Text, JSON
from sqlalchemy.dialects.postgresql import UUID
import uuid
from datetime import datetime

class Base(DeclarativeBase):
    pass

class Document(Base):
    __tablename__ = "documents"
    
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4
    )
    file_name: Mapped[str] = mapped_column(String(500))
    original_name: Mapped[str] = mapped_column(String(500))
    file_path: Mapped[str] = mapped_column(Text)
    file_size: Mapped[int] = mapped_column(BigInteger)
    file_hash: Mapped[str] = mapped_column(String(64), index=True)
    mime_type: Mapped[str] = mapped_column(String(100))
    page_count: Mapped[int | None] = mapped_column(Integer)
    parse_status: Mapped[str] = mapped_column(String(50), default="PENDING")
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        default=datetime.utcnow,
        onupdate=datetime.utcnow
    )

# ... 其他模型
```

**预期产出**:
- ✅ 数据库模型完整
- ✅ 配置管理可用
- ✅ 数据库连接测试通过

---

### Day 3: PDF解析 + OCR服务

#### 上午（4小时）：PDF解析器

**任务清单**:
```python
✅ 1. PDF解析器实现
   app/parsers/pdf_parser.py
   - extract_text() 提取文本
   - extract_pages() 逐页提取
   - get_page_images() 页面转图片
   
✅ 2. 页面检测集成
   使用D组的inspect_pdf()
   判断是否需要OCR
   
✅ 3. 单元测试
   准备测试PDF文件
   测试文本提取
   测试页面检测
```

**代码示例**:
```python
# app/parsers/pdf_parser.py
import fitz  # PyMuPDF
from pathlib import Path
from typing import List, Dict
from app.utils.page_inspector import inspect_pdf
from app.models.contracts import PageInspection

class PDFParser:
    """PDF解析器，提取文本和页面图片"""
    
    def __init__(self):
        self.dpi = 150  # 图片DPI
    
    def extract_text(self, pdf_path: str) -> str:
        """提取整个PDF的文本"""
        doc = fitz.open(pdf_path)
        text_parts = []
        for page in doc:
            text_parts.append(page.get_text())
        doc.close()
        return "\n\n".join(text_parts)
    
    def extract_pages(self, pdf_path: str) -> List[Dict]:
        """逐页提取文本和元数据"""
        doc = fitz.open(pdf_path)
        pages = []
        
        for page_num, page in enumerate(doc, start=1):
            text = page.get_text()
            images = page.get_images()
            
            pages.append({
                "page_number": page_num,
                "text": text,
                "char_count": len(text),
                "image_count": len(images),
                "width": page.rect.width,
                "height": page.rect.height
            })
        
        doc.close()
        return pages
    
    def get_page_image(self, pdf_path: str, page_number: int) -> bytes:
        """获取指定页面的图片"""
        doc = fitz.open(pdf_path)
        page = doc[page_number - 1]
        
        # 转换为图片
        pix = page.get_pixmap(dpi=self.dpi)
        img_bytes = pix.tobytes("png")
        
        doc.close()
        return img_bytes
    
    def inspect_pages(self, pdf_path: str) -> List[PageInspection]:
        """检测哪些页面需要OCR"""
        return inspect_pdf(pdf_path)
```

**预期产出**:
- ✅ PDF文本提取正常
- ✅ 页面检测功能正常
- ✅ 图片转换正常

#### 下午（4小时）：OCR服务集成

**任务清单**:
```python
✅ 1. OCR服务客户端
   app/parsers/ocr_service.py
   - PaddleOCRClient类
   - recognize() 调用OCR API
   - normalize_response() 归一化结果
   
✅ 2. 配置OCR服务地址
   config/local.yaml
   预留API key配置
   
✅ 3. OCR结果处理
   转换为统一的TextBlock格式
   坐标归一化
   
✅ 4. 测试OCR调用
   准备扫描件测试
   验证识别准确率
```

**代码示例**:
```python
# app/parsers/ocr_service.py
import httpx
from typing import List, Dict, Optional
from app.models.contracts import TextBlock, Box, Locator
from app.config import settings

class PaddleOCRClient:
    """PaddleOCR服务客户端"""
    
    def __init__(self):
        self.base_url = settings.ocr.base_url
        self.api_key = settings.ocr.api_key
        self.timeout = settings.ocr.timeout
    
    async def recognize(
        self,
        image: bytes,
        page_number: int,
        image_width: int,
        image_height: int
    ) -> List[TextBlock]:
        """
        调用PaddleOCR识别图片
        
        Args:
            image: 图片字节
            page_number: 页码
            image_width: 图片宽度
            image_height: 图片高度
        
        Returns:
            List[TextBlock]: 文本块列表
        """
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            # 准备请求
            files = {"image": image}
            headers = {}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            
            # 调用OCR API（假设PaddleOCR服务提供HTTP接口）
            response = await client.post(
                f"{self.base_url}/ocr",
                files=files,
                headers=headers
            )
            response.raise_for_status()
            
            # 解析响应
            result = response.json()
            
            # 归一化为TextBlock
            return self._normalize_response(
                result,
                page_number,
                image_width,
                image_height
            )
    
    def _normalize_response(
        self,
        ocr_result: Dict,
        page_number: int,
        image_width: int,
        image_height: int
    ) -> List[TextBlock]:
        """归一化OCR响应"""
        blocks = []
        
        # 假设PaddleOCR返回格式：
        # {
        #   "results": [
        #     {
        #       "text": "识别的文字",
        #       "confidence": 0.95,
        #       "bbox": [[x0, y0], [x1, y1], [x2, y2], [x3, y3]]
        #     }
        #   ]
        # }
        
        for item in ocr_result.get("results", []):
            text = item["text"]
            confidence = item["confidence"]
            bbox_points = item["bbox"]
            
            # 提取边界框
            xs = [p[0] for p in bbox_points]
            ys = [p[1] for p in bbox_points]
            
            box = Box(
                x0=min(xs),
                y0=min(ys),
                x1=max(xs),
                y1=max(ys)
            )
            
            locator = Locator(
                coordinate_system="PIXEL",
                bbox=box,
                page_number=page_number
            )
            
            blocks.append(TextBlock(
                text=text,
                locator=locator,
                confidence=confidence
            ))
        
        return blocks
```

**预期产出**:
- ✅ OCR服务调用成功
- ✅ 结果归一化正常
- ✅ 识别准确率可接受

---

### Day 4: 字段提取器实现

#### 上午（4小时）：规则提取器

**任务清单**:
```python
✅ 1. 关键词提取器
   app/extractors/keyword.py
   - KeywordExtractor类
   - extract() 方法
   - 支持正则表达式
   
✅ 2. Phase 0硬编码5个字段
   - mine_name（煤矿名称）
   - residual_gas_threshold（临界值）
   - report_date（报告日期）
   - borehole_count（钻孔数量）
   - drainage_volume（抽采量）
   
✅ 3. 提取规则定义
   config/fields.yaml
   每个字段的关键词、正则等
   
✅ 4. 单元测试
   模拟文档测试提取
```

**代码示例**:
```python
# app/extractors/keyword.py
import re
from typing import Dict, Optional, List
from app.models.contracts import ExtractedField, EvidenceRef, Locator, Box

class KeywordExtractor:
    """关键词规则提取器"""
    
    def __init__(self):
        # Phase 0硬编码规则
        self.rules = {
            "mine_name": {
                "keywords": ["煤矿名称", "矿井名称", "煤矿："],
                "pattern": r"(?:煤矿名称|矿井名称|煤矿)[:：]\s*([^，。\n]{2,30})"
            },
            "residual_gas_threshold": {
                "keywords": ["临界值", "残余瓦斯含量"],
                "pattern": r"残余瓦斯含量临界值[为是]?\s*(\d+\.?\d*)\s*m³/t"
            },
            "report_date": {
                "keywords": ["报告日期", "编制日期"],
                "pattern": r"(?:报告日期|编制日期)[:：]\s*(\d{4}年\d{1,2}月\d{1,2}日)"
            },
            "borehole_count": {
                "keywords": ["钻孔数量", "钻孔"],
                "pattern": r"钻孔\s*(\d+)\s*个"
            },
            "drainage_volume": {
                "keywords": ["抽采量", "抽采总量"],
                "pattern": r"抽采量[为是]?\s*(\d+\.?\d*)\s*m³"
            }
        }
    
    async def extract(
        self,
        document_text: str,
        field_key: str,
        field_config: Dict
    ) -> Optional[ExtractedField]:
        """
        提取单个字段
        
        Args:
            document_text: 文档全文
            field_key: 字段标识
            field_config: 字段配置
        
        Returns:
            ExtractedField | None
        """
        rule = self.rules.get(field_key)
        if not rule:
            return None
        
        # 正则匹配
        pattern = rule["pattern"]
        match = re.search(pattern, document_text)
        
        if not match:
            return None
        
        # 提取值
        value = match.group(1)
        
        # 计算置信度（简单规则：匹配到就是高置信度）
        confidence = 0.95
        
        # 构造证据
        # TODO: 精确定位到页面和坐标
        evidence = EvidenceRef(
            page_number=1,  # 需要精确定位
            locator=Locator(
                coordinate_system="PDF_POINT",
                bbox=Box(x0=0, y0=0, x1=100, y1=20)
            ),
            quote=match.group(0),
            confidence=confidence
        )
        
        return ExtractedField(
            field_key=field_key,
            field_value=value,
            extraction_method="keyword",
            confidence=confidence,
            evidence=evidence
        )
```

**预期产出**:
- ✅ 规则提取器可用
- ✅ 5个字段规则定义完成
- ✅ 基础测试通过

#### 下午（4小时）：LLM提取器

**任务清单**:
```python
✅ 1. LLM客户端封装
   app/services/llm_client.py
   - 兼容OpenAI API格式
   - 支持重试机制
   
✅ 2. LLM提取器
   app/extractors/llm.py
   - LLMExtractor类
   - extract() 方法
   - Prompt工程
   
✅ 3. 配置模型服务
   config/local.yaml
   配置qwen3.7-27b地址和API key
   
✅ 4. 测试LLM调用
   验证模型响应
   测试提取准确率
```

**代码示例**:
```python
# app/extractors/llm.py
from openai import AsyncOpenAI
from typing import Dict, Optional
import json
from app.models.contracts import ExtractedField, EvidenceRef
from app.config import settings

class LLMExtractor:
    """LLM提取器（兜底）"""
    
    def __init__(self):
        self.client = AsyncOpenAI(
            base_url=settings.text_llm.base_url,
            api_key=settings.text_llm.api_key or "dummy"
        )
        self.model = settings.text_llm.model_name
    
    async def extract(
        self,
        document_text: str,
        field_key: str,
        field_config: Dict
    ) -> Optional[ExtractedField]:
        """
        使用LLM提取字段
        
        Args:
            document_text: 文档全文（或相关片段）
            field_key: 字段标识
            field_config: 字段配置
        
        Returns:
            ExtractedField | None
        """
        # 构造Prompt
        prompt = self._build_prompt(document_text, field_config)
        
        try:
            # 调用LLM
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "你是一个专业的文档信息提取助手。"},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.1,
                max_tokens=200
            )
            
            # 解析响应
            content = response.choices[0].message.content
            result = self._parse_response(content, field_key, field_config)
            
            return result
        
        except Exception as e:
            print(f"LLM提取失败: {e}")
            return None
    
    def _build_prompt(self, document_text: str, field_config: Dict) -> str:
        """构造提取Prompt"""
        field_name = field_config.get("field_name", "未知字段")
        field_type = field_config.get("field_type", "STRING")
        
        # 限制文档长度（避免超长）
        if len(document_text) > 4000:
            document_text = document_text[:4000] + "..."
        
        prompt = f"""请从以下文档中提取"{field_name}"字段。

文档内容：
{document_text}

要求：
1. 只返回字段值，不要解释
2. 如果找不到，返回"NOT_FOUND"
3. 如果是数值，只返回数字，不要单位

请提取"{field_name}"："""
        
        return prompt
    
    def _parse_response(
        self,
        response: str,
        field_key: str,
        field_config: Dict
    ) -> Optional[ExtractedField]:
        """解析LLM响应"""
        response = response.strip()
        
        if response == "NOT_FOUND" or not response:
            return None
        
        # 简单处理：直接作为值
        return ExtractedField(
            field_key=field_key,
            field_value=response,
            extraction_method="llm",
            confidence=0.85,  # LLM置信度稍低
            evidence=None  # LLM无法提供精确坐标
        )
```

**预期产出**:
- ✅ LLM服务调用成功
- ✅ 提取器可用
- ✅ 基础测试通过

---

### Day 5: API开发 + 前端框架

#### 上午（4小时）：FastAPI接口

**任务清单**:
```python
✅ 1. 健康检查接口
   app/api/v1/health.py
   GET /api/v1/health
   
✅ 2. 文档上传接口
   app/api/v1/documents.py
   POST /api/v1/documents/upload
   
✅ 3. 解析任务接口
   app/api/v1/parse.py
   POST /api/v1/parse/start
   GET /api/v1/parse/{parse_run_id}/status
   GET /api/v1/parse/{parse_run_id}/result
   
✅ 4. MinIO集成
   app/storage/minio_client.py
   上传文件到MinIO
   
✅ 5. Celery任务
   app/tasks/parse_tasks.py
   @celery_app.task parse_document_task()
```

**代码示例**:
```python
# app/api/v1/documents.py
from fastapi import APIRouter, UploadFile, File, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.schemas import DocumentResponse
from app.storage.database import get_db
from app.storage.minio_client import MinIOClient
from app.models.database import Document
import hashlib
from uuid import uuid4

router = APIRouter()

@router.post("/upload", response_model=DocumentResponse)
async def upload_document(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db)
):
    """上传PDF文档"""
    
    # 读取文件
    content = await file.read()
    
    # 计算哈希
    file_hash = hashlib.sha256(content).hexdigest()
    
    # 检查是否已存在
    # ... (省略去重逻辑)
    
    # 上传到MinIO
    minio_client = MinIOClient()
    file_path = f"documents/{uuid4()}.pdf"
    await minio_client.upload(file_path, content)
    
    # 保存到数据库
    document = Document(
        file_name=file.filename,
        original_name=file.filename,
        file_path=file_path,
        file_size=len(content),
        file_hash=file_hash,
        mime_type="application/pdf"
    )
    db.add(document)
    await db.commit()
    await db.refresh(document)
    
    return document
```

**预期产出**:
- ✅ 所有API接口可用
- ✅ Swagger文档生成
- ✅ Postman测试通过

#### 下午（4小时）：前端框架初始化

**任务清单**:
```bash
✅ 1. 创建Vite + React项目
   cd frontend
   pnpm create vite@latest . --template react-ts
   pnpm install
   
✅ 2. 安装核心依赖
   pnpm add antd @ant-design/icons
   pnpm add axios zustand @tanstack/react-query
   pnpm add react-router-dom dayjs
   
✅ 3. 项目结构初始化
   src/
   ├── api/          # API调用
   ├── components/   # 通用组件
   ├── pages/        # 页面
   ├── stores/       # 状态管理
   ├── types/        # TypeScript类型
   └── utils/        # 工具函数
   
✅ 4. 配置代理
   vite.config.ts
   配置API代理到后端8000端口
   
✅ 5. 创建基础布局
   src/layouts/MainLayout.tsx
   顶部导航 + 侧边栏 + 内容区
```

**代码示例**:
```typescript
// src/api/client.ts
import axios from 'axios';

export const apiClient = axios.create({
  baseURL: '/api/v1',
  timeout: 30000,
});

apiClient.interceptors.response.use(
  (response) => response.data,
  (error) => {
    console.error('API Error:', error);
    return Promise.reject(error);
  }
);

// src/api/documents.ts
import { apiClient } from './client';

export interface Document {
  document_id: string;
  file_name: string;
  file_size: number;
  parse_status: string;
  created_at: string;
}

export const documentsApi = {
  upload: async (file: File): Promise<Document> => {
    const formData = new FormData();
    formData.append('file', file);
    return apiClient.post('/documents/upload', formData);
  },
  
  list: async (page: number = 1, pageSize: number = 20) => {
    return apiClient.get('/documents', {
      params: { page, page_size: pageSize }
    });
  },
};
```

**预期产出**:
- ✅ 前端项目启动成功
- ✅ 基础布局完成
- ✅ API客户端可用

---

## Week 2: 多路径验证 + 性能优化

### Day 6: 前端核心功能开发

#### 全天（8小时）：前端页面开发

**任务清单**:
```typescript
✅ 1. 文档上传页面
   src/pages/Upload/index.tsx
   - 文件拖拽上传
   - 上传进度显示
   - 上传成功后跳转

✅ 2. 文档列表页面
   src/pages/Documents/index.tsx
   - 表格展示
   - 分页
   - 状态筛选
   - 触发解析按钮

✅ 3. 解析结果页面
   src/pages/ParseResult/index.tsx
   - 字段列表展示
   - 置信度显示
   - 证据展示（引用文字）
   - 坐标高亮（Phase 1）

✅ 4. 解析状态轮询
   使用react-query自动轮询
   实时更新进度
```

**UI设计要点**:
```
设计风格：
✅ 去AI味：不用渐变、不用紫色、不用圆角过大
✅ 专业感：使用深色调、直角卡片、表格为主
✅ 功能性：信息密度适中、操作直观

配色方案：
- 主色：#1890ff（蓝色，专业）
- 成功：#52c41a（绿色）
- 警告：#faad14（橙色）
- 错误：#f5222d（红色）
- 背景：#f0f2f5（浅灰）
- 文字：#262626（深灰）

布局：
- 顶部导航：60px高，白色背景
- 侧边栏：200px宽，浅灰背景
- 内容区：白色卡片，24px间距
- 表格：斑马纹，hover高亮
```

**预期产出**:
- ✅ 上传功能完整
- ✅ 列表展示正常
- ✅ 结果查看可用

---

### Day 7: 端到端流程打通

#### 上午（4小时）：解析服务集成

**任务清单**:
```python
✅ 1. 完整解析流程实现
   app/services/parse_service.py
   - 加载文档
   - PDF解析
   - OCR识别（如需）
   - 字段提取
   - 保存结果

✅ 2. Celery任务完善
   app/tasks/parse_tasks.py
   - 状态更新
   - 错误处理
   - 日志记录

✅ 3. 启动Celery Worker
   celery -A app.tasks.celery_app worker -l info
```

**代码示例**:
```python
# app/services/parse_service.py
from typing import List
from uuid import UUID
from app.parsers.pdf_parser import PDFParser
from app.parsers.ocr_service import PaddleOCRClient
from app.extractors.keyword import KeywordExtractor
from app.extractors.llm import LLMExtractor
from app.models.database import ParseRun, ExtractedField
from app.storage.database import get_db
from datetime import datetime

class ParseService:
    """解析服务"""
    
    def __init__(self):
        self.pdf_parser = PDFParser()
        self.ocr_client = PaddleOCRClient()
        self.keyword_extractor = KeywordExtractor()
        self.llm_extractor = LLMExtractor()
    
    async def parse_document(
        self,
        document_id: UUID,
        parse_run_id: UUID
    ):
        """执行文档解析"""
        
        # 更新状态：RUNNING
        async with get_db() as db:
            parse_run = await db.get(ParseRun, parse_run_id)
            parse_run.status = "RUNNING"
            parse_run.started_at = datetime.utcnow()
            await db.commit()
        
        try:
            # 1. 加载文档
            document = await self._load_document(document_id)
            
            # 2. PDF解析
            pages = self.pdf_parser.extract_pages(document.file_path)
            
            # 3. 检测是否需要OCR
            inspections = self.pdf_parser.inspect_pages(document.file_path)
            
            # 4. OCR识别（如需要）
            for inspection in inspections:
                if inspection.needs_ocr:
                    page_image = self.pdf_parser.get_page_image(
                        document.file_path,
                        inspection.page_number
                    )
                    ocr_result = await self.ocr_client.recognize(
                        page_image,
                        inspection.page_number,
                        pages[inspection.page_number - 1]["width"],
                        pages[inspection.page_number - 1]["height"]
                    )
                    # 合并OCR文本到页面
                    pages[inspection.page_number - 1]["ocr_text"] = ocr_result
            
            # 5. 拼接全文
            full_text = "\n\n".join([p["text"] for p in pages])
            
            # 6. 字段提取（5个字段）
            fields_to_extract = [
                "mine_name",
                "residual_gas_threshold",
                "report_date",
                "borehole_count",
                "drainage_volume"
            ]
            
            extracted_fields = []
            for field_key in fields_to_extract:
                # 先用规则提取
                result = await self.keyword_extractor.extract(
                    full_text,
                    field_key,
                    {}
                )
                
                # 如果规则失败或置信度低，LLM兜底
                if not result or result.confidence < 0.8:
                    llm_result = await self.llm_extractor.extract(
                        full_text,
                        field_key,
                        {}
                    )
                    if llm_result:
                        result = llm_result
                
                if result:
                    extracted_fields.append(result)
            
            # 7. 保存结果
            async with get_db() as db:
                for field in extracted_fields:
                    db_field = ExtractedField(
                        parse_run_id=parse_run_id,
                        document_id=document_id,
                        field_key=field.field_key,
                        field_value=field.field_value,
                        extraction_method=field.extraction_method,
                        confidence=field.confidence
                    )
                    db.add(db_field)
                
                # 更新状态：COMPLETED
                parse_run.status = "COMPLETED"
                parse_run.completed_at = datetime.utcnow()
                parse_run.duration_seconds = (
                    parse_run.completed_at - parse_run.started_at
                ).total_seconds()
                parse_run.fields_extracted = len(extracted_fields)
                
                await db.commit()
        
        except Exception as e:
            # 更新状态：FAILED
            async with get_db() as db:
                parse_run = await db.get(ParseRun, parse_run_id)
                parse_run.status = "FAILED"
                parse_run.error_message = str(e)
                await db.commit()
            
            raise
```

**预期产出**:
- ✅ 端到端流程打通
- ✅ 解析任务正常运行
- ✅ 结果正确保存

#### 下午（4小时）：功能测试 + Bug修复

**任务清单**:
```
✅ 1. 准备测试文档（5-10份）
   - 包含扫描件
   - 包含手写签字
   - 不同页数（10-50页）

✅ 2. 完整流程测试
   - 上传文档
   - 触发解析
   - 查看状态
   - 查看结果

✅ 3. 记录问题
   - 准确率
   - 处理速度
   - 错误情况

✅ 4. Bug修复
   - 修复发现的问题
   - 优化性能
```

**测试用例**:
```
测试文档1: 标准PDF（20页，全文本）
预期：准确率95%+，处理时间<10秒

测试文档2: 扫描件（30页）
预期：准确率90%+，处理时间<20秒

测试文档3: 混合（50页，部分扫描）
预期：准确率90%+，处理时间<30秒
```

**预期产出**:
- ✅ 测试用例全部通过
- ✅ 主要Bug修复
- ✅ 准确率记录

---

### Day 8: 多路径融合实现

#### 全天（8小时）：多路径融合

**任务清单**:
```python
✅ 1. 融合算法实现
   app/extractors/fusion.py
   - MultiPathFusion类
   - fuse_candidates() 方法
   - 投票+加权+一致性奖励

✅ 2. 修改提取流程
   同时调用keyword和llm
   收集所有候选结果
   执行融合算法

✅ 3. 对比测试
   单路径 vs 多路径
   准确率对比
   处理时间对比

✅ 4. 参数调优
   融合算法参数
   置信度阈值
```

**代码示例**:
```python
# app/extractors/fusion.py
from typing import List, Dict, Optional
from app.models.contracts import ExtractedField

class MultiPathFusion:
    """多路径融合算法"""
    
    def fuse_candidates(
        self,
        candidates: List[ExtractedField],
        field_config: Dict
    ) -> Optional[ExtractedField]:
        """
        融合多个候选结果
        
        算法：
        1. 值归一化
        2. 投票（相同值分组）
        3. 加权（考虑方法可靠性）
        4. 一致性奖励（多路径一致提升置信度）
        5. 选择最佳
        """
        if not candidates:
            return None
        
        if len(candidates) == 1:
            return candidates[0]
        
        # 值归一化
        normalized = self._normalize_values(candidates, field_config)
        
        # 按值分组
        groups = {}
        for candidate in normalized:
            value = candidate.field_value_normalized
            if value not in groups:
                groups[value] = []
            groups[value].append(candidate)
        
        # 加权评分
        scores = {}
        for value, group in groups.items():
            # 路径权重
            weights = {
                "keyword": 1.0,
                "regex": 1.0,
                "llm": 0.9
            }
            
            # 加权平均置信度
            weighted_conf = sum(
                c.confidence * weights.get(c.extraction_method, 1.0)
                for c in group
            ) / sum(weights.get(c.extraction_method, 1.0) for c in group)
            
            # 一致性奖励
            if len(group) > 1:
                bonus = min(0.15, 0.08 * len(group))
                weighted_conf = min(1.0, weighted_conf + bonus)
            
            scores[value] = {
                "score": weighted_conf,
                "candidates": group
            }
        
        # 选择最佳
        best_value = max(scores.keys(), key=lambda v: scores[v]["score"])
        best_candidates = scores[best_value]["candidates"]
        
        # 返回主候选（置信度最高的）
        primary = max(best_candidates, key=lambda c: c.confidence)
        primary.confidence = scores[best_value]["score"]
        primary.extraction_method = f"multipath({len(best_candidates)})"
        
        return primary
    
    def _normalize_values(
        self,
        candidates: List[ExtractedField],
        field_config: Dict
    ) -> List[ExtractedField]:
        """值归一化"""
        # 简化版：直接返回
        # TODO: 实现数值归一化、单位转换等
        return candidates
```

**预期产出**:
- ✅ 融合算法实现完成
- ✅ 准确率提升2-5%
- ✅ 对比数据记录

---

### Day 9: 性能优化 + 文档完善

#### 上午（4小时）：性能优化

**任务清单**:
```python
✅ 1. 性能分析
   - 记录各阶段耗时
   - 识别瓶颈

✅ 2. 优化点
   - PDF解析并行化（多页）
   - OCR批量调用
   - 数据库批量写入
   - 缓存热点数据

✅ 3. GPU占用测试
   - 监控GPU使用率
   - 计算平均占用
   - 对比不同方案

✅ 4. 性能测试
   - 单文档处理时间
   - 并发处理能力
   - 资源占用
```

**性能指标**:
```
目标：
- 处理时间：<30秒/文档（50页）
- 准确率：>90%
- GPU占用：<0.1个（日处理1000份）
- 内存：<2GB

优化后预期：
- 处理时间：<20秒
- 准确率：>92%
- GPU占用：<0.05个
```

**预期产出**:
- ✅ 性能优化完成
- ✅ 达到目标指标
- ✅ 性能报告

#### 下午（4小时）：文档与代码整理

**任务清单**:
```
✅ 1. API文档完善
   - Swagger注释
   - 请求/响应示例
   - 错误码说明

✅ 2. README编写
   - 项目介绍
   - 快速开始
   - 开发指南
   - 部署说明

✅ 3. 代码注释
   - 关键函数注释
   - 复杂逻辑说明
   - TODO标记

✅ 4. 测试用例
   - 单元测试补充
   - 集成测试
   - 测试覆盖率>70%
```

**预期产出**:
- ✅ 文档完整
- ✅ 代码规范
- ✅ 测试覆盖充分

---

### Day 10: 测试报告 + 演示准备

#### 全天（8小时）：测试报告编写

**任务清单**:
```
✅ 1. 准确率测试
   - 准备10-20份测试文档
   - 人工标注标准答案
   - 系统提取结果
   - 计算准确率

✅ 2. 性能测试
   - 单文档处理时间
   - 并发处理测试
   - GPU占用测试
   - 内存占用测试

✅ 3. 技术可行性报告
   - 目标达成情况
   - 准确率数据
   - 性能数据
   - 问题与风险
   - Phase 1建议

✅ 4. 演示准备
   - 演示流程设计
   - 演示数据准备
   - PPT制作（可选）
   - 录屏演示
```

**技术可行性报告大纲**:
```markdown
# Phase 0 技术可行性报告

## 一、执行概述
- 目标回顾
- 完成情况
- 核心结论

## 二、功能实现
- 已实现功能清单
- 技术架构
- 核心代码统计

## 三、准确率测试
- 测试数据集（10-20份）
- 测试方法
- 准确率结果
  - 整体准确率：XX%
  - 各字段准确率
  - 错误类型分析

## 四、性能测试
- 单文档处理时间
- 并发处理能力
- GPU占用
- 内存占用
- 对比目标

## 五、技术验证
- 方案D核心创新验证
  - 多路径融合效果
  - 规则为主模型为辅
- D组代码复用价值
- 技术栈适配性

## 六、问题与风险
- 发现的问题
- 技术风险
- 性能瓶颈

## 七、Phase 1建议
- 功能优先级
- 技术优化方向
- 资源需求
```

**预期产出**:
- ✅ 完整测试报告
- ✅ 演示就绪
- ✅ Phase 1规划

---

## 📊 验收标准

### Phase 0必须达成

```
✅ 功能完整性
   - 文档上传 ✅
   - PDF解析 ✅
   - OCR识别 ✅
   - 字段提取（5个字段）✅
   - 结果展示 ✅

✅ 准确率
   - 整体准确率 > 90% ✅
   - 关键字段（临界值）> 95% ✅

✅ 性能
   - 处理时间 < 30秒/文档 ✅
   - GPU占用 < 0.1个 ✅

✅ 稳定性
   - 无严重Bug ✅
   - 错误处理完善 ✅

✅ 文档
   - 技术可行性报告 ✅
   - 代码注释完整 ✅
```

### 加分项

```
⭐ 准确率 > 92%
⭐ 处理时间 < 20秒
⭐ 多路径融合效果明显（+3-5%）
⭐ 前端界面美观易用
⭐ 测试覆盖率 > 70%
```

---

## 🎯 关键里程碑

```
Day 1 ✅ 环境搭建完成
Day 2 ✅ 数据模型就绪
Day 3 ✅ PDF+OCR可用
Day 4 ✅ 提取器实现
Day 5 ✅ API+前端框架
Day 6 ✅ 前端功能完成
Day 7 ✅ 端到端打通
Day 8 ✅ 多路径融合
Day 9 ✅ 性能优化
Day 10 ✅ 测试报告
```

---

## 🚨 风险与应对

### 风险1: OCR准确率不够

**应对**:
- 调整图片预处理（增强、去噪）
- 尝试不同OCR参数
- 准备备用OCR服务

### 风险2: LLM响应慢

**应对**:
- 优化Prompt长度
- 并发调用多个字段
- 考虑使用更快的模型

### 风险3: 前端开发延迟

**应对**:
- 简化UI设计
- 使用Ant Design Pro模板
- 先实现核心功能

### 风险4: 准确率不达标

**应对**:
- 分析错误类型
- 优化提取规则
- 增加LLM兜底比例

---

## 📞 每日站会

建议每天早上9:00进行15分钟站会：

```
1. 昨天完成了什么？
2. 今天计划做什么？
3. 遇到什么阻碍？
```

保持团队同步，及时发现和解决问题。

---

**任务拆解完成！准备开始实施！** 🚀