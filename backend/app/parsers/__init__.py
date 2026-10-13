"""解析器包。"""

from app.parsers.ocr_client import PaddleOCRClient
from app.parsers.pdf_parser import PDFParser

__all__ = [
    "PDFParser",
    "PaddleOCRClient",
]
