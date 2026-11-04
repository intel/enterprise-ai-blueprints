# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Communication tools — email, SMS, callback scheduling."""
import uuid
from datetime import datetime, timedelta
from tools.registry import tool
from backend.database import query_one


@tool(
    name="send_order_confirmation_email",
    description="Resend the order confirmation email to the customer.",
    parameters={
        "type": "object",
        "properties": {
            "order_id": {"type": "string", "description": "The order ID"},
            "customer_id": {"type": "string", "description": "The customer ID"}
        },
        "required": ["order_id", "customer_id"]
    }
)
def send_order_confirmation_email(order_id: str, customer_id: str) -> dict:
    order = query_one("SELECT id, status, total FROM orders WHERE id = ? AND customer_id = ?", (order_id, customer_id))
    if not order:
        return {"error": f"Order {order_id} not found for customer {customer_id}"}

    customer = query_one("SELECT email, first_name FROM customers WHERE id = ?", (customer_id,))
    if not customer:
        return {"error": f"Customer {customer_id} not found"}

    email_parts = customer["email"].split("@")
    masked_email = email_parts[0][:2] + "***@" + email_parts[1]

    return {
        "sent": True,
        "email_to": masked_email,
        "order_id": order_id,
        "message": f"Order confirmation for {order_id} has been resent to {masked_email}.",
    }


@tool(
    name="send_return_label",
    description="Email a prepaid return shipping label to the customer.",
    parameters={
        "type": "object",
        "properties": {
            "return_id": {"type": "string", "description": "The return ID or RMA number"},
            "customer_id": {"type": "string", "description": "The customer ID"}
        },
        "required": ["return_id", "customer_id"]
    }
)
def send_return_label(return_id: str, customer_id: str) -> dict:
    customer = query_one("SELECT email, first_name FROM customers WHERE id = ?", (customer_id,))
    if not customer:
        return {"error": f"Customer {customer_id} not found"}

    email_parts = customer["email"].split("@")
    masked_email = email_parts[0][:2] + "***@" + email_parts[1]
    tracking = f"1Z{uuid.uuid4().hex[:12].upper()}"

    return {
        "sent": True,
        "email_to": masked_email,
        "return_id": return_id,
        "return_tracking": tracking,
        "carrier": "FedEx",
        "drop_off_locations": "Any FedEx location or schedule a pickup at fedex.com",
        "message": f"Return label sent to {masked_email}. Tracking: {tracking}. Drop off at any FedEx location.",
    }


@tool(
    name="schedule_callback",
    description="Schedule a phone callback from a support specialist.",
    parameters={
        "type": "object",
        "properties": {
            "customer_id": {"type": "string", "description": "The customer ID"},
            "phone": {"type": "string", "description": "Phone number to call back"},
            "preferred_time": {"type": "string", "description": "Preferred callback time: morning (9am-12pm), afternoon (12pm-5pm), evening (5pm-8pm), or specific time like '2:30 PM'"},
            "reason": {"type": "string", "description": "Brief reason for the callback"}
        },
        "required": ["customer_id", "phone", "preferred_time"]
    }
)
def schedule_callback(customer_id: str, phone: str, preferred_time: str, reason: str = "") -> dict:
    customer = query_one("SELECT first_name FROM customers WHERE id = ?", (customer_id,))
    if not customer:
        return {"error": f"Customer {customer_id} not found"}

    time_slots = {
        "morning": "9:00 AM - 12:00 PM",
        "afternoon": "12:00 PM - 5:00 PM",
        "evening": "5:00 PM - 8:00 PM",
    }
    slot = time_slots.get(preferred_time.lower(), preferred_time)
    callback_date = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    return {
        "scheduled": True,
        "customer_name": customer["first_name"],
        "phone": phone[:3] + "***" + phone[-4:] if len(phone) > 7 else phone,
        "callback_date": callback_date,
        "callback_time": slot,
        "reference": f"CB-{uuid.uuid4().hex[:6].upper()}",
        "reason": reason,
        "message": f"Callback scheduled for {callback_date} during {slot}. A specialist will call {phone[:3]}***{phone[-4:]}.",
    }
