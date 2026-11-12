# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""SQLite database setup and access layer."""
import sqlite3
import json
import os
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

from config import DB_PATH

_db_initialized = False


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def get_db():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    """Create all tables if they don't exist."""
    global _db_initialized
    if _db_initialized:
        return
    with get_db() as conn:
        conn.executescript(SCHEMA_SQL)
    _db_initialized = True


def query_one(sql: str, params: tuple = ()) -> Optional[Dict[str, Any]]:
    with get_db() as conn:
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None


def query_all(sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def execute(sql: str, params: tuple = ()) -> int:
    with get_db() as conn:
        cur = conn.execute(sql, params)
        return cur.lastrowid


def execute_many(sql: str, params_list: list):
    with get_db() as conn:
        conn.executemany(sql, params_list)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS customers (
    id TEXT PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    first_name TEXT NOT NULL,
    last_name TEXT NOT NULL,
    phone TEXT,
    shipping_address TEXT,  -- JSON
    billing_address TEXT,   -- JSON
    loyalty_tier TEXT DEFAULT 'bronze',
    loyalty_points INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    brand TEXT NOT NULL,
    base_price REAL NOT NULL,
    description TEXT,
    specs TEXT,         -- JSON
    rating_avg REAL DEFAULT 0.0,
    review_count INTEGER DEFAULT 0,
    image_url TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS product_variants (
    id TEXT PRIMARY KEY,
    product_id TEXT NOT NULL,
    sku TEXT UNIQUE NOT NULL,
    options TEXT,      -- JSON
    price REAL NOT NULL,
    stock_quantity INTEGER DEFAULT 0,
    is_available INTEGER DEFAULT 1,
    FOREIGN KEY (product_id) REFERENCES products(id)
);

CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    order_date TEXT NOT NULL,
    total REAL NOT NULL,
    shipping_address TEXT,  -- JSON
    billing_address TEXT,   -- JSON
    payment_method TEXT,
    tracking_number TEXT,
    carrier TEXT,
    estimated_delivery TEXT,
    FOREIGN KEY (customer_id) REFERENCES customers(id)
);

CREATE TABLE IF NOT EXISTS order_items (
    id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    product_id TEXT NOT NULL,
    product_name TEXT NOT NULL,
    variant_id TEXT,
    quantity INTEGER DEFAULT 1,
    unit_price REAL NOT NULL,
    item_status TEXT DEFAULT 'active',
    FOREIGN KEY (order_id) REFERENCES orders(id),
    FOREIGN KEY (product_id) REFERENCES products(id)
);

CREATE TABLE IF NOT EXISTS shipping_events (
    id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    tracking_number TEXT NOT NULL,
    event_type TEXT NOT NULL,
    location TEXT,
    timestamp TEXT NOT NULL,
    description TEXT,
    FOREIGN KEY (order_id) REFERENCES orders(id)
);

CREATE TABLE IF NOT EXISTS returns (
    id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'requested',
    reason TEXT,
    rma_number TEXT UNIQUE,
    refund_amount REAL DEFAULT 0,
    refund_method TEXT,
    items TEXT,  -- JSON list of item_ids
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    FOREIGN KEY (order_id) REFERENCES orders(id),
    FOREIGN KEY (customer_id) REFERENCES customers(id)
);

CREATE TABLE IF NOT EXISTS support_tickets (
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    subject TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    priority TEXT DEFAULT 'medium',
    category TEXT,
    assigned_to TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (customer_id) REFERENCES customers(id)
);

CREATE TABLE IF NOT EXISTS ticket_notes (
    id TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL,
    author TEXT NOT NULL,
    note TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (ticket_id) REFERENCES support_tickets(id)
);

CREATE TABLE IF NOT EXISTS knowledge_base (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    category TEXT NOT NULL,
    tags TEXT,  -- JSON
    helpful_count INTEGER DEFAULT 0,
    view_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS promotions (
    id TEXT PRIMARY KEY,
    code TEXT UNIQUE NOT NULL,
    description TEXT,
    discount_type TEXT NOT NULL,
    discount_value REAL NOT NULL,
    min_order REAL DEFAULT 0,
    valid_from TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    applicable_categories TEXT,  -- JSON
    usage_limit INTEGER DEFAULT 0,
    usage_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS warranty_claims (
    id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    issue_description TEXT,
    status TEXT DEFAULT 'pending',
    claim_date TEXT NOT NULL,
    resolution TEXT,
    FOREIGN KEY (order_id) REFERENCES orders(id),
    FOREIGN KEY (customer_id) REFERENCES customers(id)
);

CREATE TABLE IF NOT EXISTS product_reviews (
    id TEXT PRIMARY KEY,
    product_id TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    rating INTEGER NOT NULL,
    title TEXT,
    content TEXT,
    verified_purchase INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY (product_id) REFERENCES products(id)
);

CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items(order_id);
CREATE INDEX IF NOT EXISTS idx_shipping_order ON shipping_events(order_id);
CREATE INDEX IF NOT EXISTS idx_returns_order ON returns(order_id);
CREATE INDEX IF NOT EXISTS idx_tickets_customer ON support_tickets(customer_id);
CREATE INDEX IF NOT EXISTS idx_products_category ON products(category);
CREATE INDEX IF NOT EXISTS idx_variants_product ON product_variants(product_id);
CREATE INDEX IF NOT EXISTS idx_reviews_product ON product_reviews(product_id);
CREATE INDEX IF NOT EXISTS idx_customers_email ON customers(email);
"""
