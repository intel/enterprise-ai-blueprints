# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Return and refund processing tools."""
import json
import uuid
from datetime import datetime, timedelta
from tools.registry import tool
from backend.database import query_one, query_all, execute
from config import RETURN_WINDOW_DAYS


@tool(
    name="check_return_eligibility",
    description="Check if items from an order are eligible for return (within 30-day window, not already returned).",
    parameters={
        "type": "object",
        "properties": {
            "order_id": {"type": "string", "description": "The order ID"},
            "item_ids": {"type": "array", "items": {"type": "string"}, "description": "List of item IDs to check"}
        },
        "required": ["order_id"]
    }
)
def check_return_eligibility(order_id: str, item_ids: list = None) -> dict:
    order = query_one("SELECT * FROM orders WHERE id = ?", (order_id,))
    if not order:
        return {"error": f"Order {order_id} not found"}

    if order["status"] not in ("delivered", "shipped"):
        return {"eligible": False, "reason": f"Order status is '{order['status']}'. Only delivered orders can be returned."}

    order_date = datetime.fromisoformat(order["order_date"])
    days_since = (datetime.now() - order_date).days
    if days_since > RETURN_WINDOW_DAYS:
        return {"eligible": False, "reason": f"Return window expired. Order was placed {days_since} days ago (limit: {RETURN_WINDOW_DAYS} days)."}

    items = query_all("SELECT * FROM order_items WHERE order_id = ?", (order_id,))
    if item_ids:
        items = [i for i in items if i["id"] in item_ids]

    eligible_items = []
    ineligible_items = []
    for item in items:
        if item["item_status"] == "returned":
            ineligible_items.append({"item_id": item["id"], "product": item["product_name"], "reason": "Already returned"})
        else:
            eligible_items.append({"item_id": item["id"], "product": item["product_name"], "price": item["unit_price"]})

    return {
        "eligible": len(eligible_items) > 0,
        "days_remaining": RETURN_WINDOW_DAYS - days_since,
        "eligible_items": eligible_items,
        "ineligible_items": ineligible_items,
        "refund_estimate": sum(i["price"] for i in eligible_items),
    }


@tool(
    name="initiate_return",
    description="Start a return process for items in an order. Creates an RMA and return label.",
    parameters={
        "type": "object",
        "properties": {
            "order_id": {"type": "string", "description": "The order ID"},
            "item_ids": {"type": "array", "items": {"type": "string"}, "description": "List of item IDs to return"},
            "reason": {"type": "string", "description": "Reason for return (defective, wrong_item, not_as_described, changed_mind, other)"}
        },
        "required": ["order_id", "item_ids", "reason"]
    }
)
def initiate_return(order_id: str, item_ids: list, reason: str) -> dict:
    order = query_one("SELECT * FROM orders WHERE id = ?", (order_id,))
    if not order:
        return {"error": f"Order {order_id} not found"}

    items = query_all("SELECT * FROM order_items WHERE order_id = ? AND id IN ({})".format(  # nosec B608 - only '?' placeholders are interpolated
        ",".join("?" * len(item_ids))), (order_id, *item_ids))

    if not items:
        return {"error": "No valid items found for return"}

    refund_amount = sum(i["unit_price"] * i["quantity"] for i in items)
    rma_number = f"RMA-{uuid.uuid4().hex[:8].upper()}"
    return_id = f"RET-{uuid.uuid4().hex[:8]}"

    execute(
        "INSERT INTO returns (id, order_id, customer_id, status, reason, rma_number, refund_amount, refund_method, items, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (return_id, order_id, order["customer_id"], "approved", reason, rma_number,
         refund_amount, "original_payment", json.dumps(item_ids), datetime.now().isoformat())
    )

    return {
        "return_id": return_id,
        "rma_number": rma_number,
        "status": "approved",
        "refund_amount": refund_amount,
        "refund_method": "original_payment",
        "items_count": len(items),
        "next_steps": [
            f"Return label will be emailed within 1 hour",
            "Pack items in original packaging",
            "Drop off at any FedEx location",
            "Refund processed within 5-7 business days after receipt"
        ],
    }


@tool(
    name="process_refund",
    description="Process a refund for a return. Refund goes to original payment method or store credit.",
    parameters={
        "type": "object",
        "properties": {
            "order_id": {"type": "string", "description": "The order ID"},
            "refund_method": {"type": "string", "description": "Refund method: original_payment or store_credit"},
            "amount": {"type": "number", "description": "Refund amount (if partial refund)"}
        },
        "required": ["order_id", "refund_method"]
    }
)
def process_refund(order_id: str, refund_method: str = "original_payment", amount: float = None) -> dict:
    order = query_one("SELECT * FROM orders WHERE id = ?", (order_id,))
    if not order:
        return {"error": f"Order {order_id} not found"}

    refund_amount = amount or order["total"]
    return {
        "order_id": order_id,
        "refund_amount": refund_amount,
        "refund_method": refund_method,
        "status": "processing",
        "estimated_completion": (datetime.now() + timedelta(days=5)).strftime("%Y-%m-%d"),
        "confirmation_number": f"REF-{uuid.uuid4().hex[:8].upper()}",
        "message": f"Refund of ${refund_amount:.2f} will be processed to {refund_method.replace('_', ' ')} within 5-7 business days."
    }


@tool(
    name="get_return_status",
    description="Check the status of an existing return request.",
    parameters={
        "type": "object",
        "properties": {
            "return_id": {"type": "string", "description": "The return ID or RMA number"}
        },
        "required": ["return_id"]
    }
)
def get_return_status(return_id: str) -> dict:
    ret = query_one("SELECT * FROM returns WHERE id = ? OR rma_number = ?", (return_id, return_id))
    if not ret:
        return {"error": f"Return {return_id} not found"}
    if ret.get("items"):
        ret["items"] = json.loads(ret["items"])
    return ret
