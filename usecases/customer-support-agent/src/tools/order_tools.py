# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Order lookup and tracking tools."""
import json
from tools.registry import tool
from backend.database import query_one, query_all


@tool(
    name="get_order_details",
    description="Look up full order details by order ID. Returns order status, items, shipping address, payment, and tracking info.",
    parameters={
        "type": "object",
        "properties": {
            "order_id": {"type": "string", "description": "The order ID (e.g. ORD-2024-000123)"}
        },
        "required": ["order_id"]
    }
)
def get_order_details(order_id: str) -> dict:
    order = query_one("SELECT * FROM orders WHERE id = ?", (order_id,))
    if not order:
        return {"error": f"Order {order_id} not found"}
    items = query_all("SELECT * FROM order_items WHERE order_id = ?", (order_id,))
    order["items"] = items
    if order.get("shipping_address"):
        order["shipping_address"] = json.loads(order["shipping_address"])
    return order


@tool(
    name="get_orders_by_email",
    description="Find orders by customer email address. Returns a list of order summaries.",
    parameters={
        "type": "object",
        "properties": {
            "email": {"type": "string", "description": "Customer email address"}
        },
        "required": ["email"]
    }
)
def get_orders_by_email(email: str) -> dict:
    customer = query_one("SELECT id FROM customers WHERE email = ?", (email,))
    if not customer:
        return {"error": f"No customer found with email {email}"}
    orders = query_all(
        "SELECT id, status, order_date, total FROM orders WHERE customer_id = ? ORDER BY order_date DESC LIMIT 10",
        (customer["id"],)
    )
    return {"customer_id": customer["id"], "orders": orders}


@tool(
    name="track_shipment",
    description="Get shipment tracking events timeline for an order.",
    parameters={
        "type": "object",
        "properties": {
            "order_id": {"type": "string", "description": "The order ID to track"}
        },
        "required": ["order_id"]
    }
)
def track_shipment(order_id: str) -> dict:
    order = query_one("SELECT id, status, tracking_number, carrier, estimated_delivery FROM orders WHERE id = ?", (order_id,))
    if not order:
        return {"error": f"Order {order_id} not found"}
    if not order.get("tracking_number"):
        return {"order_id": order_id, "status": order["status"], "message": "Order has not been shipped yet. No tracking available."}
    events = query_all(
        "SELECT event_type, location, timestamp, description FROM shipping_events WHERE order_id = ? ORDER BY timestamp ASC",
        (order_id,)
    )
    return {
        "order_id": order_id,
        "status": order["status"],
        "carrier": order["carrier"],
        "tracking_number": order["tracking_number"],
        "estimated_delivery": order["estimated_delivery"],
        "events": events,
    }


@tool(
    name="list_customer_orders",
    description="List orders for a customer with optional status filtering.",
    parameters={
        "type": "object",
        "properties": {
            "customer_id": {"type": "string", "description": "The customer ID"},
            "status_filter": {"type": "string", "description": "Filter by status: pending, processing, shipped, delivered, cancelled, returned"},
            "limit": {"type": "integer", "description": "Max results to return (default 10)"}
        },
        "required": ["customer_id"]
    }
)
def list_customer_orders(customer_id: str, status_filter: str = None, limit: int = 10) -> dict:
    if status_filter:
        orders = query_all(
            "SELECT id, status, order_date, total FROM orders WHERE customer_id = ? AND status = ? ORDER BY order_date DESC LIMIT ?",
            (customer_id, status_filter, limit)
        )
    else:
        orders = query_all(
            "SELECT id, status, order_date, total FROM orders WHERE customer_id = ? ORDER BY order_date DESC LIMIT ?",
            (customer_id, limit)
        )
    return {"customer_id": customer_id, "total_found": len(orders), "orders": orders}
