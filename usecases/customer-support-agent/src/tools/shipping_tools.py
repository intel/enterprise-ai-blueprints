# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Shipping rate and delivery tools."""
from datetime import datetime, timedelta
from tools.registry import tool
from backend.database import query_one, execute
import json


@tool(
    name="get_shipping_rates",
    description="Calculate available shipping options and costs for a given destination.",
    parameters={
        "type": "object",
        "properties": {
            "zip_code": {"type": "string", "description": "Destination ZIP code"},
            "order_total": {"type": "number", "description": "Order subtotal (for free shipping eligibility)"},
            "shipping_speed": {"type": "string", "description": "Specific speed: standard, express, next_day"}
        },
        "required": ["zip_code"]
    }
)
def get_shipping_rates(zip_code: str, order_total: float = 0, shipping_speed: str = None) -> dict:
    options = [
        {
            "speed": "standard",
            "name": "Standard Shipping",
            "delivery_days": "5-7 business days",
            "cost": 0.00 if order_total >= 50 else 5.99,
            "estimated_delivery": (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d"),
            "note": "Free on orders over $50" if order_total < 50 else "FREE",
        },
        {
            "speed": "express",
            "name": "Express Shipping",
            "delivery_days": "2-3 business days",
            "cost": 12.99,
            "estimated_delivery": (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d"),
        },
        {
            "speed": "next_day",
            "name": "Next-Day Delivery",
            "delivery_days": "1 business day",
            "cost": 24.99,
            "estimated_delivery": (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d"),
            "note": "Order by 2 PM for same-day dispatch",
        },
    ]

    if shipping_speed:
        options = [o for o in options if o["speed"] == shipping_speed]

    return {
        "zip_code": zip_code,
        "order_total": order_total,
        "free_shipping_threshold": 50.00,
        "options": options,
    }


@tool(
    name="estimate_delivery_date",
    description="Get estimated delivery date for an existing order.",
    parameters={
        "type": "object",
        "properties": {
            "order_id": {"type": "string", "description": "The order ID"}
        },
        "required": ["order_id"]
    }
)
def estimate_delivery_date(order_id: str) -> dict:
    order = query_one("SELECT id, status, estimated_delivery, carrier, tracking_number, order_date FROM orders WHERE id = ?", (order_id,))
    if not order:
        return {"error": f"Order {order_id} not found"}

    if order["status"] == "delivered":
        return {"order_id": order_id, "status": "delivered", "message": "This order has already been delivered."}

    if order["status"] == "cancelled":
        return {"order_id": order_id, "status": "cancelled", "message": "This order was cancelled."}

    return {
        "order_id": order_id,
        "status": order["status"],
        "carrier": order["carrier"],
        "tracking_number": order["tracking_number"],
        "estimated_delivery": order["estimated_delivery"],
        "message": f"Your order is {order['status']}. Estimated delivery: {order['estimated_delivery'] or 'calculating...'}",
    }


@tool(
    name="update_shipping_address",
    description="Update the shipping address on a pending/processing order. Cannot update after shipment.",
    parameters={
        "type": "object",
        "properties": {
            "order_id": {"type": "string", "description": "The order ID"},
            "street": {"type": "string", "description": "New street address"},
            "city": {"type": "string", "description": "New city"},
            "state": {"type": "string", "description": "New state code"},
            "zip": {"type": "string", "description": "New ZIP code"},
            "country": {"type": "string", "description": "Country code (default US)"}
        },
        "required": ["order_id", "street", "city", "state", "zip"]
    }
)
def update_shipping_address(order_id: str, street: str, city: str,
                            state: str, zip: str, country: str = "US") -> dict:
    order = query_one("SELECT id, status FROM orders WHERE id = ?", (order_id,))
    if not order:
        return {"error": f"Order {order_id} not found"}

    if order["status"] not in ("pending", "processing"):
        return {
            "updated": False,
            "error": f"Cannot update address. Order status is '{order['status']}'. Address can only be changed for pending or processing orders.",
        }

    new_address = json.dumps({"street": street, "city": city, "state": state, "zip": zip, "country": country})
    execute("UPDATE orders SET shipping_address = ? WHERE id = ?", (new_address, order_id))

    return {
        "updated": True,
        "order_id": order_id,
        "new_address": {"street": street, "city": city, "state": state, "zip": zip, "country": country},
        "message": "Shipping address updated successfully.",
    }
