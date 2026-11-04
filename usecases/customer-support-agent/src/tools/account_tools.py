# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Customer account management tools."""
import json
from tools.registry import tool
from backend.database import query_one, query_all, execute


@tool(
    name="authenticate_customer",
    description="Verify customer identity using email, phone, or order ID. Returns masked customer profile.",
    parameters={
        "type": "object",
        "properties": {
            "email": {"type": "string", "description": "Customer email address"},
            "phone": {"type": "string", "description": "Customer phone number"},
            "order_id": {"type": "string", "description": "An order ID belonging to the customer"}
        },
        "required": []
    }
)
def authenticate_customer(email: str = None, phone: str = None, order_id: str = None) -> dict:
    customer = None
    if email:
        customer = query_one("SELECT * FROM customers WHERE email = ?", (email,))
    elif phone:
        customer = query_one("SELECT * FROM customers WHERE phone = ?", (phone,))
    elif order_id:
        order = query_one("SELECT customer_id FROM orders WHERE id = ?", (order_id,))
        if order:
            customer = query_one("SELECT * FROM customers WHERE id = ?", (order["customer_id"],))

    if not customer:
        return {"authenticated": False, "error": "Customer not found. Please verify the information provided."}

    # Return masked profile
    email_parts = customer["email"].split("@")
    masked_email = email_parts[0][:2] + "***@" + email_parts[1]
    return {
        "authenticated": True,
        "customer_id": customer["id"],
        "name": f"{customer['first_name']} {customer['last_name']}",
        "email_masked": masked_email,
        "loyalty_tier": customer["loyalty_tier"],
        "loyalty_points": customer["loyalty_points"],
    }


@tool(
    name="get_customer_profile",
    description="Get full customer profile including addresses, loyalty info, and account details.",
    parameters={
        "type": "object",
        "properties": {
            "customer_id": {"type": "string", "description": "The customer ID"}
        },
        "required": ["customer_id"]
    }
)
def get_customer_profile(customer_id: str) -> dict:
    customer = query_one("SELECT * FROM customers WHERE id = ?", (customer_id,))
    if not customer:
        return {"error": f"Customer {customer_id} not found"}

    if customer.get("shipping_address"):
        customer["shipping_address"] = json.loads(customer["shipping_address"])
    if customer.get("billing_address"):
        customer["billing_address"] = json.loads(customer["billing_address"])

    order_count = query_one("SELECT COUNT(*) as count FROM orders WHERE customer_id = ?", (customer_id,))
    customer["total_orders"] = order_count["count"] if order_count else 0
    return customer


@tool(
    name="update_customer_address",
    description="Update a customer's shipping or billing address.",
    parameters={
        "type": "object",
        "properties": {
            "customer_id": {"type": "string", "description": "The customer ID"},
            "address_type": {"type": "string", "description": "Type: shipping or billing"},
            "street": {"type": "string", "description": "Street address"},
            "city": {"type": "string", "description": "City"},
            "state": {"type": "string", "description": "State code (e.g. CA, NY)"},
            "zip": {"type": "string", "description": "ZIP code"},
            "country": {"type": "string", "description": "Country code (default US)"}
        },
        "required": ["customer_id", "address_type", "street", "city", "state", "zip"]
    }
)
def update_customer_address(customer_id: str, address_type: str, street: str,
                            city: str, state: str, zip: str, country: str = "US") -> dict:
    customer = query_one("SELECT id FROM customers WHERE id = ?", (customer_id,))
    if not customer:
        return {"error": f"Customer {customer_id} not found"}

    new_address = json.dumps({"street": street, "city": city, "state": state, "zip": zip, "country": country})
    col = "shipping_address" if address_type == "shipping" else "billing_address"
    execute(f"UPDATE customers SET {col} = ? WHERE id = ?", (new_address, customer_id))

    return {
        "updated": True,
        "address_type": address_type,
        "new_address": {"street": street, "city": city, "state": state, "zip": zip, "country": country},
        "message": f"{address_type.capitalize()} address updated successfully."
    }


@tool(
    name="get_loyalty_points",
    description="Get customer's loyalty points balance, tier, and rewards information.",
    parameters={
        "type": "object",
        "properties": {
            "customer_id": {"type": "string", "description": "The customer ID"}
        },
        "required": ["customer_id"]
    }
)
def get_loyalty_points(customer_id: str) -> dict:
    customer = query_one("SELECT first_name, loyalty_tier, loyalty_points FROM customers WHERE id = ?", (customer_id,))
    if not customer:
        return {"error": f"Customer {customer_id} not found"}

    tier_benefits = {
        "bronze": {"multiplier": "1x", "perks": ["Earn 10 pts/$1", "Birthday bonus 500 pts"]},
        "silver": {"multiplier": "1.5x", "perks": ["Earn 15 pts/$1", "Birthday bonus 500 pts", "Early sale access"]},
        "gold": {"multiplier": "2x", "perks": ["Earn 20 pts/$1", "Free express shipping", "Birthday bonus 1000 pts", "Priority support"]},
        "platinum": {"multiplier": "3x", "perks": ["Earn 30 pts/$1", "Free next-day shipping", "Birthday bonus 2000 pts", "VIP support", "Early product access"]},
    }

    tier = customer["loyalty_tier"]
    next_tiers = {"bronze": ("silver", 5000), "silver": ("gold", 20000), "gold": ("platinum", 50000), "platinum": (None, None)}
    next_tier, next_threshold = next_tiers[tier]

    return {
        "customer_name": customer["first_name"],
        "tier": tier,
        "points": customer["loyalty_points"],
        "cash_value": f"${customer['loyalty_points'] // 100:.2f}",
        "benefits": tier_benefits.get(tier, {}),
        "next_tier": next_tier,
        "points_to_next_tier": max(0, next_threshold - customer["loyalty_points"]) if next_threshold else 0,
    }


@tool(
    name="reset_password",
    description="Send a password reset link to the customer's email.",
    parameters={
        "type": "object",
        "properties": {
            "customer_id": {"type": "string", "description": "The customer ID"}
        },
        "required": ["customer_id"]
    }
)
def reset_password(customer_id: str) -> dict:
    customer = query_one("SELECT email, first_name FROM customers WHERE id = ?", (customer_id,))
    if not customer:
        return {"error": f"Customer {customer_id} not found"}

    email_parts = customer["email"].split("@")
    masked = email_parts[0][:2] + "***@" + email_parts[1]
    return {
        "sent": True,
        "email_masked": masked,
        "message": f"Password reset link sent to {masked}. Link expires in 24 hours.",
    }
