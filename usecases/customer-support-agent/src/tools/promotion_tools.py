# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Promotion, coupon, and loyalty reward tools."""
import json
from datetime import datetime
from tools.registry import tool
from backend.database import query_one, query_all


@tool(
    name="validate_coupon",
    description="Check if a coupon code is valid and calculate the discount amount.",
    parameters={
        "type": "object",
        "properties": {
            "coupon_code": {"type": "string", "description": "The coupon code to validate"},
            "order_total": {"type": "number", "description": "Current order total to calculate discount"},
            "product_categories": {"type": "array", "items": {"type": "string"}, "description": "Categories of items in cart"}
        },
        "required": ["coupon_code"]
    }
)
def validate_coupon(coupon_code: str, order_total: float = 0, product_categories: list = None) -> dict:
    promo = query_one("SELECT * FROM promotions WHERE code = ?", (coupon_code.upper(),))
    if not promo:
        return {"valid": False, "error": f"Coupon code '{coupon_code}' not found."}

    now = datetime.now().isoformat()
    if now > promo["valid_until"]:
        return {"valid": False, "error": "This coupon has expired.", "expired_on": promo["valid_until"]}

    if now < promo["valid_from"]:
        return {"valid": False, "error": "This coupon is not yet active.", "active_from": promo["valid_from"]}

    if promo["usage_count"] >= promo["usage_limit"]:
        return {"valid": False, "error": "This coupon has reached its usage limit."}

    if order_total > 0 and order_total < promo["min_order"]:
        return {"valid": False, "error": f"Minimum order of ${promo['min_order']:.2f} required. Your total: ${order_total:.2f}"}

    categories = json.loads(promo["applicable_categories"]) if promo["applicable_categories"] else ["all"]
    if "all" not in categories and product_categories:
        matching = [c for c in product_categories if c in categories]
        if not matching:
            return {"valid": False, "error": f"This coupon only applies to: {', '.join(categories)}"}

    # Calculate discount
    if promo["discount_type"] == "percentage":
        discount = order_total * (promo["discount_value"] / 100) if order_total else 0
        discount_display = f"{promo['discount_value']}% off"
    else:
        discount = promo["discount_value"]
        discount_display = f"${promo['discount_value']:.2f} off"

    return {
        "valid": True,
        "code": promo["code"],
        "description": promo["description"],
        "discount_type": promo["discount_type"],
        "discount_value": promo["discount_value"],
        "discount_display": discount_display,
        "calculated_discount": round(discount, 2),
        "new_total": round(max(0, order_total - discount), 2) if order_total else None,
        "min_order": promo["min_order"],
        "applicable_categories": categories,
        "valid_until": promo["valid_until"],
    }


@tool(
    name="get_active_promotions",
    description="List currently active promotions and deals.",
    parameters={
        "type": "object",
        "properties": {
            "category": {"type": "string", "description": "Filter by product category (optional)"}
        },
        "required": []
    }
)
def get_active_promotions(category: str = None) -> dict:
    now = datetime.now().isoformat()
    promos = query_all(
        "SELECT code, description, discount_type, discount_value, min_order, valid_until, applicable_categories "
        "FROM promotions WHERE valid_from <= ? AND valid_until >= ? AND usage_count < usage_limit",
        (now, now)
    )

    for p in promos:
        if p.get("applicable_categories"):
            p["applicable_categories"] = json.loads(p["applicable_categories"])

    if category:
        promos = [p for p in promos if "all" in p.get("applicable_categories", []) or category in p.get("applicable_categories", [])]

    return {"active_promotions": len(promos), "promotions": promos}


@tool(
    name="apply_price_match",
    description="Submit a price match request against a competitor's lower price.",
    parameters={
        "type": "object",
        "properties": {
            "order_id": {"type": "string", "description": "The order ID (must be within 14 days of purchase)"},
            "competitor_name": {"type": "string", "description": "Competitor name (Amazon, Best Buy, Walmart)"},
            "competitor_price": {"type": "number", "description": "The lower price found at competitor"}
        },
        "required": ["order_id", "competitor_name", "competitor_price"]
    }
)
def apply_price_match(order_id: str, competitor_name: str, competitor_price: float) -> dict:
    order = query_one("SELECT id, total, order_date FROM orders WHERE id = ?", (order_id,))
    if not order:
        return {"error": f"Order {order_id} not found"}

    order_date = datetime.fromisoformat(order["order_date"])
    days_since = (datetime.now() - order_date).days
    if days_since > 14:
        return {"approved": False, "error": f"Price match must be requested within 14 days. Order was placed {days_since} days ago."}

    approved_retailers = ["amazon", "best buy", "walmart", "target", "newegg"]
    if competitor_name.lower() not in approved_retailers:
        return {"approved": False, "error": f"'{competitor_name}' is not an approved price match retailer. Approved: {', '.join(approved_retailers)}"}

    difference = order["total"] - competitor_price
    if difference <= 0:
        return {"approved": False, "error": "Competitor price is not lower than your purchase price."}

    return {
        "approved": True,
        "order_id": order_id,
        "original_price": order["total"],
        "competitor_price": competitor_price,
        "competitor": competitor_name,
        "refund_amount": round(difference, 2),
        "message": f"Price match approved! Refund of ${difference:.2f} will be processed within 3-5 business days.",
    }


@tool(
    name="get_loyalty_rewards",
    description="List available rewards that a customer can redeem with their loyalty points.",
    parameters={
        "type": "object",
        "properties": {
            "customer_id": {"type": "string", "description": "The customer ID"}
        },
        "required": ["customer_id"]
    }
)
def get_loyalty_rewards(customer_id: str) -> dict:
    customer = query_one("SELECT loyalty_points, loyalty_tier FROM customers WHERE id = ?", (customer_id,))
    if not customer:
        return {"error": f"Customer {customer_id} not found"}

    points = customer["loyalty_points"]
    rewards = [
        {"name": "$5 Store Credit", "points_required": 500, "type": "credit"},
        {"name": "$10 Store Credit", "points_required": 1000, "type": "credit"},
        {"name": "$25 Store Credit", "points_required": 2500, "type": "credit"},
        {"name": "$50 Store Credit", "points_required": 5000, "type": "credit"},
        {"name": "Free Express Shipping (1 use)", "points_required": 800, "type": "shipping"},
        {"name": "Free Next-Day Shipping (1 use)", "points_required": 1500, "type": "shipping"},
        {"name": "10% Off Next Order", "points_required": 2000, "type": "discount"},
        {"name": "20% Off Accessories", "points_required": 3000, "type": "discount"},
        {"name": "Extended Warranty (6 months)", "points_required": 5000, "type": "warranty"},
    ]

    for r in rewards:
        r["redeemable"] = points >= r["points_required"]

    available = [r for r in rewards if r["redeemable"]]
    return {
        "customer_id": customer_id,
        "current_points": points,
        "tier": customer["loyalty_tier"],
        "available_rewards": available,
        "all_rewards": rewards,
    }
