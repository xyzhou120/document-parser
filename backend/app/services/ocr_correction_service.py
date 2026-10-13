"""OCR image-verification candidates.  Publication is intentionally P11-owned."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


@dataclass(frozen=True)
class OCRCorrectionCandidate:
    candidate_id: str
    page_number: int
    region_id: str
    raw_ocr: str
    corrected_transcription: str | None
    bbox: list[float] | None
    coordinate_system: str | None
    image_ref: dict[str, Any] | None
    context: dict[str, Any]
    status: str = "proposed"
    reason: str = "image_verification_required"
    revision_id: str | None = None
    verification_receipt: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"candidate_id": self.candidate_id, "page_number": self.page_number, "region_id": self.region_id,
                "raw_ocr": self.raw_ocr, "corrected_transcription": self.corrected_transcription,
                "bbox": self.bbox, "coordinate_system": self.coordinate_system, "image_ref": self.image_ref,
                "context": self.context, "status": self.status, "reason": self.reason,
                "revision_id": self.revision_id, "verification_receipt": deepcopy(self.verification_receipt),
                "requires_publication": self.status == "verified" and self.corrected_transcription != self.raw_ocr}


class OCRCorrectionService:
    def propose(
        self, *, page_number: int, region_id: str, raw_ocr: str, corrected_transcription: str | None,
        bbox: list[float] | None = None, coordinate_system: str | None = None,
        image_ref: Mapping[str, Any] | None = None, context: Mapping[str, Any] | None = None,
        image_verification: str = "unverified", reason: str | None = None,
        revision_id: str | None = None, candidate_id: str | None = None,
        verification_receipt: Mapping[str, Any] | None = None,
    ) -> OCRCorrectionCandidate:
        raw = str(raw_ocr or "")
        corrected = str(corrected_transcription) if corrected_transcription is not None else None
        receipt = deepcopy(dict(verification_receipt or {}))
        verification = receipt.get("verification") or {}
        valid_bbox = bool(bbox and len(bbox) == 4 and all(isinstance(v, int | float) and math.isfinite(v) for v in bbox)
                          and bbox[2] > bbox[0] and bbox[3] > bbox[1])
        attrs = verification.get("attributes") or {}
        value_support = attrs.get("value") or {}
        verified = bool(
            revision_id and candidate_id and image_ref and image_ref.get("image_hash") and valid_bbox
            and coordinate_system in {"PDF_POINT", "PIXEL", "NORMALIZED"}
            and receipt.get("source") == "original_image"
            and receipt.get("revision_id") == revision_id
            and receipt.get("region_id") == region_id
            and (not candidate_id or receipt.get("candidate_id") == candidate_id)
            and (receipt.get("image_ref") or {}).get("image_hash") == image_ref.get("image_hash")
            and verification.get("status") == "verified"
            and verification.get("transcription") == corrected
            and value_support.get("evidence_ids") and value_support.get("basis")
        )
        # No visual proof means no guessed correction.  An unreadable image is a
        # durable review blocker, never a not-found or silently corrected value.
        if not verified:
            corrected = None
            status = "unreadable" if image_verification.lower() in {"unreadable", "failed"} else "proposed"
            reason = reason or ("image_unreadable" if status == "unreadable" else "image_verification_required")
        else:
            status = "verified" if corrected and corrected != raw else "unchanged"
            reason = reason or ("visual_transcription_differs" if status == "verified" else "visual_confirmation")
        identity = {"revision_id": revision_id, "page": page_number, "region": region_id, "raw": raw, "bbox": bbox, "corrected": corrected, "image_ref": image_ref}
        return OCRCorrectionCandidate(_digest(identity)[:32], page_number, region_id, raw, corrected, deepcopy(bbox), coordinate_system,
                                      deepcopy(dict(image_ref)) if image_ref else None, deepcopy(dict(context or {})), status, reason, revision_id, receipt or None)

    def merge_candidates(self, candidates: list[OCRCorrectionCandidate]) -> list[OCRCorrectionCandidate]:
        """Merge same-stage candidates without inventing a winner on conflict."""
        merged: dict[str, OCRCorrectionCandidate] = {}
        for candidate in candidates:
            key = f"{candidate.revision_id}:{(candidate.image_ref or {}).get('image_hash')}:{candidate.page_number}:{candidate.region_id}:{candidate.raw_ocr}:{candidate.bbox}"
            current = merged.get(key)
            if current is None:
                merged[key] = candidate
                continue
            if current.status == "verified" and candidate.status in {"proposed", "unreadable"}:
                continue
            if current.status in {"proposed", "unreadable"} and candidate.status == "verified":
                merged[key] = candidate
            elif current.corrected_transcription != candidate.corrected_transcription:
                merged[key] = OCRCorrectionCandidate(current.candidate_id, current.page_number, current.region_id, current.raw_ocr, None,
                                                      current.bbox, current.coordinate_system, current.image_ref,
                                                      {**current.context, "alternatives": [current.corrected_transcription, candidate.corrected_transcription]},
                                                      "conflict", "verification_conflict", current.revision_id)
            elif current.status != "verified" and candidate.status == "verified":
                merged[key] = candidate
        return list(merged.values())

    def record_stage(self, checkpoint: Mapping[str, Any], candidates: list[OCRCorrectionCandidate], *, max_no_progress: int = 3) -> dict[str, Any]:
        updated = deepcopy(dict(checkpoint))
        merged = self.merge_candidates(candidates)
        fingerprint = _digest([c.as_dict() for c in merged])
        no_progress = int(updated.get("no_progress_count", 0)) + 1 if updated.get("correction_fingerprint") == fingerprint else 0
        updated.update(correction_fingerprint=fingerprint, correction_candidates=[c.as_dict() for c in merged], no_progress_count=no_progress,
                       correction_state="failed_no_progress" if no_progress >= max_no_progress else "awaiting_publication" if any(c.status == "verified" for c in merged) else "review_required")
        return updated


ocr_correction_service = OCRCorrectionService()

