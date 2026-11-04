# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Escalation and support ticket tools."""
import uuid
from datetime import datetime
from tools.registry import tool
from backend.database import query_one, query_all, execute


@tool(
    name="transfer_to_human_agent",
    description="Escalate the conversation to a human support agent. Use when the customer explicitly requests human help or the issue is too complex.",
    parameters={
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "Brief summary of the issue for the human agent"},
            "priority": {"type": "string", "description": "Priority: low, medium, high, urgent"},
            "department": {"type": "string", "description": "Department: general, billing, technical, returns, shipping"}
        },
        "required": ["summary", "priority", "department"]
    }
)
def transfer_to_human_agent(summary: str, priority: str = "medium", department: str = "general") -> dict:
    queue_times = {"low": "15-20 minutes", "medium": "8-12 minutes", "high": "3-5 minutes", "urgent": "1-2 minutes"}
    return {
        "transferred": True,
        "department": department,
        "priority": priority,
        "queue_position": 3 if priority == "urgent" else 7,
        "estimated_wait": queue_times.get(priority, "10-15 minutes"),
        "reference_number": f"ESC-{uuid.uuid4().hex[:8].upper()}",
        "message": f"Transferring to {department} team. A specialist will be with you shortly.",
        "summary_passed": summary,
    }


@tool(
    name="create_support_ticket",
    description="Create a support ticket for follow-up on issues that can't be resolved immediately.",
    parameters={
        "type": "object",
        "properties": {
            "customer_id": {"type": "string", "description": "The customer ID"},
            "subject": {"type": "string", "description": "Ticket subject line"},
            "description": {"type": "string", "description": "Detailed description of the issue"},
            "priority": {"type": "string", "description": "Priority: low, medium, high, urgent"},
            "category": {"type": "string", "description": "Category: order_issue, product_defect, billing, shipping, account, other"}
        },
        "required": ["customer_id", "subject", "description"]
    }
)
def create_support_ticket(customer_id: str, subject: str, description: str,
                          priority: str = "medium", category: str = "other") -> dict:
    ticket_id = f"TKT-{uuid.uuid4().hex[:8].upper()}"
    now = datetime.now().isoformat()

    execute(
        "INSERT INTO support_tickets (id, customer_id, subject, description, status, priority, category, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (ticket_id, customer_id, subject, description, "open", priority, category, now, now)
    )

    response_times = {"low": "48 hours", "medium": "24 hours", "high": "4 hours", "urgent": "1 hour"}
    return {
        "ticket_id": ticket_id,
        "status": "open",
        "priority": priority,
        "category": category,
        "expected_response": response_times.get(priority, "24 hours"),
        "message": f"Ticket {ticket_id} created. Our team will respond within {response_times.get(priority, '24 hours')}.",
    }


@tool(
    name="get_ticket_status",
    description="Check the status of an existing support ticket.",
    parameters={
        "type": "object",
        "properties": {
            "ticket_id": {"type": "string", "description": "The ticket ID (e.g. TKT-ABCD1234)"}
        },
        "required": ["ticket_id"]
    }
)
def get_ticket_status(ticket_id: str) -> dict:
    ticket = query_one("SELECT * FROM support_tickets WHERE id = ?", (ticket_id,))
    if not ticket:
        return {"error": f"Ticket {ticket_id} not found"}

    notes = query_all(
        "SELECT author, note, created_at FROM ticket_notes WHERE ticket_id = ? ORDER BY created_at DESC LIMIT 5",
        (ticket_id,)
    )
    ticket["notes"] = notes
    return ticket
