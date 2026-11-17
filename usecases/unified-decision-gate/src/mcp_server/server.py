# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""
Banking MCP server (FastMCP over Streamable HTTP).

Exposes a set of read + a few write banking tools backed by synthetic in-memory
data (see data.py). Runs as a sidecar container next to the agent; the agent
connects as a remote MCP client at http://localhost:8000/mcp.

Transport: Streamable HTTP on 0.0.0.0:8000, mount path /mcp.
stateless_http=True keeps the server free of per-session state so it needs no
sticky sessions (fine here because the data is read-mostly synthetic demo data).

All tools return JSON-serializable dicts. Errors are returned as structured
{"error": "..."} payloads (not exceptions) so the LLM can reason about them.
Ownership is enforced server-side where a user_id is available.
"""

from __future__ import annotations

import logging
import os

from mcp.server.fastmcp import FastMCP

from . import data

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

MCP_HOST = os.getenv("MCP_HOST", "0.0.0.0")  # nosec B104 - containerized service must bind all interfaces to be reachable
MCP_PORT = int(os.getenv("MCP_PORT", "8000"))

# Bind host/port explicitly on the constructor — mcp.run() may otherwise bind
# loopback, which is wrong inside a container.
mcp = FastMCP("banking", stateless_http=True, host=MCP_HOST, port=MCP_PORT)


# ---------------------------------------------------------------------------
# Identity / profile
# ---------------------------------------------------------------------------

@mcp.tool()
def get_user(user_id: str) -> dict:
    """Get a banking customer's profile by their user id (e.g. "U1001").

    Returns name, contact details, masked SSN (last 4 only), and customer-since
    date. Returns {"error": ...} if no such user exists.
    """
    user = data.masked_user(user_id)
    if not user:
        return {"error": f"No user found with id '{user_id}'."}
    return user


@mcp.tool()
def find_user(name: str | None = None, email: str | None = None) -> dict:
    """Look up customers by (partial) name and/or exact email to resolve identity.

    Use this when the user gives their name/email but not their user id. Returns
    a list of matching users (each with their user_id). Provide at least one of
    name or email.
    """
    if not name and not email:
        return {"error": "Provide at least one of 'name' or 'email'."}
    matches = data.find_users(name=name, email=email)
    return {"count": len(matches), "users": matches}


# ---------------------------------------------------------------------------
# Accounts & balances
# ---------------------------------------------------------------------------

@mcp.tool()
def get_accounts(user_id: str) -> dict:
    """List all bank accounts (checking / savings / credit) owned by a user.

    Returns each account's id, type, nickname, currency, opened date, and status.
    """
    if not data.USERS.get(user_id):
        return {"error": f"No user found with id '{user_id}'."}
    accounts = data.list_accounts_for(user_id)
    return {"user_id": user_id, "count": len(accounts), "accounts": accounts}


@mcp.tool()
def get_account_balance(account_id: str) -> dict:
    """Get the available and current balance for a single account by account id.

    For credit accounts, current_balance is the amount owed and available_balance
    is the remaining credit line.
    """
    bal = data.get_balance(account_id)
    if bal is None:
        return {"error": f"No account found with id '{account_id}'."}
    return {"account_id": account_id, **bal}


@mcp.tool()
def get_balances(user_id: str) -> dict:
    """Get balances across all of a user's accounts in one call.

    Returns a per-account balance list plus a total of available balances across
    the user's deposit accounts (checking + savings).
    """
    if not data.USERS.get(user_id):
        return {"error": f"No user found with id '{user_id}'."}
    accounts = data.list_accounts_for(user_id)
    balances = []
    total_available = 0.0
    for acc in accounts:
        bal = data.get_balance(acc["account_id"])
        if bal is None:
            continue
        entry = {"account_id": acc["account_id"], "type": acc["type"],
                 "nickname": acc["nickname"], **bal}
        balances.append(entry)
        if acc["type"] in ("checking", "savings"):
            total_available += bal["available_balance"]
    return {
        "user_id": user_id,
        "balances": balances,
        "total_available_deposits": round(total_available, 2),
    }


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------

@mcp.tool()
def get_transactions(account_id: str, limit: int = 10, since: str | None = None) -> dict:
    """Get recent transactions for an account, newest first.

    Args:
        account_id: the account to query.
        limit: max number of transactions to return (default 10).
        since: optional ISO date (YYYY-MM-DD); only return transactions on/after it.

    Amounts are positive for credits (deposits) and negative for debits.
    """
    if data.get_balance(account_id) is None:
        return {"error": f"No account found with id '{account_id}'."}
    txns = data.list_transactions(account_id, limit=limit, since=since)
    return {"account_id": account_id, "count": len(txns), "transactions": txns}


# ---------------------------------------------------------------------------
# Loans
# ---------------------------------------------------------------------------

@mcp.tool()
def get_loans(user_id: str) -> dict:
    """List all loans held by a user (mortgage / auto / personal / student).

    Returns each loan's remaining principal, APR, monthly payment, and next due date.
    """
    if not data.USERS.get(user_id):
        return {"error": f"No user found with id '{user_id}'."}
    loans = data.list_loans_for(user_id)
    return {"user_id": user_id, "count": len(loans), "loans": loans}


@mcp.tool()
def get_loan_summary(loan_id: str) -> dict:
    """Get a payoff-oriented summary for a single loan by loan id.

    Includes the current payoff amount (remaining principal), APR, monthly
    payment, an estimated number of payments remaining, and the next due date.
    """
    loan = data.get_loan(loan_id)
    if not loan:
        return {"error": f"No loan found with id '{loan_id}'."}
    monthly = loan.get("monthly_payment") or 0
    remaining = loan.get("principal_remaining") or 0
    # Simple estimate ignoring interest — indicative only, for a demo.
    payments_remaining = int(-(-remaining // monthly)) if monthly else None
    return {
        "loan_id": loan_id,
        "type": loan["type"],
        "payoff_amount": remaining,
        "apr": loan["apr"],
        "monthly_payment": monthly,
        "estimated_payments_remaining": payments_remaining,
        "next_due_date": loan["next_due_date"],
        "status": loan["status"],
    }


# ---------------------------------------------------------------------------
# Operations (mutating) — the agent must confirm with the user before calling.
# ---------------------------------------------------------------------------

@mcp.tool()
def transfer_funds(from_account: str, to_account: str, amount: float, memo: str = "") -> dict:
    """Transfer money between two accounts (mutates in-memory demo balances).

    Validates that both accounts exist and that the source has sufficient
    available funds. Returns a confirmation id and the updated balances, or
    {"error": ...} on failure. This is a demo — no real money moves.
    """
    return data.apply_transfer(from_account, to_account, amount, memo)


@mcp.tool()
def pay_loan(loan_id: str, from_account: str, amount: float) -> dict:
    """Make a payment toward a loan from one of the user's accounts.

    Validates funds and reduces the loan's remaining principal. Returns a
    confirmation id, the amount applied, and the new remaining principal, or
    {"error": ...} on failure. Demo only — no real money moves.
    """
    return data.apply_loan_payment(loan_id, from_account, amount)


if __name__ == "__main__":
    logger.info("Starting Banking MCP server on %s:%s (path /mcp)", MCP_HOST, MCP_PORT)
    # Streamable HTTP transport; serves the MCP endpoint at /mcp.
    mcp.run(transport="streamable-http")
