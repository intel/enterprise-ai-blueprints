# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Product search and information tools."""
import json
from tools.registry import tool
from backend.database import query_one, query_all


@tool(
    name="search_products",
    description="Search for products by keyword, category, brand, or price range. Returns matching products with prices and ratings.",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search keywords (e.g. 'gaming laptop', 'wireless headphones')"},
            "category": {"type": "string", "description": "Filter by category: laptops, smartphones, headphones, tablets, tvs, cameras, smart_home, smartwatches, speakers, accessories, gaming, storage"},
            "min_price": {"type": "number", "description": "Minimum price filter"},
            "max_price": {"type": "number", "description": "Maximum price filter"},
            "brand": {"type": "string", "description": "Filter by brand name"},
            "sort_by": {"type": "string", "description": "Sort by: price_low, price_high, rating, name"}
        },
        "required": []
    }
)
def search_products(query: str = "", category: str = None, min_price: float = None,
                    max_price: float = None, brand: str = None, sort_by: str = "rating") -> dict:
    conditions = []
    params = []

    if query:
        conditions.append("(name LIKE ? OR description LIKE ? OR category LIKE ?)")
        params.extend([f"%{query}%", f"%{query}%", f"%{query}%"])
    if category:
        conditions.append("category = ?")
        params.append(category)
    if min_price is not None:
        conditions.append("base_price >= ?")
        params.append(min_price)
    if max_price is not None:
        conditions.append("base_price <= ?")
        params.append(max_price)
    if brand:
        conditions.append("brand LIKE ?")
        params.append(f"%{brand}%")

    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    order = {"price_low": "base_price ASC", "price_high": "base_price DESC",
             "rating": "rating_avg DESC", "name": "name ASC"}.get(sort_by, "rating_avg DESC")

    products = query_all(
        f"SELECT id, name, category, brand, base_price, rating_avg, review_count, description FROM products{where} ORDER BY {order} LIMIT 10",  # nosec B608 - only literal SQL fragments are interpolated; all values are bound parameters
        tuple(params)
    )
    return {"total_results": len(products), "products": products}


@tool(
    name="get_product_details",
    description="Get full product details including specifications, pricing, variants, and review summary.",
    parameters={
        "type": "object",
        "properties": {
            "product_id": {"type": "string", "description": "The product ID (e.g. PROD-0001)"}
        },
        "required": ["product_id"]
    }
)
def get_product_details(product_id: str) -> dict:
    product = query_one("SELECT * FROM products WHERE id = ?", (product_id,))
    if not product:
        return {"error": f"Product {product_id} not found"}

    if product.get("specs"):
        product["specs"] = json.loads(product["specs"])

    variants = query_all(
        "SELECT id, sku, options, price, stock_quantity, is_available FROM product_variants WHERE product_id = ?",
        (product_id,)
    )
    for v in variants:
        if v.get("options"):
            v["options"] = json.loads(v["options"])

    product["variants"] = variants
    product["in_stock"] = any(v["stock_quantity"] > 0 for v in variants)
    return product


@tool(
    name="compare_products",
    description="Compare specifications of multiple products side by side.",
    parameters={
        "type": "object",
        "properties": {
            "product_ids": {"type": "array", "items": {"type": "string"}, "description": "List of product IDs to compare (2-4)"}
        },
        "required": ["product_ids"]
    }
)
def compare_products(product_ids: list) -> dict:
    if len(product_ids) < 2:
        return {"error": "Need at least 2 products to compare"}
    if len(product_ids) > 4:
        product_ids = product_ids[:4]

    products = []
    for pid in product_ids:
        p = query_one("SELECT * FROM products WHERE id = ?", (pid,))
        if p:
            if p.get("specs"):
                p["specs"] = json.loads(p["specs"])
            products.append(p)

    if len(products) < 2:
        return {"error": "Not enough valid products found for comparison"}

    return {"products": products, "comparison_count": len(products)}


@tool(
    name="get_product_reviews",
    description="Get customer reviews for a product with optional rating filter.",
    parameters={
        "type": "object",
        "properties": {
            "product_id": {"type": "string", "description": "The product ID"},
            "rating_filter": {"type": "integer", "description": "Show only reviews with this rating (1-5)"},
            "limit": {"type": "integer", "description": "Max reviews to return (default 5)"}
        },
        "required": ["product_id"]
    }
)
def get_product_reviews(product_id: str, rating_filter: int = None, limit: int = 5) -> dict:
    product = query_one("SELECT name, rating_avg, review_count FROM products WHERE id = ?", (product_id,))
    if not product:
        return {"error": f"Product {product_id} not found"}

    if rating_filter:
        reviews = query_all(
            "SELECT rating, title, content, verified_purchase, created_at FROM product_reviews WHERE product_id = ? AND rating = ? ORDER BY created_at DESC LIMIT ?",
            (product_id, rating_filter, limit)
        )
    else:
        reviews = query_all(
            "SELECT rating, title, content, verified_purchase, created_at FROM product_reviews WHERE product_id = ? ORDER BY created_at DESC LIMIT ?",
            (product_id, limit)
        )

    # Rating distribution
    dist = query_all(
        "SELECT rating, COUNT(*) as count FROM product_reviews WHERE product_id = ? GROUP BY rating ORDER BY rating DESC",
        (product_id,)
    )

    return {
        "product_name": product["name"],
        "average_rating": product["rating_avg"],
        "total_reviews": product["review_count"],
        "rating_distribution": {str(d["rating"]): d["count"] for d in dist},
        "reviews": reviews,
    }
