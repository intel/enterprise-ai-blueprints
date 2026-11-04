# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""TechGadgets.com AI Customer Support Agent — Streamlit UI with tool calling."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ['TOKENIZERS_PARALLELISM'] = 'false'

import json
import streamlit as st

from config import LLM_BASE_URL, LLM_MODEL
from backend.database import init_db
from backend.seed_data import seed_all
from tools.registry import import_all_tools, list_tools
from agent.agent_loop import run_agent, run_agent_streaming

# ─── Initialization ──────────────────────────────────────────────────────────

@st.cache_resource
def _init():
    init_db()
    seed_all()
    import_all_tools()
    return True

_init()

# ─── Page Config ─────────────────────────────────────────────────────────────

st.set_page_config(page_title="TechGadgets Support", page_icon="🛒", layout="wide")
st.title("TechGadgets.com Customer Support 🛒")
st.caption(f"AI Agent with **{len(list_tools())} tools** | Model: `{LLM_MODEL}` | Endpoint: `{LLM_BASE_URL}`")

# ─── Sidebar ─────────────────────────────────────────────────────────────────

with st.sidebar:
    st.header("Customer Session")
    customer_id = st.text_input("Customer ID", value="CUST-0001", help="Enter a customer ID (CUST-0001 to CUST-0100)")

    st.divider()
    st.header("Available Tools")
    tools = list_tools()
    tool_categories = {}
    for t in tools:
        cat = t["name"].split("_")[0] if "_" in t["name"] else "other"
        tool_categories.setdefault(cat, []).append(t["name"])

    for cat, tool_names in sorted(tool_categories.items()):
        with st.expander(f"{cat.title()} ({len(tool_names)})"):
            for tn in tool_names:
                st.code(tn, language=None)

    st.divider()
    st.subheader("Quick Test Queries")
    test_queries = [
        "What gaming laptops do you have?",
        "Where is my order ORD-2024-000013?",
        "I want to return my headphones",
        "Is coupon SAVE20 valid?",
        "What's your return policy?",
        "Check warranty on ORD-2024-000050",
    ]
    for q in test_queries:
        if st.button(q, key=q, use_container_width=True):
            st.session_state["prefill_query"] = q

# ─── Chat State ──────────────────────────────────────────────────────────────

if "messages" not in st.session_state:
    st.session_state.messages = []
if "conversation_history" not in st.session_state:
    st.session_state.conversation_history = []

# Display chat history
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        if msg.get("tool_calls"):
            with st.expander(f"🔧 Tools called: {len(msg['tool_calls'])}"):
                for tc in msg["tool_calls"]:
                    st.markdown(f"**{tc['name']}** ({tc['duration_ms']:.0f}ms)")
                    col1, col2 = st.columns(2)
                    with col1:
                        st.json(tc["arguments"])
                    with col2:
                        result_data = tc["result"].get("data", tc["result"])
                        st.json(result_data if isinstance(result_data, dict) else {"result": result_data})

# ─── Chat Input ──────────────────────────────────────────────────────────────

prefill = st.session_state.pop("prefill_query", None)
query = st.chat_input("How can I help you today?") or prefill

if query:
    # Add user message
    st.session_state.messages.append({"role": "user", "content": query})
    with st.chat_message("user"):
        st.markdown(query)

    # Run agent
    with st.chat_message("assistant"):
        status = st.status("Thinking...", expanded=True)
        tool_calls_display = []

        def on_tool_call(name, args):
            status.update(label=f"Calling tool: {name}...", state="running")
            status.write(f"**→ {name}**({json.dumps(args, default=str)[:100]})")

        def on_tool_result(name, result):
            data = result.get("data", result)
            preview = json.dumps(data, default=str)[:200]
            status.write(f"**← {name}** returned: `{preview}`")

        response = run_agent(
            query=query,
            customer_id=customer_id,
            conversation_history=st.session_state.conversation_history,
            on_tool_call=on_tool_call,
            on_tool_result=on_tool_result,
        )

        status.update(
            label=f"Done — {response.turns} turn(s), {len(response.tool_calls)} tool call(s), {response.total_latency_ms:.0f}ms",
            state="complete",
            expanded=False,
        )

        st.markdown(response.content)

        # Show tool calls
        if response.tool_calls:
            with st.expander(f"🔧 Tools called: {len(response.tool_calls)}"):
                for tc in response.tool_calls:
                    st.markdown(f"**{tc.name}** ({tc.duration_ms:.0f}ms)")
                    col1, col2 = st.columns(2)
                    with col1:
                        st.json(tc.arguments)
                    with col2:
                        result_data = tc.result.get("data", tc.result)
                        st.json(result_data if isinstance(result_data, dict) else {"result": result_data})
                    tool_calls_display.append({
                        "name": tc.name, "arguments": tc.arguments,
                        "result": tc.result, "duration_ms": tc.duration_ms
                    })

    # Save to history
    st.session_state.messages.append({
        "role": "assistant",
        "content": response.content,
        "tool_calls": tool_calls_display,
    })
    st.session_state.conversation_history.append({"role": "user", "content": query})
    st.session_state.conversation_history.append({"role": "assistant", "content": response.content})
