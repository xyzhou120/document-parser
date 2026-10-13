"""API 路由包。"""

from app.api.artifacts import router as artifacts_router
from app.api.batches import router as batches_router
from app.api.documents import router as documents_router
from app.api.integrations import router as integrations_router
from app.api.observability import router as observability_router
from app.api.parse import router as parse_router
from app.api.releases import router as releases_router
from app.api.reliable_execution import router as reliable_execution_router
from app.api.assistant import router as assistant_router
from app.api.review_workspace import router as review_workspace_router
from app.api.reviews import router as reviews_router
from app.api.rules import router as rules_router
from app.api.services import router as services_router
from app.api.stats import router as stats_router

__all__ = [
    "parse_router",
    "artifacts_router",
    "batches_router",
    "documents_router",
    "integrations_router",
    "observability_router",
    "releases_router",
    "rules_router",
    "services_router",
    "stats_router",
    "reviews_router",
    "review_workspace_router",
    "reliable_execution_router",
    "assistant_router",
]
