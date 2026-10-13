"""
质量评估服务（从D组PoC迁移）
评估页面和字段提取的质量
"""

import logging
from typing import List

from app.models.contracts import PageQuality, QualityIssue

logger = logging.getLogger(__name__)


def assess_page_quality(
    page_number: int,
    ocr_blocks: List[dict],
    confidence_threshold: float = 0.8,
) -> PageQuality:
    """
    评估页面质量

    Args:
        page_number: 页码
        ocr_blocks: OCR识别的文本块（包含confidence）
        confidence_threshold: 置信度阈值

    Returns:
        PageQuality
    """
    if not ocr_blocks:
        return PageQuality(
            page_number=page_number,
            status="QUALITY_OK",
            mean_confidence=1.0,
            p10_confidence=1.0,
            low_confidence_ratio=0.0,
            issues=[],
        )

    # 提取置信度
    confidences = [block.get("confidence", 1.0) for block in ocr_blocks]
    confidences.sort()

    # 计算指标
    mean_confidence = sum(confidences) / len(confidences)
    p10_confidence = confidences[int(len(confidences) * 0.1)] if confidences else 0.0
    low_confidence_count = sum(1 for c in confidences if c < confidence_threshold)
    low_confidence_ratio = low_confidence_count / len(confidences)

    # 判断状态
    issues = []
    if mean_confidence < 0.7:
        status = "REVIEW_REQUIRED"
        issues.append(
            QualityIssue(
                code="LOW_MEAN_CONFIDENCE",
                severity="ERROR",
                message=f"平均置信度过低: {mean_confidence:.2f}",
                page_number=page_number,
            )
        )
    elif mean_confidence < 0.85:
        status = "DEGRADED"
        issues.append(
            QualityIssue(
                code="MODERATE_CONFIDENCE",
                severity="WARNING",
                message=f"置信度一般: {mean_confidence:.2f}",
                page_number=page_number,
            )
        )
    else:
        status = "QUALITY_OK"

    if low_confidence_ratio > 0.3:
        issues.append(
            QualityIssue(
                code="HIGH_LOW_CONFIDENCE_RATIO",
                severity="WARNING",
                message=f"低置信度文本占比过高: {low_confidence_ratio:.2%}",
                page_number=page_number,
            )
        )

    logger.info(
        f"Page {page_number} quality: {status}, "
        f"mean_conf={mean_confidence:.2f}, "
        f"low_ratio={low_confidence_ratio:.2%}"
    )

    return PageQuality(
        page_number=page_number,
        status=status,
        mean_confidence=mean_confidence,
        p10_confidence=p10_confidence,
        low_confidence_ratio=low_confidence_ratio,
        issues=issues,
    )
