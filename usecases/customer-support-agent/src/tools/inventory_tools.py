# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Inventory and stock checking tools."""
import json
import random
from datetime import datetime, timedelta
from tools.registry import tool
from backend.database import query_one, query_all


@tool(
    name="check_stock",
    description="Check if a product is currently in stock and available for purchase.",
    parameters={
        "type": "object",
        "properties": {
            "product_id": {"type": "string", "description": "The product ID"},
            "variant_id": {"type": "string", "description": "Specific variant ID (optional, checks all variants if omitted)"}
        },
        "required": ["product_id"]
    }
)
def check_stock(product_id: str, variant_id: str = None) -> dict:
    product = query_one("SELECT id, name FROM products WHERE id = ?", (product_id,))
    if not product:
        return {"error": f"Product {product_id} not found"}

    if variant_id:
        variants = query_all(
            "SELECT id, options, price, stock_quantity, is_available FROM product_variants WHERE id = ? AND product_id = ?",
            (variant_id, product_id)
        )
    else:
        variants = query_all(
            "SELECT id, options, price, stock_quantity, is_available FROM product_variants WHERE product_id = ?",
            (product_id,)
        )

    for v in variants:
        if v.get("options"):
            v["options"] = json.loads(v["options"])

    in_stock_variants = [v for v in variants if v["stock_quantity"] > 0]
    out_of_stock_variants = [v for v in variants if v["stock_quantity"] == 0]

    return {
        "product_id": product_id,
        "product_name": product["name"],
        "in_stock": len(in_stock_variants) > 0,
        "total_variants": len(variants),
        "in_stock_variants": in_stock_variants,
        "out_of_stock_variants": out_of_stock_variants,
    }


@tool(
    name="get_restock_estimate",
    description="Get estimated restock date for an out-of-stock product.",
    parameters={
        "type": "object",
        "properties": {
            "product_id": {"type": "string", "description": "The product ID"}
        },
        "required": ["product_id"]
    }
)
def get_restock_estimate(product_id: str) -> dict:
    product = query_one("SELECT id, name, category FROM products WHERE id = ?", (product_id,))
    if not product:
        return {"error": f"Product {product_id} not found"}

    variants = query_all(
        "SELECT stock_quantity FROM product_variants WHERE product_id = ?", (product_id,)
    )
    total_stock = sum(v["stock_quantity"] for v in variants)

    if total_stock > 0:
        return {
            "product_id": product_id,
            "product_name": product["name"],
            "currently_in_stock": True,
            "stock_quantity": total_stock,
            "message": "This product is currently in stock."
        }

    # Simulate restock estimate
    days_until_restock = random.randint(7, 21)
    restock_date = (datetime.now() + timedelta(days=days_until_restock)).strftime("%Y-%m-%d")

    return {
        "product_id": product_id,
        "product_name": product["name"],
        "currently_in_stock": False,
        "estimated_restock_date": restock_date,
        "days_until_restock": days_until_restock,
        "can_backorder": True,
        "message": f"Expected back in stock by {restock_date}. You can place a backorder or sign up for restock notifications."
    }


@tool(
    name="check_store_availability",
    description="Check if a product is available for in-store pickup at nearby locations.",
    parameters={
        "type": "object",
        "properties": {
            "product_id": {"type": "string", "description": "The product ID"},
            "zip_code": {"type": "string", "description": "ZIP code to search near"}
        },
        "required": ["product_id", "zip_code"]
    }
)
def check_store_availability(product_id: str, zip_code: str) -> dict:
    product = query_one("SELECT id, name FROM products WHERE id = ?", (product_id,))
    if not product:
        return {"error": f"Product {product_id} not found"}

    # Simulated store data
    stores = [
        {"name": "TechGadgets Downtown", "distance": "2.3 miles", "in_stock": True, "quantity": 5, "address": f"123 Main St, near {zip_code}"},
        {"name": "TechGadgets Mall", "distance": "4.7 miles", "in_stock": True, "quantity": 2, "address": f"456 Mall Blvd, near {zip_code}"},
        {"name": "TechGadgets Express", "distance": "8.1 miles", "in_stock": False, "quantity": 0, "address": f"789 Commerce Way, near {zip_code}"},
    ]
    available_stores = [s for s in stores if s["in_stock"]]

    return {
        "product_id": product_id,
        "product_name": product["name"],
        "zip_code": zip_code,
        "stores_checked": len(stores),
        "available_nearby": len(available_stores),
        "stores": stores,
    }


@tool(
    name="list_product_inventory",
    description="Browse available inventory by category with stock status.",
    parameters={
        "type": "object",
        "properties": {
            "category": {"type": "string", "description": "Product category to browse"},
            "in_stock_only": {"type": "boolean", "description": "Only show in-stock items (default true)"}
        },
        "required": []
    }
)
def list_product_inventory(category: str = None, in_stock_only: bool = True) -> dict:
    if category:
        products = query_all(
            "SELECT p.id, p.name, p.base_price, p.category, COALESCE(SUM(v.stock_quantity), 0) as total_stock "
            "FROM products p LEFT JOIN product_variants v ON p.id = v.product_id "
            "WHERE p.category = ? GROUP BY p.id ORDER BY p.name",
            (category,)
        )
    else:
        products = query_all(
            "SELECT p.id, p.name, p.base_price, p.category, COALESCE(SUM(v.stock_quantity), 0) as total_stock "
            "FROM products p LEFT JOIN product_variants v ON p.id = v.product_id "
            "GROUP BY p.id ORDER BY p.category, p.name"
        )

    if in_stock_only:
        products = [p for p in products if p["total_stock"] > 0]

    for p in products:
        p["in_stock"] = p["total_stock"] > 0

    return {"total_products": len(products), "category": category or "all", "products": products}
