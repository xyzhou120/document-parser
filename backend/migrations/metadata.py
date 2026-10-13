"""Import every mapped model without importing configured engines or credentials."""

import importlib
import pkgutil

import app.models
from app.models.database import Base


def load_metadata():
    for module in pkgutil.iter_modules(app.models.__path__):
        importlib.import_module(f"app.models.{module.name}")
    return Base.metadata
