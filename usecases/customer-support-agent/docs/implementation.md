# Implementation Details

This document describes the internal architecture, agent loop mechanics, tool system, memory integration, database schema, and API design of the TechGadgets.com Customer Support Agent.

## Architecture Overview

The system is composed of these major components:

1. **Streamlit UI** (`customer_support_agent.py`) — Interactive chat interface for customers
2. **FastAPI Server** (`api/server.py`) — REST API and OpenAI-compatible endpoint for programmatic access
3. **Agent Loop** (`agent/agent_loop.py`) — Core reasoning loop that orchestrates LLM calls and tool execution
4. **Tool Registry** (`tools/registry.py` + tool modules) — 40 registered tools spanning 11 categories
5. **Backend** (`backend/`) — SQLite database, Pydantic models, and seed data generation
6. **Memory** (`agent/memory.py`) — long-term per-customer memory in RediSearch, with embeddings from LiteLLM
7. **Parallel executor** (`agent/tool_executor.py`) — runs a batch of tool calls concurrently (thread pool or KubeRay)

```
┌─────────────────────────────────────────────────────────────┐
│                        Entry Points                          │
│  ┌──────────────┐              ┌──────────────────────┐     │
│  │  Streamlit   │              │   FastAPI Server     │     │
│  │  (port 8501) │              │   (port 8502)        │     │
│  └──────┬───────┘              └──────────┬───────────┘     │
│         │                                  │                 │
│         └──────────────┬───────────────────┘                 │
│                        v                                     │
│              ┌─────────────────┐                             │
│              │   Agent Loop    │                             │
│              │ (run_agent())   │                             │
│              └───────┬─────────┘                             │
│                      │                                       │
│         ┌────────────┼────────────┐                          │
│         v            v            v                          │
│  ┌─────────────┐ ┌────────┐ ┌──────────┐                   │
│  │ Tool Parser │ │  LLM   │ │  Prompt  │                   │
│  │             │ │ Client │ │  Builder │                   │
│  └──────┬──────┘ └────────┘ └──────────┘                   │
│         │                                                    │
│         v                                                    │
│  ┌─────────────────┐                                        │
│  │  Tool Registry   │                                        │
│  │  (40+ tools)     │                                        │
│  └────────┬─────────┘                                        │
│           │                                                  │
│     ┌─────┼──────┐                                           │
│     v     v      v                                           │
│  ┌──────┐ ┌──────────┐ ┌────────┐                           │
│  │SQLite│ │ Memory   │ │Business│                           │
│  │  DB  │ │(RediSearch│ │ Logic  │                           │
│  │      │ │+LiteLLM) │ │        │                           │
│  └──────┘ └──────────┘ └────────┘                           │
└─────────────────────────────────────────────────────────────┘
```

## Agent Loop

The agent loop is the core reasoning engine. It follows a synchronous loop pattern:

### Flow

```
User Query
    │
    v
Build System Prompt (tools + customer context + memory)
    │
    v
┌──────────────────────────────────────────────┐
│  LOOP (max 50 turns)                         │
│                                              │
│  1. Send messages to LLM                     │
│  2. Receive response text                    │
│  3. Parse response for <tool_call> tags      │
│  4. If NO tool call → return final text      │
│  5. If tool call found:                      │
│     a. Execute tool via registry             │
│     b. Format result as <tool_result> XML    │
│     c. Append assistant + tool_result msgs   │
│     d. Continue loop                         │
└──────────────────────────────────────────────┘
    │
    v
AgentResponse (content, tool_calls[], turns, tokens, latency)
```

### Key Implementation Details

**File:** `src/agent/agent_loop.py`

- Uses the **OpenAI Python SDK** (`openai.OpenAI`) pointed at a configurable base URL
- Supports both synchronous (`run_agent()`) and streaming (`run_agent_streaming()`) modes
- Each turn is a standard `chat.completions.create()` call with `max_tokens=1500`, `temperature=0.3`
- Tool call results are injected back as `user` messages with `<tool_result>` XML wrapping
- Callbacks (`on_tool_call`, `on_tool_result`, `on_thinking`) allow real-time UI updates
- The loop caps at `MAX_AGENT_TURNS=50` to prevent runaway loops

### Response Dataclass

```python
@dataclass
class AgentResponse:
    content: str                          # Final text response to user
    tool_calls: List[ToolCallRecord]      # All tool calls made (name, args, result, duration)
    turns: int                            # Number of LLM calls made
    total_tokens: int                     # Cumulative token usage
    total_latency_ms: float               # Wall-clock time
```

## Tool Call Parsing

The agent uses **text-based tool calling** rather than native function calling. The LLM is instructed (via the system prompt) to emit tool calls in a specific XML format. The parser (`agent/tool_parser.py`) extracts these from the raw response.

### Supported Formats

1. **XML-style (primary):**
   ```
   <tool_call>
   {"name": "get_order_details", "arguments": {"order_id": "ORD-2024-000013"}}
   </tool_call>
   ```

2. **XML with code block:**
   ```
   <tool_call>
   ```json
   {"name": "search_products", "arguments": {"query": "gaming laptop"}}
   ```
   </tool_call>
   ```

3. **Text-style (fallback):**
   ```
   TOOL_CALL: get_order_details(order_id="ORD-2024-000013")
   ```

4. **Bare JSON (emergency fallback):**
   ```
   {"name": "check_stock", "arguments": {"product_id": "PROD-001"}}
   ```

The parser returns a `ParsedResponse` dataclass:

```python
@dataclass
class ParsedResponse:
    has_tool_call: bool          # Whether a tool call was detected
    tool_name: Optional[str]     # Extracted tool name
    tool_args: Dict[str, Any]    # Parsed arguments
    text_before: str             # Text preceding the tool call
    text_after: str              # Text following the tool call
    raw_content: str             # Original LLM output
```

### Tool Result Injection

After execution, results are formatted and injected as the next user message:

```xml
<tool_result>
{
  "success": true,
  "data": { ... }
}
</tool_result>
```

## Tool System

### Registry Pattern

Tools are registered using a decorator pattern defined in `tools/registry.py`:

```python
@tool(
    name="get_order_details",
    description="Look up full order details by order ID.",
    parameters={
        "type": "object",
        "properties": {
            "order_id": {"type": "string", "description": "The order ID (e.g. ORD-2024-000123)"}
        },
        "required": ["order_id"]
    }
)
def get_order_details(order_id: str) -> dict:
    # Implementation...
```

The registry provides:

- `@tool(name, description, parameters)` — Decorator to register a function
- `list_tools()` — Returns all tool metadata (for API and system prompt)
- `execute_tool(name, arguments)` — Dispatches a call by name with error handling
- `format_tools_for_prompt()` — Generates tool documentation for the system prompt
- `import_all_tools()` — Triggers module imports to run all `@tool` decorators

### Tool Execution Flow

```
execute_tool(name, arguments)
    │
    ├─ Tool not found → {"error": "Unknown tool: ...", "available_tools": [...]}
    │
    ├─ TypeError (bad args) → {"error": "Invalid arguments for ...: ..."}
    │
    ├─ Exception → {"error": "Tool execution failed: ..."}
    │
    └─ Success → {"success": true, "data": <return value>}
```

### Complete Tool Inventory (40 Tools)

| Module | Tools |
|--------|-------|
| `order_tools.py` | `get_order_details`, `get_orders_by_email`, `track_shipment`, `list_customer_orders` |
| `refund_tools.py` | `check_return_eligibility`, `initiate_return`, `process_refund`, `get_return_status` |
| `product_tools.py` | `search_products`, `get_product_details`, `compare_products`, `get_product_reviews` |
| `account_tools.py` | `authenticate_customer`, `get_customer_profile`, `update_customer_address`, `get_loyalty_points`, `reset_password` |
| `shipping_tools.py` | `get_shipping_rates`, `estimate_delivery_date`, `update_shipping_address` |
| `warranty_tools.py` | `check_warranty_status`, `file_warranty_claim` |
| `promotion_tools.py` | `validate_coupon`, `get_active_promotions`, `apply_price_match`, `get_loyalty_rewards` |
| `inventory_tools.py` | `check_stock`, `get_restock_estimate`, `check_store_availability`, `list_product_inventory` |
| `knowledge_base_tools.py` | `search_knowledge_base`, `get_faq_article`, `get_policy_info`, `get_troubleshooting_guide` |
| `escalation_tools.py` | `transfer_to_human_agent`, `create_support_ticket`, `get_ticket_status` |
| `communication_tools.py` | `send_order_confirmation_email`, `send_return_label`, `schedule_callback` |

## Memory System

**File:** `src/agent/memory.py`

Long-term memory stores short interaction summaries per customer as vectors in
the shared Redis Stack (`redis-stack-server.redis`, RediSearch) and retrieves the
most relevant ones to prime the system prompt. Embeddings are produced by LiteLLM
through its OpenAI-compatible `/v1/embeddings` endpoint, so there is no local
embedding model in the image.

### Components

- **Redis Stack (RediSearch)** — HASH documents under the prefix `csa:mem:`,
  indexed by `MEMORY_INDEX` (`csa_mem_idx`) with the schema:
  `customer_id` (TAG), `text` (TEXT), `ts` (NUMERIC), `embedding`
  (VECTOR, HNSW, FLOAT32, `EMBEDDING_DIMS`, COSINE).
- **Embedding model** — `EMBEDDING_MODEL` (default `bge-base-en`, the AI
  Solutions catalog name for `BAAI/bge-base-en-v1.5`, 768 dims), called with the
  same `LLM_BASE_URL` / `LLM_API_KEY` as the chat model. It must be added to
  `llm_services_autodeploy_models` to be served.

### How Memory Works

1. **Startup** — `init_memory()` (called from the FastAPI startup hook) connects
   with `REDIS_URL`, pings, and creates the index if it does not exist. If
   `MEMORY_ENABLED=false` or any step fails, memory is marked unavailable.
2. **Recall** — before each `/api/v1/chat` (and `/api/v1/conversation`) call,
   `search_memory(customer_id, message)` embeds the message and runs a KNN query
   (`MEMORY_TOP_K`, default 4) filtered by `@customer_id:{...}`, so one customer
   never recalls another's history. The hits are merged with any caller-supplied
   `memory_context` and injected into the system prompt.
3. **Store** — after the agent answers, `add_memory()` stores
   `User asked: … | Agent answered: …` (each side truncated to 400 chars) with its
   embedding and a timestamp.
4. **Fail-open** — every Redis or embedding error is logged as `[memory] …` and
   degrades to "no memory" instead of failing the request. `GET /api/v1/health`
   reports `memory_enabled` (index available); embedding failures appear only in
   the logs.

### Configuration

```
MEMORY_ENABLED=true
REDIS_URL=redis://default:<password>@redis-stack-server.redis.svc.cluster.local:6379
MEMORY_INDEX=csa_mem_idx
MEMORY_TOP_K=4
EMBEDDING_MODEL=bge-base-en
EMBEDDING_DIMS=768
```

`EMBEDDING_DIMS` must match the embedding model. After switching to a model with
a different dimension, drop the existing index (`FT.DROPINDEX csa_mem_idx DD`)
so it is recreated.

In Docker Compose, a local `redis-stack` container provides RediSearch.

## Parallel Tool Execution

**File:** `src/agent/tool_executor.py`

When the model emits several `<tool_call>` blocks in one turn,
`parse_all_tool_calls()` returns them all and `execute_tools()` runs the batch
concurrently (up to `TOOL_MAX_PARALLEL`), returning results in order plus the
backend used. `TOOL_EXECUTOR=local` uses a `ThreadPoolExecutor`;
`TOOL_EXECUTOR=ray` submits each call as a Ray task to `RAY_ADDRESS` (the
agent-toolkit KubeRay cluster). Because the SQLite DB exists only in the agent
pod, each Ray task calls back to `SELF_API_URL/api/v1/tools/{name}`. If Ray
cannot be reached, execution falls back to the local thread pool.

## Database Schema

The agent uses **SQLite** with auto-initialization and seed data generation. The database is created at startup via `init_db()` and populated via `seed_all()`.

### Tables

| Table | Purpose | Key Fields |
|-------|---------|------------|
| `customers` | Customer profiles | `id`, `email`, `first_name`, `last_name`, `phone`, `shipping_address` (JSON), `loyalty_tier`, `loyalty_points` |
| `products` | Product catalog | `id`, `name`, `category`, `brand`, `base_price`, `description`, `specs` (JSON), `rating_avg` |
| `product_variants` | SKU-level variants | `id`, `product_id`, `sku`, `options` (JSON), `price`, `stock_quantity`, `is_available` |
| `orders` | Customer orders | `id`, `customer_id`, `status`, `order_date`, `total`, `tracking_number`, `carrier`, `estimated_delivery` |
| `order_items` | Line items in orders | `id`, `order_id`, `product_id`, `product_name`, `quantity`, `unit_price`, `item_status` |
| `shipping_events` | Tracking event timeline | `id`, `order_id`, `tracking_number`, `event_type`, `location`, `timestamp` |
| `returns` | Return requests | `id`, `order_id`, `customer_id`, `status`, `reason`, `rma_number`, `refund_amount` |
| `support_tickets` | Escalation tickets | `id`, `customer_id`, `subject`, `status`, `priority`, `category`, `assigned_to` |
| `ticket_notes` | Notes on tickets | `id`, `ticket_id`, `author`, `note`, `created_at` |
| `knowledge_base` | FAQ/policy articles | `id`, `title`, `content`, `category`, `tags` (JSON) |
| `promotions` | Active coupons/deals | `id`, `code`, `discount_type`, `discount_value`, `valid_from`, `valid_until` |
| `warranty_claims` | Warranty claims | `id`, `order_id`, `item_id`, `customer_id`, `issue_description`, `status` |
| `product_reviews` | Customer reviews | `id`, `product_id`, `customer_id`, `rating`, `title`, `content`, `verified_purchase` |

### Entity Relationships

```
customers ─┬─── orders ──── order_items
            │        │
            │        ├──── shipping_events
            │        │
            │        ├──── returns
            │        │
            │        └──── warranty_claims
            │
            ├─── support_tickets ──── ticket_notes
            │
            └─── product_reviews

products ──── product_variants
```

### Indexes

The schema includes indexes on high-cardinality foreign keys: `orders(customer_id)`, `orders(status)`, `order_items(order_id)`, `shipping_events(order_id)`, `returns(order_id)`, `support_tickets(customer_id)`, `products(category)`, `product_variants(product_id)`, `product_reviews(product_id)`, `customers(email)`.

### Business Rules (Configured in `config.py`)

| Rule | Value |
|------|-------|
| Return window | 30 days |
| Standard warranty | 1 year |
| Loyalty points per dollar | 10 |

## API Design

### REST API (`/api/v1/`)

The FastAPI server provides a dedicated REST API for direct agent interaction:

**POST `/api/v1/chat`** — Single-turn agent interaction

Request:
```json
{
  "message": "Where is my order ORD-2024-000013?",
  "customer_id": "CUST-0001",
  "conversation_history": [],
  "memory_context": ""
}
```

Response:
```json
{
  "response": "Your order ORD-2024-000013 is currently...",
  "tool_calls": [
    {
      "name": "get_order_details",
      "arguments": {"order_id": "ORD-2024-000013"},
      "result": {"success": true, "data": {...}},
      "duration_ms": 12.5
    }
  ],
  "metadata": {
    "turns": 2,
    "total_tokens": 1450,
    "latency_ms": 2340.5,
    "tools_called": 1
  }
}
```

**POST `/api/v1/conversation`** — Multi-turn (returns updated history for subsequent calls)

**POST `/api/v1/tools/{tool_name}`** — Direct tool execution (bypass agent loop)

**GET `/api/v1/tools`** — List all tools and their parameter schemas

### OpenAI-Compatible Endpoint (`/v1/chat/completions`)

Allows external benchmarks and clients to interact with the agent using the standard OpenAI chat completions protocol. Supports both streaming (SSE) and non-streaming modes.

Request:
```json
{
  "model": "techgadgets-support-agent",
  "messages": [
    {"role": "system", "content": "Customer: CUST-0001"},
    {"role": "user", "content": "What gaming laptops do you have?"}
  ],
  "stream": false
}
```

Response (non-streaming):
```json
{
  "id": "chatcmpl-abc123",
  "object": "chat.completion",
  "model": "qwen2-5-coder-14b",
  "choices": [
    {
      "index": 0,
      "message": {"role": "assistant", "content": "..."},
      "finish_reason": "stop"
    }
  ],
  "usage": {"prompt_tokens": 500, "completion_tokens": 300, "total_tokens": 800},
  "agent_trace": {
    "framework": "custom_agent_loop",
    "llm_calls": 2,
    "tool_calls_made": [...],
    "total_latency_ms": 3200.0
  }
}
```

The `agent_trace` field is an extension that exposes tool-calling metadata for benchmark evaluation.

### Discovery Endpoints

| Endpoint | Purpose |
|----------|---------|
| `GET /v1/entities` | Returns agent entity info (for unified benchmark auto-detection) |
| `GET /v1/models` | OpenAI-compatible model listing |
| `GET /meta` | Framework metadata, capabilities, and endpoint map |

## Configuration via Environment Variables

All configuration is centralized in `src/config.py` and read from environment variables at import time:

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `LLM_BASE_URL` | `str` | `""` (chart: `http://litellm.litellm.svc.cluster.local:4000/v1`) | LiteLLM OpenAI-compatible base URL |
| `LLM_API_KEY` | `str` | `""` | LiteLLM key |
| `LLM_MODEL` | `str` | `qwen2-5-coder-14b` | Chat model registered in LiteLLM |
| `DB_PATH` | `str` | `src/techgadgets.db` (image/chart: `/app/data/techgadgets.db`) | Path to SQLite database file |
| `MEMORY_ENABLED` | `bool` | `true` | Enable long-term memory |
| `REDIS_URL` | `str` | `redis://redis-stack-server.redis.svc.cluster.local:6379/0` | RediSearch URL (must include credentials on AI Solutions) |
| `MEMORY_INDEX` | `str` | `csa_mem_idx` | RediSearch index name |
| `MEMORY_TOP_K` | `int` | `4` | Memories recalled per query |
| `EMBEDDING_MODEL` | `str` | `bge-base-en` | Embedding model served by LiteLLM |
| `EMBEDDING_DIMS` | `int` | `768` | Embedding dimension |
| `TOOL_EXECUTOR` | `str` | `local` | `local` or `ray` |
| `TOOL_MAX_PARALLEL` | `int` | `8` | Max concurrent tool calls |
| `RAY_ADDRESS` | `str` | `ray://ray-cluster-head-svc.ray-system:10001` | Ray client address |
| `SELF_API_URL` | `str` | `http://customer-support-agent.customer-support-agent.svc.cluster.local:8502` | Callback URL for Ray tasks |
| `API_PORT` | `int` | `8502` | FastAPI server port |
| `STREAMLIT_PORT` | `int` | `8501` | Streamlit UI port |

The deploy script also accepts `OPENAI_BASE_URL` / `OPENAI_API_KEY` /
`MODEL_NAME` and maps them onto the `LLM_*` variables.

### Non-Configurable Constants (set in `config.py`)

| Constant | Value | Description |
|----------|-------|-------------|
| `LLM_VERIFY_SSL` | `False` | Defined but not currently used — the OpenAI client verifies TLS normally, so trust the gateway CA for `https://litellm.<base>` |
| `MAX_AGENT_TURNS` | `50` | Maximum loop iterations |
| `MAX_TOKENS_PER_RESPONSE` | `1500` | LLM max_tokens per call |
| `TEMPERATURE` | `0.3` | LLM temperature |
| `RETURN_WINDOW_DAYS` | `30` | Business rule: return eligibility |
| `WARRANTY_STANDARD_YEARS` | `1` | Business rule: standard warranty |
| `LOYALTY_POINTS_PER_DOLLAR` | `10` | Business rule: points accrual rate |

## System Prompt Construction

The system prompt (`agent/prompt_builder.py`) is assembled dynamically at each conversation start:

1. **Role description** — Professional customer support agent persona
2. **Tool definitions** — Auto-generated from the registry (names, descriptions, parameters)
3. **Tool calling format** — Instructions for the `<tool_call>` XML format
4. **Rules** — 11 behavioral rules (authentication, tool usage, escalation, transparency)
5. **Customer context** — Customer ID (or "not authenticated yet") and memory context

The tool definitions section is regenerated from the live registry each time, ensuring the prompt always matches the currently registered tools.
