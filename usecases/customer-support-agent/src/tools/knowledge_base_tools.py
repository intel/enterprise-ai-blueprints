# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Knowledge base and FAQ retrieval tools."""
import json
from tools.registry import tool
from backend.database import query_one, query_all


@tool(
    name="search_knowledge_base",
    description="Search the FAQ and help articles knowledge base for relevant information.",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query (e.g. 'return policy', 'bluetooth troubleshooting')"},
            "category": {"type": "string", "description": "Filter by category: policies, how_to, troubleshooting, account, products"}
        },
        "required": ["query"]
    }
)
def search_knowledge_base(query: str, category: str = None) -> dict:
    conditions = ["(title LIKE ? OR content LIKE ? OR tags LIKE ?)"]
    params = [f"%{query}%", f"%{query}%", f"%{query}%"]

    if category:
        conditions.append("category = ?")
        params.append(category)

    where = " WHERE " + " AND ".join(conditions)
    articles = query_all(
        f"SELECT id, title, category, content, tags, helpful_count FROM knowledge_base{where} ORDER BY helpful_count DESC LIMIT 5",  # nosec B608 - only literal SQL fragments are interpolated; all values are bound parameters
        tuple(params)
    )

    for a in articles:
        if a.get("tags"):
            a["tags"] = json.loads(a["tags"])
        # Truncate content for search results
        if len(a.get("content", "")) > 300:
            a["content_preview"] = a["content"][:300] + "..."
        else:
            a["content_preview"] = a["content"]

    return {"total_results": len(articles), "articles": articles}


@tool(
    name="get_faq_article",
    description="Retrieve a specific FAQ or help article by its ID.",
    parameters={
        "type": "object",
        "properties": {
            "article_id": {"type": "string", "description": "The article ID (e.g. KB-0001)"}
        },
        "required": ["article_id"]
    }
)
def get_faq_article(article_id: str) -> dict:
    article = query_one("SELECT * FROM knowledge_base WHERE id = ?", (article_id,))
    if not article:
        return {"error": f"Article {article_id} not found"}
    if article.get("tags"):
        article["tags"] = json.loads(article["tags"])
    return article


@tool(
    name="get_policy_info",
    description="Retrieve store policy information (returns, warranty, shipping, privacy, price match).",
    parameters={
        "type": "object",
        "properties": {
            "policy_type": {"type": "string", "description": "Policy type: return, warranty, shipping, privacy, price_match, cancellation, loyalty"}
        },
        "required": ["policy_type"]
    }
)
def get_policy_info(policy_type: str) -> dict:
    keyword_map = {
        "return": "Return Policy",
        "warranty": "Warranty Coverage",
        "shipping": "Shipping Information",
        "privacy": "Privacy Policy",
        "price_match": "Price Match",
        "cancellation": "Cancel",
        "loyalty": "Loyalty Program",
    }
    title_search = keyword_map.get(policy_type, policy_type)
    article = query_one("SELECT * FROM knowledge_base WHERE title LIKE ? AND category = 'policies'", (f"%{title_search}%",))
    if not article:
        article = query_one("SELECT * FROM knowledge_base WHERE title LIKE ?", (f"%{title_search}%",))
    if not article:
        return {"error": f"Policy '{policy_type}' not found. Available: return, warranty, shipping, privacy, price_match, cancellation, loyalty"}
    if article.get("tags"):
        article["tags"] = json.loads(article["tags"])
    return article


@tool(
    name="get_troubleshooting_guide",
    description="Get a step-by-step troubleshooting guide for common product issues.",
    parameters={
        "type": "object",
        "properties": {
            "product_category": {"type": "string", "description": "Product category: laptop, headphones, smart_home, smartphone, tv"},
            "issue_type": {"type": "string", "description": "Issue type: power, connectivity, display, audio, performance, wifi, bluetooth"}
        },
        "required": ["product_category", "issue_type"]
    }
)
def get_troubleshooting_guide(product_category: str, issue_type: str) -> dict:
    articles = query_all(
        "SELECT * FROM knowledge_base WHERE category = 'troubleshooting' AND (title LIKE ? OR title LIKE ? OR tags LIKE ?)",
        (f"%{product_category}%", f"%{issue_type}%", f"%{issue_type}%")
    )
    if not articles:
        return {"error": f"No troubleshooting guide found for {product_category}/{issue_type}. Try searching the knowledge base."}

    for a in articles:
        if a.get("tags"):
            a["tags"] = json.loads(a["tags"])
    return {"guides": articles}
