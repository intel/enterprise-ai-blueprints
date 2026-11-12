# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Warranty check and claims tools."""
import uuid
from datetime import datetime, timedelta
from tools.registry import tool
from backend.database import query_one, query_all, execute
from config import WARRANTY_STANDARD_YEARS


@tool(
    name="check_warranty_status",
    description="Check warranty status and coverage for a purchased item.",
    parameters={
        "type": "object",
        "properties": {
            "order_id": {"type": "string", "description": "The order ID"},
            "item_id": {"type": "string", "description": "The order item ID (optional, checks all items if omitted)"}
        },
        "required": ["order_id"]
    }
)
def check_warranty_status(order_id: str, item_id: str = None) -> dict:
    order = query_one("SELECT id, order_date, customer_id FROM orders WHERE id = ?", (order_id,))
    if not order:
        return {"error": f"Order {order_id} not found"}

    if item_id:
        items = query_all("SELECT * FROM order_items WHERE order_id = ? AND id = ?", (order_id, item_id))
    else:
        items = query_all("SELECT * FROM order_items WHERE order_id = ?", (order_id,))

    if not items:
        return {"error": "No items found for this order"}

    order_date = datetime.fromisoformat(order["order_date"])
    warranty_end = order_date + timedelta(days=365 * WARRANTY_STANDARD_YEARS)
    now = datetime.now()
    is_active = now < warranty_end
    days_remaining = (warranty_end - now).days if is_active else 0

    item_warranties = []
    for item in items:
        product = query_one("SELECT name, base_price FROM products WHERE id = ?", (item["product_id"],))
        extended_price_2yr = round(item["unit_price"] * 0.12, 2)
        extended_price_3yr = round(item["unit_price"] * 0.18, 2)

        item_warranties.append({
            "item_id": item["id"],
            "product_name": product["name"] if product else item["product_name"],
            "purchase_date": order["order_date"],
            "warranty_type": "Standard (1 year)",
            "warranty_end": warranty_end.strftime("%Y-%m-%d"),
            "is_active": is_active,
            "days_remaining": max(0, days_remaining),
            "coverage": ["Manufacturing defects", "Hardware failures", "Battery defects", "Display defects (>5 dead pixels)"],
            "not_covered": ["Physical damage", "Water damage", "Normal wear", "Software issues"],
            "extended_warranty_available": is_active,
            "extended_options": [
                {"plan": "2-Year Extended", "price": extended_price_2yr, "total_coverage": "2 years from purchase"},
                {"plan": "3-Year Extended", "price": extended_price_3yr, "total_coverage": "3 years from purchase"},
                {"plan": "Accidental Damage Add-on", "price": round(item["unit_price"] * 0.05, 2), "coverage": "Drops, spills, cracked screen"},
            ] if is_active else [],
        })

    return {"order_id": order_id, "items": item_warranties}


@tool(
    name="file_warranty_claim",
    description="File a warranty claim for a defective product.",
    parameters={
        "type": "object",
        "properties": {
            "order_id": {"type": "string", "description": "The order ID"},
            "item_id": {"type": "string", "description": "The item ID with the defect"},
            "issue_description": {"type": "string", "description": "Description of the defect or issue"}
        },
        "required": ["order_id", "item_id", "issue_description"]
    }
)
def file_warranty_claim(order_id: str, item_id: str, issue_description: str) -> dict:
    order = query_one("SELECT id, order_date, customer_id FROM orders WHERE id = ?", (order_id,))
    if not order:
        return {"error": f"Order {order_id} not found"}

    item = query_one("SELECT * FROM order_items WHERE id = ? AND order_id = ?", (item_id, order_id))
    if not item:
        return {"error": f"Item {item_id} not found in order {order_id}"}

    # Check warranty is active
    order_date = datetime.fromisoformat(order["order_date"])
    warranty_end = order_date + timedelta(days=365 * WARRANTY_STANDARD_YEARS)
    if datetime.now() > warranty_end:
        return {"filed": False, "error": "Warranty has expired. Consider purchasing an extended warranty or contacting support for repair options."}

    claim_id = f"WC-{uuid.uuid4().hex[:8].upper()}"
    execute(
        "INSERT INTO warranty_claims (id, order_id, item_id, customer_id, issue_description, status, claim_date) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (claim_id, order_id, item_id, order["customer_id"], issue_description, "submitted", datetime.now().isoformat())
    )

    return {
        "filed": True,
        "claim_id": claim_id,
        "status": "submitted",
        "product": item["product_name"],
        "issue": issue_description,
        "next_steps": [
            "A warranty specialist will review your claim within 24 hours",
            "You may be asked to provide photos of the defect",
            "If approved: replacement shipped within 3-5 business days or repair arranged",
            "You'll receive updates via email"
        ],
        "estimated_resolution": "5-10 business days",
    }
