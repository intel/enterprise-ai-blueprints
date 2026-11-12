# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Pydantic data models for TechGadgets.com."""
from __future__ import annotations
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel


class OrderStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    RETURNED = "returned"


class ReturnStatus(str, Enum):
    REQUESTED = "requested"
    APPROVED = "approved"
    LABEL_SENT = "label_sent"
    IN_TRANSIT = "in_transit"
    RECEIVED = "received"
    REFUNDED = "refunded"
    DENIED = "denied"


class TicketStatus(str, Enum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    WAITING_CUSTOMER = "waiting_customer"
    RESOLVED = "resolved"
    CLOSED = "closed"


class TicketPriority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


class LoyaltyTier(str, Enum):
    BRONZE = "bronze"
    SILVER = "silver"
    GOLD = "gold"
    PLATINUM = "platinum"


class Customer(BaseModel):
    id: str
    email: str
    first_name: str
    last_name: str
    phone: str
    shipping_address: Dict[str, Any]
    billing_address: Dict[str, Any]
    loyalty_tier: LoyaltyTier = LoyaltyTier.BRONZE
    loyalty_points: int = 0
    created_at: str


class Product(BaseModel):
    id: str
    name: str
    category: str
    brand: str
    base_price: float
    description: str
    specs: Dict[str, Any]
    rating_avg: float
    review_count: int
    image_url: str = ""


class ProductVariant(BaseModel):
    id: str
    product_id: str
    sku: str
    options: Dict[str, str]
    price: float
    stock_quantity: int
    is_available: bool


class OrderItem(BaseModel):
    id: str
    order_id: str
    product_id: str
    product_name: str
    variant_id: Optional[str] = None
    quantity: int
    unit_price: float
    item_status: str


class Order(BaseModel):
    id: str
    customer_id: str
    status: OrderStatus
    order_date: str
    total: float
    shipping_address: Dict[str, Any]
    payment_method: str
    tracking_number: Optional[str] = None
    carrier: Optional[str] = None
    estimated_delivery: Optional[str] = None
    items: List[OrderItem] = []


class ShippingEvent(BaseModel):
    id: str
    order_id: str
    tracking_number: str
    event_type: str
    location: str
    timestamp: str
    description: str


class ReturnRequest(BaseModel):
    id: str
    order_id: str
    customer_id: str
    status: ReturnStatus
    reason: str
    rma_number: str
    refund_amount: float
    refund_method: str
    items: List[str]
    created_at: str
    resolved_at: Optional[str] = None


class SupportTicket(BaseModel):
    id: str
    customer_id: str
    subject: str
    description: str
    status: TicketStatus
    priority: TicketPriority
    category: str
    assigned_to: Optional[str] = None
    created_at: str
    updated_at: str


class KnowledgeBaseArticle(BaseModel):
    id: str
    title: str
    content: str
    category: str
    tags: List[str]
    helpful_count: int = 0


class Promotion(BaseModel):
    id: str
    code: str
    description: str
    discount_type: str  # "percentage" or "fixed"
    discount_value: float
    min_order: float
    valid_from: str
    valid_until: str
    applicable_categories: List[str]
    usage_limit: int
    usage_count: int


class WarrantyClaim(BaseModel):
    id: str
    order_id: str
    item_id: str
    customer_id: str
    issue_description: str
    status: str
    claim_date: str
    resolution: Optional[str] = None


class ProductReview(BaseModel):
    id: str
    product_id: str
    customer_id: str
    rating: int
    title: str
    content: str
    verified_purchase: bool
    created_at: str
