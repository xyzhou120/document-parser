"""导入所有映射模型，但不导入已配置的引擎或凭据。"""

import importlib
import pkgutil

import app.models
from app.models.database import Base


def load_metadata():
    for module in pkgutil.iter_modules(app.models.__path__):
        importlib.import_module(f"app.models.{module.name}")
    return Base.metadata
