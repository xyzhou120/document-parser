import asyncio

from app.services.ocr_capacity import MemoryOCRCapacity
from app.services.vision_transcription import interpret_vision_text


def test_capacity_waits_until_a_slot_is_released():
    gate = MemoryOCRCapacity()

    async def run():
        first = await gate.try_acquire(1, lease_seconds=30, now=10)
        second = await gate.try_acquire(1, lease_seconds=30, now=11)
        await gate.release(first)
        third = await gate.try_acquire(1, lease_seconds=30, now=12)
        expired = await gate.try_acquire(1, lease_seconds=30, now=12)
        later = await gate.try_acquire(1, lease_seconds=30, now=50)
        return first, second, third, expired, later

    first, second, third, expired, later = asyncio.run(run())
    assert first and second is None and third and expired is None
    assert later  # 上一个名额在第 42 秒到期，第 50 秒可以再领。


def test_expired_lease_frees_the_slot_without_an_explicit_release():
    gate = MemoryOCRCapacity()

    async def run():
        await gate.try_acquire(1, lease_seconds=5, now=0)
        blocked = await gate.try_acquire(1, lease_seconds=5, now=4.9)
        freed = await gate.try_acquire(1, lease_seconds=5, now=5)
        return blocked, freed

    blocked, freed = asyncio.run(run())
    assert blocked is None and freed


def test_blank_vision_answer_is_verified_empty_and_text_is_kept():
    assert interpret_vision_text("  BLANK ")["state"] == "blank"
    assert interpret_vision_text("")["blank_verified"] is True
    assert interpret_vision_text("  瓦斯含量 1.02\n压力 0.19  ")["text"].startswith("瓦斯含量")
