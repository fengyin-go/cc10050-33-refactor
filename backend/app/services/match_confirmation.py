"""规格匹配确认的共用写入、锁与回执转换。

人工复核入口和规格匹配器的直接应用入口此前各自维护一份确认写入。这里把
“按同一个匹配结果加锁 -> 幂等写入确认 -> 保留首张回执”的部分收拢起来；
两个入口仍保留各自历史上的入参、返回字段和错误文案。
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Iterator

from app.store import store

_LOCKS_GUARD = RLock()
_LOCKS: dict[str, RLock] = {}

CONFIRMATION_KEY = "confirmation"
CONFIRMATION_RECEIPT_KEY = "confirmation_receipt"
CONFIRMATION_ROW_KEY = "确认回执"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def find_match(match_id: Any) -> dict[str, Any] | None:
    """按主键查找匹配记录；字符串数字与整数 ID 视为同一标识。"""
    if match_id is None:
        return None
    target = str(match_id)
    return next(
        (row for row in store.rows("sku_match") if str(row.get("id", "")) == target),
        None,
    )


def find_match_by_reference(reference: Any) -> dict[str, Any] | None:
    """按复核记录中的“匹配记录”引用查找匹配行。"""
    if reference is None:
        return None
    ref = str(reference)
    if not ref:
        return None
    return find_match(ref)


def _lock_key(reference: Any = None, match_id: Any = None) -> str:
    if match_id is not None:
        match = find_match(match_id)
        if match is not None:
            return f"sku_match:{match.get('id')}"
        return f"sku_match-ref:{match_id}"

    match = find_match_by_reference(reference)
    if match is not None:
        return f"sku_match:{match.get('id')}"
    return f"match-reference:{reference}"


def _lock_for(key: str) -> RLock:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = RLock()
            _LOCKS[key] = lock
        return lock


@contextmanager
def confirmation_lock(reference: Any = None, match_id: Any = None) -> Iterator[None]:
    """串行化同一匹配结果的两个确认入口。"""
    key = _lock_key(reference=reference, match_id=match_id)
    lock = _lock_for(key)
    with lock:
        yield


def _public_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    """复制回执，避免调用方通过返回值改动行内历史。"""
    return {k: v for k, v in receipt.items() if k != "history"}


def write_match_confirmation(
    match: dict[str, Any] | None = None,
    *,
    match_id: Any = None,
    approved: bool = True,
    source: str = "shared",
    reviewer: str | None = None,
    reviewed_by: str | None = None,
    review_id: Any = None,
    review_code: str | None = None,
    review_conclusion: Any = None,
    conclusion: Any = None,
    confirmed_at: str | None = None,
) -> dict[str, Any]:
    """写入两个入口共用的匹配确认结果。

    同一真实结果重复到达时保留首次确认时间、审核人和结论，只补齐后到入口
    的关联信息；相反结果则递增版本并保留 history，避免无声覆盖。
    """
    if match is None:
        match = find_match(match_id)
    if match is None:
        return {
            "found": False,
            "match": None,
            "match_id": match_id,
            "duplicate": False,
            "receipt": None,
        }

    with confirmation_lock(match_id=match.get("id")):
        previous_raw = match.get(CONFIRMATION_KEY)
        previous = previous_raw if isinstance(previous_raw, dict) else None
        previous_approved = previous.get("approved") if previous else None
        duplicate = bool(previous and previous_approved == approved)

        history: list[dict[str, Any]] = []
        if previous:
            old_history = previous.get("history")
            if isinstance(old_history, list):
                history.extend(item for item in old_history if isinstance(item, dict))
            if previous_approved != approved:
                history.append(_public_receipt(previous))

        if duplicate and previous is not None:
            receipt = dict(previous)
            receipt.pop("history", None)
            receipt["duplicate"] = True
            revision = int(receipt.get("revision", 1) or 1)

            # 直接应用入口通常没有审核人；复核入口后到时补齐审核信息，但不
            # 覆盖已经留下的原始审核人/时间。
            if not receipt.get("reviewer"):
                receipt["reviewer"] = reviewer if reviewer is not None else reviewed_by
            if review_id is not None and not receipt.get("review_id"):
                receipt["review_id"] = review_id
            if review_code and not receipt.get("review_code"):
                receipt["review_code"] = review_code
            incoming_conclusion = (
                review_conclusion if review_conclusion is not None else conclusion
            )
            if incoming_conclusion not in (None, "") and not receipt.get("review_conclusion"):
                receipt["review_conclusion"] = incoming_conclusion

            sources = receipt.setdefault("sources", [])
            source_token = f"{source}:{review_id if review_id is not None else match.get('id')}"
            if source_token not in sources:
                sources.append(source_token)
        else:
            revision = int(match.get("confirmation_revision", 0) or 0) + 1
            timestamp = confirmed_at or utc_now_iso()
            incoming_conclusion = (
                review_conclusion if review_conclusion is not None else conclusion
            )
            receipt = {
                "receipt_id": f"MC-{match.get('id')}-{revision}",
                "match_id": match.get("id"),
                "review_id": review_id,
                "review_code": review_code,
                "approved": approved,
                "decision": "approved" if approved else "rejected",
                "result_status": "已匹配" if approved else "匹配失败",
                "source": source,
                "sources": [
                    f"{source}:{review_id if review_id is not None else match.get('id')}"
                ],
                "reviewer": reviewer if reviewer is not None else reviewed_by,
                "review_conclusion": incoming_conclusion,
                "confirmed_at": timestamp,
                "revision": revision,
                "duplicate": False,
            }

        if history:
            receipt["history"] = history

        # 先留存确认回执和追查字段，再切换真实状态；若进程在两步之间中断，
        # 恢复读取仍能看到有依据的处理中原因，原请求也可幂等继续。
        match["confirmation_revision"] = revision
        match["receipt_id"] = receipt.get("receipt_id")
        match["confirmation_id"] = receipt.get("receipt_id")
        match[CONFIRMATION_KEY] = receipt
        match[CONFIRMATION_RECEIPT_KEY] = receipt
        match[CONFIRMATION_ROW_KEY] = receipt

        projection = {
            "复核角色": receipt.get("reviewer"),
            "审核人": receipt.get("reviewer"),
            "reviewer": receipt.get("reviewer"),
            "复核结论": receipt.get("review_conclusion"),
            "review_conclusion": receipt.get("review_conclusion"),
            "复核时间": receipt.get("confirmed_at"),
            "confirmed_at": receipt.get("confirmed_at"),
            "review_id": receipt.get("review_id"),
            "复核记录编号": receipt.get("review_code"),
            "review_code": receipt.get("review_code"),
        }
        for field, value in projection.items():
            if value not in (None, ""):
                match[field] = value
            else:
                match.pop(field, None)

        # 真实状态只来自本次被确认的结果。
        if approved:
            match["status"] = "已匹配"
            match["pending"] = False
        else:
            match["status"] = "匹配失败"
            match["abnormal"] = True

        return {
            "found": True,
            "match": match,
            "match_id": match.get("id"),
            "duplicate": duplicate,
            "revision": revision,
            "receipt": _public_receipt(receipt),
        }


def review_approval_receipt(write_result: dict[str, Any]) -> dict[str, Any]:
    """转换为人工复核入口历史上的回执。"""
    return {
        "ok": True,
        "message": "复核已通过",
        "match_backfilled": bool(write_result.get("found")),
    }


def matcher_approval_receipt(_write_result: dict[str, Any]) -> dict[str, Any]:
    """转换为规格匹配器直接应用入口历史上的回执。"""
    return {"ok": True, "message": "复核通过，匹配已确认"}
