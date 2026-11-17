# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""
Synthetic, in-memory banking dataset for the Banking Assistant MCP server.

IMPORTANT — this is 100% fake demo data:
  • No real customers, no real PII. SSNs are masked to a fake last-4 only.
  • Addresses / emails / phone numbers are obviously synthetic.
  • Data lives in module-level dicts and is rebuilt from SEED on process start.
  • Mutating operations (transfers, loan payments) change the in-memory copy
    only; nothing is persisted and everything resets on restart.

Do NOT wire this module to a real banking system.
"""

from __future__ import annotations

import copy
import itertools
from typing import Any

# ---------------------------------------------------------------------------
# Seed data — the immutable source of truth. `reset()` deep-copies this into the
# mutable working set so a fresh process (or an explicit reset) always starts
# from a known state.
# ---------------------------------------------------------------------------

_SEED_USERS: list[dict[str, Any]] = [
    {
        "user_id": "U1001",
        "name": "Ada Lovelace",
        "email": "ada.lovelace@example.com",
        "phone": "+1-555-0101",
        "address": "1 Analytical Engine Way, Springfield, EX 00001",
        "ssn_last4": "1234",
        "customer_since": "2016-03-12",
    },
    {
        "user_id": "U1002",
        "name": "Alan Turing",
        "email": "alan.turing@example.com",
        "phone": "+1-555-0102",
        "address": "42 Enigma Street, Springfield, EX 00002",
        "ssn_last4": "5678",
        "customer_since": "2019-07-01",
    },
    {
        "user_id": "U1003",
        "name": "Grace Hopper",
        "email": "grace.hopper@example.com",
        "phone": "+1-555-0103",
        "address": "9 Compiler Court, Springfield, EX 00003",
        "ssn_last4": "9012",
        "customer_since": "2021-11-20",
    },
]

_SEED_ACCOUNTS: list[dict[str, Any]] = [
    # Ada — checking, savings
    {"account_id": "ACC-1001-CHK", "user_id": "U1001", "type": "checking",
     "nickname": "Everyday Checking", "currency": "USD",
     "opened_date": "2016-03-12", "status": "active"},
    {"account_id": "ACC-1001-SAV", "user_id": "U1001", "type": "savings",
     "nickname": "Rainy Day Savings", "currency": "USD",
     "opened_date": "2017-01-05", "status": "active"},
    # Alan — checking, savings, credit
    {"account_id": "ACC-1002-CHK", "user_id": "U1002", "type": "checking",
     "nickname": "Main Checking", "currency": "USD",
     "opened_date": "2019-07-01", "status": "active"},
    {"account_id": "ACC-1002-SAV", "user_id": "U1002", "type": "savings",
     "nickname": "Holiday Fund", "currency": "USD",
     "opened_date": "2020-02-14", "status": "active"},
    {"account_id": "ACC-1002-CRD", "user_id": "U1002", "type": "credit",
     "nickname": "Rewards Card", "currency": "USD",
     "opened_date": "2021-06-30", "status": "active"},
    # Grace — checking
    {"account_id": "ACC-1003-CHK", "user_id": "U1003", "type": "checking",
     "nickname": "Primary Checking", "currency": "USD",
     "opened_date": "2021-11-20", "status": "active"},
]

_SEED_BALANCES: dict[str, dict[str, Any]] = {
    "ACC-1001-CHK": {"available_balance": 4250.75, "current_balance": 4250.75, "as_of": "2026-07-17"},
    "ACC-1001-SAV": {"available_balance": 18200.00, "current_balance": 18200.00, "as_of": "2026-07-17"},
    "ACC-1002-CHK": {"available_balance": 980.10, "current_balance": 980.10, "as_of": "2026-07-17"},
    "ACC-1002-SAV": {"available_balance": 5600.00, "current_balance": 5600.00, "as_of": "2026-07-17"},
    # credit: current_balance is amount owed; available is remaining credit line
    "ACC-1002-CRD": {"available_balance": 3200.00, "current_balance": 1800.00, "as_of": "2026-07-17"},
    "ACC-1003-CHK": {"available_balance": 12750.42, "current_balance": 12750.42, "as_of": "2026-07-17"},
}

_SEED_TRANSACTIONS: list[dict[str, Any]] = [
    # Ada checking
    {"txn_id": "T-0001", "account_id": "ACC-1001-CHK", "date": "2026-07-15", "amount": -52.30,
     "currency": "USD", "description": "Grocery purchase", "merchant": "FreshMart", "category": "groceries"},
    {"txn_id": "T-0002", "account_id": "ACC-1001-CHK", "date": "2026-07-14", "amount": -12.99,
     "currency": "USD", "description": "Streaming subscription", "merchant": "StreamCo", "category": "entertainment"},
    {"txn_id": "T-0003", "account_id": "ACC-1001-CHK", "date": "2026-07-12", "amount": 3200.00,
     "currency": "USD", "description": "Payroll deposit", "merchant": "Employer Inc", "category": "income"},
    {"txn_id": "T-0004", "account_id": "ACC-1001-CHK", "date": "2026-07-10", "amount": -88.40,
     "currency": "USD", "description": "Electric bill", "merchant": "City Power", "category": "utilities"},
    {"txn_id": "T-0005", "account_id": "ACC-1001-CHK", "date": "2026-07-08", "amount": -25.00,
     "currency": "USD", "description": "Coffee shop", "merchant": "Bean There", "category": "dining"},
    # Ada savings
    {"txn_id": "T-0006", "account_id": "ACC-1001-SAV", "date": "2026-07-01", "amount": 500.00,
     "currency": "USD", "description": "Monthly transfer to savings", "merchant": "Internal Transfer", "category": "transfer"},
    {"txn_id": "T-0007", "account_id": "ACC-1001-SAV", "date": "2026-06-01", "amount": 500.00,
     "currency": "USD", "description": "Monthly transfer to savings", "merchant": "Internal Transfer", "category": "transfer"},
    # Alan checking
    {"txn_id": "T-0008", "account_id": "ACC-1002-CHK", "date": "2026-07-16", "amount": -140.00,
     "currency": "USD", "description": "Car service", "merchant": "AutoFix", "category": "auto"},
    {"txn_id": "T-0009", "account_id": "ACC-1002-CHK", "date": "2026-07-13", "amount": -63.75,
     "currency": "USD", "description": "Restaurant", "merchant": "The Turing Table", "category": "dining"},
    {"txn_id": "T-0010", "account_id": "ACC-1002-CHK", "date": "2026-07-11", "amount": 2100.00,
     "currency": "USD", "description": "Payroll deposit", "merchant": "University", "category": "income"},
    # Alan credit
    {"txn_id": "T-0011", "account_id": "ACC-1002-CRD", "date": "2026-07-15", "amount": -220.00,
     "currency": "USD", "description": "Online electronics", "merchant": "GadgetHub", "category": "shopping"},
    {"txn_id": "T-0012", "account_id": "ACC-1002-CRD", "date": "2026-07-09", "amount": -45.60,
     "currency": "USD", "description": "Fuel", "merchant": "GasStop", "category": "auto"},
    # Grace checking
    {"txn_id": "T-0013", "account_id": "ACC-1003-CHK", "date": "2026-07-16", "amount": 4800.00,
     "currency": "USD", "description": "Consulting payment", "merchant": "Navy Contract", "category": "income"},
    {"txn_id": "T-0014", "account_id": "ACC-1003-CHK", "date": "2026-07-12", "amount": -310.00,
     "currency": "USD", "description": "Conference travel", "merchant": "AirBudget", "category": "travel"},
]

_SEED_LOANS: list[dict[str, Any]] = [
    {"loan_id": "LN-2001", "user_id": "U1001", "type": "mortgage",
     "principal_original": 320000.00, "principal_remaining": 268400.55, "apr": 3.75,
     "term_months": 360, "monthly_payment": 1482.00, "next_due_date": "2026-08-01", "status": "active"},
    {"loan_id": "LN-2002", "user_id": "U1002", "type": "auto",
     "principal_original": 28000.00, "principal_remaining": 9120.30, "apr": 5.20,
     "term_months": 60, "monthly_payment": 531.00, "next_due_date": "2026-08-05", "status": "active"},
    {"loan_id": "LN-2003", "user_id": "U1002", "type": "personal",
     "principal_original": 10000.00, "principal_remaining": 4300.00, "apr": 9.90,
     "term_months": 36, "monthly_payment": 322.00, "next_due_date": "2026-08-10", "status": "active"},
]


# ---------------------------------------------------------------------------
# Mutable working set (rebuilt from seed on import / reset)
# ---------------------------------------------------------------------------

USERS: dict[str, dict[str, Any]] = {}
ACCOUNTS: dict[str, dict[str, Any]] = {}
BALANCES: dict[str, dict[str, Any]] = {}
TRANSACTIONS: list[dict[str, Any]] = []
LOANS: dict[str, dict[str, Any]] = {}

_txn_counter = itertools.count(1000)


def reset() -> None:
    """Rebuild the mutable working set from the immutable seed."""
    global USERS, ACCOUNTS, BALANCES, TRANSACTIONS, LOANS
    USERS = {u["user_id"]: copy.deepcopy(u) for u in _SEED_USERS}
    ACCOUNTS = {a["account_id"]: copy.deepcopy(a) for a in _SEED_ACCOUNTS}
    BALANCES = copy.deepcopy(_SEED_BALANCES)
    TRANSACTIONS = copy.deepcopy(_SEED_TRANSACTIONS)
    LOANS = {loan["loan_id"]: copy.deepcopy(loan) for loan in _SEED_LOANS}


# Populate on import.
reset()


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------

def masked_user(user_id: str) -> dict[str, Any] | None:
    """Return a user profile with SSN presented as a masked last-4 only."""
    user = USERS.get(user_id)
    if not user:
        return None
    out = copy.deepcopy(user)
    out["ssn_masked"] = f"***-**-{out.pop('ssn_last4')}"
    return out


def find_users(name: str | None = None, email: str | None = None) -> list[dict[str, Any]]:
    """Case-insensitive substring match on name and/or exact email."""
    results = []
    for uid, user in USERS.items():
        if name and name.strip().lower() not in user["name"].lower():
            continue
        if email and email.strip().lower() != user["email"].lower():
            continue
        results.append(masked_user(uid))
    return results


def list_accounts_for(user_id: str) -> list[dict[str, Any]]:
    return [copy.deepcopy(a) for a in ACCOUNTS.values() if a["user_id"] == user_id]


def get_balance(account_id: str) -> dict[str, Any] | None:
    bal = BALANCES.get(account_id)
    return copy.deepcopy(bal) if bal else None


def list_transactions(account_id: str, limit: int = 10, since: str | None = None) -> list[dict[str, Any]]:
    """Return the account's transactions, newest first, optionally on/after `since` (YYYY-MM-DD)."""
    txns = [copy.deepcopy(t) for t in TRANSACTIONS if t["account_id"] == account_id]
    if since:
        txns = [t for t in txns if t["date"] >= since]
    # ISO dates sort lexicographically; newest first.
    txns.sort(key=lambda t: t["date"], reverse=True)
    return txns[: max(0, limit)]


def list_loans_for(user_id: str) -> list[dict[str, Any]]:
    return [copy.deepcopy(loan) for loan in LOANS.values() if loan["user_id"] == user_id]


def get_loan(loan_id: str) -> dict[str, Any] | None:
    loan = LOANS.get(loan_id)
    return copy.deepcopy(loan) if loan else None


# ---------------------------------------------------------------------------
# Mutation helpers (in-memory only)
# ---------------------------------------------------------------------------

def _next_txn_id() -> str:
    return f"T-{next(_txn_counter)}"


def apply_transfer(from_account: str, to_account: str, amount: float, memo: str = "") -> dict[str, Any]:
    """Move funds between two accounts. Returns a result dict or {'error': ...}."""
    if amount <= 0:
        return {"error": "Transfer amount must be positive."}
    if from_account == to_account:
        return {"error": "Source and destination accounts must differ."}

    src = ACCOUNTS.get(from_account)
    dst = ACCOUNTS.get(to_account)
    if not src:
        return {"error": f"Source account '{from_account}' not found."}
    if not dst:
        return {"error": f"Destination account '{to_account}' not found."}

    src_bal = BALANCES.get(from_account)
    dst_bal = BALANCES.get(to_account)
    if src_bal is None or dst_bal is None:
        return {"error": "Balance record missing for one of the accounts."}

    if src_bal["available_balance"] < amount:
        return {
            "error": "Insufficient funds.",
            "available_balance": src_bal["available_balance"],
            "requested": amount,
        }

    src_bal["available_balance"] = round(src_bal["available_balance"] - amount, 2)
    src_bal["current_balance"] = round(src_bal["current_balance"] - amount, 2)
    dst_bal["available_balance"] = round(dst_bal["available_balance"] + amount, 2)
    dst_bal["current_balance"] = round(dst_bal["current_balance"] + amount, 2)

    confirmation_id = _next_txn_id()
    TRANSACTIONS.append({
        "txn_id": confirmation_id, "account_id": from_account, "date": src_bal["as_of"],
        "amount": -amount, "currency": src["currency"],
        "description": memo or f"Transfer to {to_account}", "merchant": "Internal Transfer",
        "category": "transfer",
    })
    TRANSACTIONS.append({
        "txn_id": _next_txn_id(), "account_id": to_account, "date": dst_bal["as_of"],
        "amount": amount, "currency": dst["currency"],
        "description": memo or f"Transfer from {from_account}", "merchant": "Internal Transfer",
        "category": "transfer",
    })

    return {
        "status": "completed",
        "confirmation_id": confirmation_id,
        "from_account": from_account,
        "to_account": to_account,
        "amount": round(amount, 2),
        "memo": memo,
        "from_balance": copy.deepcopy(src_bal),
        "to_balance": copy.deepcopy(dst_bal),
    }


def apply_loan_payment(loan_id: str, from_account: str, amount: float) -> dict[str, Any]:
    """Pay down a loan from an account. Returns a result dict or {'error': ...}."""
    if amount <= 0:
        return {"error": "Payment amount must be positive."}

    loan = LOANS.get(loan_id)
    if not loan:
        return {"error": f"Loan '{loan_id}' not found."}

    src_bal = BALANCES.get(from_account)
    if src_bal is None:
        return {"error": f"Account '{from_account}' not found."}
    if src_bal["available_balance"] < amount:
        return {
            "error": "Insufficient funds.",
            "available_balance": src_bal["available_balance"],
            "requested": amount,
        }

    applied = min(amount, loan["principal_remaining"])
    src_bal["available_balance"] = round(src_bal["available_balance"] - applied, 2)
    src_bal["current_balance"] = round(src_bal["current_balance"] - applied, 2)
    loan["principal_remaining"] = round(loan["principal_remaining"] - applied, 2)

    confirmation_id = _next_txn_id()
    TRANSACTIONS.append({
        "txn_id": confirmation_id, "account_id": from_account, "date": src_bal["as_of"],
        "amount": -applied, "currency": "USD",
        "description": f"Payment to loan {loan_id}", "merchant": "Loan Servicing",
        "category": "loan_payment",
    })

    return {
        "status": "completed",
        "confirmation_id": confirmation_id,
        "loan_id": loan_id,
        "amount_applied": applied,
        "principal_remaining": loan["principal_remaining"],
        "from_account": from_account,
        "from_balance": copy.deepcopy(src_bal),
    }
