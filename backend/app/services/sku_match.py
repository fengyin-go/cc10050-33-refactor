"""规格匹配器领域服务：把站点SKU匹配到商品档案的归一规格，负责匹配置信度判定与人工复核分流。

数据链路位置：规格对齐层，消费规格归一器推送的候选规格与商品档案中心的主数据，
低置信度结果转人工复核队列，高置信度结果回填商品档案。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.services.match_confirmation import (
    confirmation_lock,
    find_match,
    matcher_approval_receipt,
    write_match_confirmation,
)
from app.store import store


class SkuMatchService(BaseModuleService):
    MODULE = "sku_match"
    ENTITY = "匹配记录"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["匹配批次", "商品档案", "候选规格", "匹配置信度", "匹配状态"]
    REQUIRED_FIELDS = ["匹配批次", "商品档案", "候选规格"]
    STATUS_ORDER = ["待匹配", "已匹配", "人工复核", "匹配失败"]
    ACTION_RULES = {"执行匹配": "已匹配", "转人工复核": "人工复核"}
    NEGATIVE_ACTIONS = []

    # ---- 业务扩展：规格匹配与置信度分流 ----

    def run_match(self, entry_id: int, confidence: float) -> dict[str, Any]:
        """执行规格匹配，按置信度分流到已匹配或人工复核。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": f"{self.ENTITY} {entry_id} 不存在"}
        goods_name = entry.get("商品档案", "")
        goods = next(
            (g for g in store.rows("goods_registry") if g.get("商品标识") == goods_name), None
        )
        if goods is None:
            entry["status"] = "匹配失败"
            entry["abnormal"] = True
            return {"ok": False, "message": f"商品档案 {goods_name} 不存在"}
        if confidence >= 0.95:
            entry["status"] = "已匹配"
            entry["pending"] = False
        else:
            entry["status"] = "人工复核"
            entry["pending"] = True
        entry["匹配置信度"] = f"{confidence:.2f}"
        return {
            "ok": True,
            "message": "匹配执行完成",
            "result_status": entry.get("status"),
            "confidence": entry.get("匹配置信度"),
        }

    def escalate_review(self, entry_id: int) -> dict[str, Any]:
        """把低置信度匹配记录升级到人工复核队列。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") != "人工复核":
            return {"ok": False, "message": "记录不存在或不处于人工复核状态"}
        rows = store.rows("match_review")
        rid = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        rows.append({
            "id": rid,
            "记录编号": f"SM-{entry_id}-{rid}",
            "匹配记录": str(entry.get("id", "")),
            "复核结论": f"置信度 {entry.get('匹配置信度', '')}",
            "复核角色": "规格审核员",
            "复核时间": datetime.now(timezone.utc).isoformat(),
            "status": "待复核",
            "pending": True,
            "abnormal": False,
        })
        return {"ok": True, "message": "已升级人工复核", "review_id": rid}

    def apply_review_result(self, entry_id: int, approved: bool) -> dict[str, Any]:
        """应用复核结论：通过则回填档案，驳回则进失败隔离。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "记录不存在"}

        with confirmation_lock(match_id=entry_id):
            if approved:
                write_result = write_match_confirmation(
                    entry,
                    approved=True,
                    source="sku_match",
                )
                return matcher_approval_receipt(write_result)

            write_match_confirmation(
                entry,
                approved=False,
                source="sku_match",
            )
            rows = store.rows("failure_quarantine")
            fid = max((int(r.get("id", 0)) for r in rows), default=0) + 1
            rows.append({
                "id": fid,
                "样本指纹": f"match-{entry_id}",
                "失败阶段": "匹配",
                "失败原因": "人工复核驳回",
                "隔离时间": datetime.now(timezone.utc).isoformat(),
                "重试次数": "0",
                "status": "待处理",
                "pending": True,
                "abnormal": False,
            })
            return {"ok": True, "message": "复核驳回，样本已隔离", "quarantine_id": fid}

    def consume_normalized(self, entry_id: int) -> dict[str, Any]:
        """消费规格归一器推送的候选规格完成对齐。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "记录不存在"}
        spec = entry.get("候选规格", "")
        matched_rule = next(
            (r for r in store.rows("spec_normalizer")
             if r.get("status") == "已启用" and r.get("归一规格") == spec), None
        )
        if matched_rule is None:
            entry["status"] = "匹配失败"
            entry["abnormal"] = True
            return {"ok": False, "message": f"候选规格 {spec} 没有启用中的归一规则"}
        entry["匹配状态"] = "归一对齐"
        return {
            "ok": True,
            "message": "候选规格已对齐归一规则",
            "rule": matched_rule.get("规则名称", ""),
        }

    def _find_related_review(self, entry: dict[str, Any]) -> dict[str, Any] | None:
        """找到与当前匹配记录关联的复核记录，优先使用确认回执中的 ID。"""
        receipt = entry.get("confirmation")
        receipt = receipt if isinstance(receipt, dict) else {}
        review_id = receipt.get("review_id")
        if review_id is not None:
            review = next(
                (r for r in store.rows("match_review")
                 if str(r.get("id", "")) == str(review_id)),
                None,
            )
            if review is not None:
                return review

        match_id = str(entry.get("id", ""))
        goods = str(entry.get("商品档案", "") or "")
        candidates = [
            r for r in store.rows("match_review")
            if str(r.get("匹配记录", "")) == match_id
            or (goods and str(r.get("匹配记录", "")) == goods)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda r: int(r.get("id", 0) or 0))

    def _find_baseline_target(self, entry_id: Any) -> dict[str, Any] | None:
        """按匹配 ID 查找；基线检查也允许直接传商品标识（如 A86）。"""
        entry = find_match(entry_id)
        if entry is not None:
            return entry
        target = str(entry_id or "")
        if not target:
            return None
        return next(
            (m for m in store.rows(self.MODULE)
             if m.get("商品档案") == target or m.get("商品标识") == target),
            None,
        )

    def link_goods_baseline(self, entry_id: int) -> dict[str, Any]:
        """检查匹配成功后商品是否具备基线价格，并给出资格/处理中依据。"""
        entry = self._find_baseline_target(entry_id)
        if entry is None:
            return {"ok": False, "message": "记录不存在或未匹配"}

        with confirmation_lock(match_id=entry.get("id")):
            # 锁内重新取行，避免等待期间另一入口已经改变状态。
            entry = find_match(entry.get("id"))
            if entry is None:
                return {"ok": False, "message": "记录不存在或未匹配"}

            goods_name = entry.get("商品档案", "")
            current_status = entry.get("status")
            receipt_raw = entry.get("confirmation")
            receipt = receipt_raw if isinstance(receipt_raw, dict) else {}
            review = self._find_related_review(entry)

            review_id = receipt.get("review_id")
            if review_id is None and review is not None:
                review_id = review.get("id")
            review_code = receipt.get("review_code") or (
                review.get("记录编号") if review is not None else None
            )
            review_status = review.get("status") if review is not None else None

            reviewer = receipt.get("reviewer")
            if reviewer in (None, "") and review is not None:
                reviewer = review.get("复核角色")
            conclusion = receipt.get("review_conclusion")
            if conclusion in (None, "") and review is not None:
                conclusion = review.get("复核结论")
            confirmed_at = receipt.get("confirmed_at")
            if confirmed_at in (None, "") and review is not None:
                confirmed_at = review.get("复核时间")

            review_pending = review_status == "待复核"
            seal_pending = review_pending
            evidence = {
                "match_id": entry.get("id"),
                "match_status": current_status,
                "goods": goods_name,
                "review_id": review_id,
                "review_code": review_code,
                "review_status": review_status,
                "receipt_id": receipt.get("receipt_id"),
                "confirmation_source": receipt.get("source"),
                "confirmation_revision": receipt.get("revision"),
                "reviewer": reviewer,
                "review_conclusion": conclusion,
                "confirmed_at": confirmed_at,
            }

            if current_status != "已匹配":
                if current_status in ("待匹配", "人工复核"):
                    qualification = "processing"
                    eligible = False
                    processing = True
                    if receipt.get("approved") is True:
                        reason_code = "confirmation_status_pending"
                        reason = (
                            f"匹配记录 {entry.get('id')} 已留存通过回执 "
                            f"{receipt.get('receipt_id', '')}，但匹配状态切换未完成；"
                            "原确认请求继续后即可生效，当前处理中"
                        )
                    else:
                        reason_code = (
                            "match_in_manual_review"
                            if current_status == "人工复核"
                            else "match_pending"
                        )
                        reason = (
                            f"匹配记录 {entry.get('id')} 当前为{current_status}，"
                            "尚未形成已确认的有效匹配结果，当前处理中"
                        )
                else:
                    qualification = "ineligible"
                    eligible = False
                    processing = False
                    reason_code = "match_failed_or_unavailable"
                    reason = (
                        f"匹配记录 {entry.get('id')} 当前为{current_status}，"
                        "不具备基线关联资格"
                    )
                eligibility = {
                    "eligible": False,
                    "match_eligible": False,
                    "status": qualification,
                    "qualification": qualification,
                    "reason_code": reason_code,
                    "reason": reason,
                    "processing": processing,
                    "processing_reason": reason if processing else None,
                    "evidence": evidence,
                }
                return {
                    "ok": False,
                    "message": "记录不存在或未匹配",
                    "goods": goods_name,
                    "match_status": current_status,
                    "has_baseline": False,
                    "has_effective": False,
                    "effective": False,
                    "match_eligible": eligible,
                    "eligible": eligible,
                    "can_associate": False,
                    "match_qualification": qualification,
                    "baseline_usable": False,
                    "baseline_available": False,
                    "baseline_ready": False,
                    "can_use_baseline": False,
                    "processing": processing,
                    "pending": processing,
                    "review_processing": review_pending,
                    "review_seal_pending": review_pending,
                    "qualification": qualification,
                    "eligibility": qualification,
                    "eligibility_status": qualification,
                    "reason_code": reason_code,
                    "eligibility_detail": eligibility,
                    "reason": reason,
                    "eligible_reason": reason,
                    "eligibility_reason": reason,
                    "match_reason": reason,
                    "pending_reason": reason if processing else None,
                    "blocked_reason": reason if processing else None,
                    "processing_reason": reason if processing else None,
                    "review_id": review_id,
                    "review_code": review_code,
                    "receipt_id": receipt.get("receipt_id"),
                    "confirmation_id": receipt.get("receipt_id"),
                    "review_status": review_status,
                    "reviewer": reviewer,
                    "审核人": reviewer,
                    "review_conclusion": conclusion,
                    "confirmed_at": confirmed_at,
                    "confirmation": receipt or None,
                    "evidence": evidence,
                    "basis": evidence,
                }

            baselines = [
                b for b in store.rows("price_baseline")
                if b.get("商品标识") == goods_name and b.get("status") == "已生效"
            ]
            has_baseline = bool(baselines)
            baseline_ids = [b.get("id") for b in baselines]
            effective_baselines = [
                {
                    "id": b.get("id"),
                    "goods": b.get("商品标识"),
                    "spec": b.get("归一规格"),
                    "normalized_spec": b.get("归一规格"),
                    "price": b.get("基线价格"),
                    "baseline_price": b.get("基线价格"),
                    "status": b.get("status"),
                    "updated_at": b.get("更新时间"),
                }
                for b in baselines
            ]
            baseline_price = baselines[0].get("基线价格") if baselines else None
            baseline_usable = has_baseline
            if seal_pending and receipt.get("approved") is True:
                reason_code = "review_seal_pending"
                processing_reason = (
                    "匹配确认已写入，但人工复核记录尚未完成封存；"
                    "可依据匹配确认回执检查基线，原通过请求可幂等继续"
                )
                reason = (
                    f"匹配记录 {entry.get('id')} 的确认结果已写入"
                    f"（回执 {receipt.get('receipt_id', '已有匹配状态')}），"
                    f"商品 {goods_name} 有 {len(baselines)} 条生效基线；"
                    "复核记录仍在封存处理中"
                )
            elif seal_pending:
                reason_code = "review_pending"
                processing_reason = (
                    "匹配状态已是已匹配，但关联人工复核记录仍为待复核；"
                    "基线资格以真实匹配状态为准，复核封存可继续追查"
                )
                reason = (
                    f"匹配记录 {entry.get('id')} 当前为已匹配，"
                    f"商品 {goods_name} 有 {len(baselines)} 条生效基线；"
                    "关联复核记录仍在处理中"
                )
            elif has_baseline:
                reason_code = "baseline_ready"
                processing_reason = None
                reason = (
                    f"匹配记录 {entry.get('id')} 已确认，商品 {goods_name} "
                    f"存在 {len(baselines)} 条生效基线，可使用基线关联"
                )
            else:
                reason_code = "baseline_missing"
                processing_reason = None
                reason = (
                    f"匹配记录 {entry.get('id')} 已确认，但商品 {goods_name} "
                    "没有已生效基线，暂不能使用基线"
                )

            evidence["baseline_ids"] = baseline_ids
            evidence["effective_baseline_count"] = len(baselines)
            evidence["effective_baselines"] = effective_baselines
            evidence["reason_code"] = reason_code
            eligibility = {
                "eligible": True,
                "match_eligible": True,
                "status": "eligible_review_seal_pending" if seal_pending else "eligible",
                "qualification": "eligible",
                "reason_code": reason_code,
                "reason": reason,
                "processing": seal_pending,
                "processing_reason": processing_reason,
                "evidence": evidence,
            }
            baseline_view = {
                "usable": baseline_usable,
                "available": has_baseline,
                "has_baseline": has_baseline,
                "has_effective": has_baseline,
                "effective": has_baseline,
                "count": len(baselines),
                "effective_count": len(baselines),
                "baseline_ids": baseline_ids,
                "effective_baselines": effective_baselines,
                "baseline_price": baseline_price,
                "goods": goods_name,
            }

            return {
                "ok": True,
                "message": "基线关联检查完成",
                "goods": goods_name,
                "match_status": current_status,
                "has_baseline": has_baseline,
                "has_effective": has_baseline,
                "effective": has_baseline,
                "baseline_count": len(baselines),
                "effective_baseline_count": len(baselines),
                "baseline_ids": baseline_ids,
                "effective_baselines": effective_baselines,
                "baseline_price": baseline_price,
                "baseline_prices": [b.get("基线价格") for b in baselines],
                "match_eligible": True,
                "eligible": True,
                "can_associate": True,
                "match_qualification": "eligible",
                "baseline": baseline_view,
                "baseline_usable": baseline_usable,
                "baseline_available": baseline_usable,
                "baseline_ready": baseline_usable,
                "can_use_baseline": baseline_usable,
                "usable": baseline_usable,
                "ready": baseline_usable,
                "processing": seal_pending,
                "pending": seal_pending,
                "review_processing": review_pending,
                "review_seal_pending": seal_pending,
                "qualification": "eligible",
                "eligibility": "eligible",
                "eligibility_status": eligibility["status"],
                "eligibility_detail": eligibility,
                "reason_code": reason_code,
                "reason": reason,
                "eligible_reason": reason,
                "eligibility_reason": reason,
                "match_reason": reason,
                "pending_reason": processing_reason,
                "blocked_reason": processing_reason
                or (None if has_baseline else reason),
                "processing_reason": processing_reason,
                "review_id": review_id,
                "review_code": review_code,
                "receipt_id": receipt.get("receipt_id"),
                "confirmation_id": receipt.get("receipt_id"),
                "review_status": review_status,
                "reviewer": reviewer,
                "reviewed_by": reviewer,
                "审核人": reviewer,
                "review_conclusion": conclusion,
                "conclusion": conclusion,
                "confirmed_at": confirmed_at,
                "review_time": confirmed_at,
                "复核角色": reviewer,
                "复核结论": conclusion,
                "复核时间": confirmed_at,
                "confirmation": receipt or None,
                "confirmation_receipt": receipt or None,
                "evidence": evidence,
                "basis": evidence,
            }

    def inspect_baseline_link(self, entry_id: int) -> dict[str, Any]:
        """基线关联检查的语义化别名。"""
        return self.link_goods_baseline(entry_id)

    check_baseline = inspect_baseline_link
    baseline_eligibility = inspect_baseline_link
    get_baseline_eligibility = inspect_baseline_link
    check_match_eligibility = inspect_baseline_link

    def batch_summary(self, batch: str) -> dict[str, Any]:
        """汇总匹配批次内各状态数量并上报指标。"""
        rows = [r for r in store.rows(self.MODULE) if r.get("匹配批次") == batch]
        if not rows:
            return {"ok": False, "message": f"批次 {batch} 不存在"}
        matched = sum(1 for r in rows if r.get("status") == "已匹配")
        review = sum(1 for r in rows if r.get("status") == "人工复核")
        return {
            "ok": True,
            "message": "批次汇总完成",
            "total": len(rows),
            "matched": matched,
            "need_review": review,
            "failed": len(rows) - matched - review,
        }
