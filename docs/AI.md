# AI workspace: read-only assistant

Status: **implemented and tested** on 2026-10-19. The default provider is local,
deterministic and offline; an OpenAI-compatible provider is optional. A provider
outage never affects sales, stock, purchasing or accounting.

---

## 1. What it is

`/ai` is an administrator/sales workspace that answers questions about the
business using a small set of **read-only** tools:

| Tool | Permission | Data |
|---|---|---|
| `business_overview` | `dashboard.view` | Month sales, margin, receivables, payables, stock value, low stock |
| `search_products` | `catalog.view` | Name/SKU/barcode search |
| `product_stock` | `inventory.view` | On-hand, reserved and available by warehouse for one product |
| `low_stock` | `inventory.view` | Products at or below reorder level |
| `sales_summary` | `sales.view` | Sales, payments and top products for a period |
| `receivables` | `customers.view` | Customer balances and ageing buckets |
| `payables` | `suppliers.view` | Supplier balances and ageing buckets |
| `recent_invoices` | `sales.view` | Most recent sales invoices |

There is **no write tool**. The registry is a fixed dictionary; an unknown tool
name is refused before any handler runs. Every tool re-checks the current user's
permission server-side, even if the UI hid it.

The assistant can read only what the signed-in user is allowed to read. It
cannot create, edit, delete, post, pay, receive stock or change a price.

---

## 2. Two providers, one safety model

### Local (default, no network)

`AI_PROVIDER=local` uses a deterministic keyword router and the same read-only
tools. It is intentionally simple: it recognises overview, low-stock, sales,
receivables, payables, recent-invoice and product/stock questions in Arabic,
French and English. A question it does not recognise falls back to the business
overview (or a product search when words remain after stop-word removal).

It does not require an API key, an internet connection or a third-party service.
It is the safe default for an Algerian shop with intermittent connectivity.

### OpenAI-compatible (optional)

Set:

```env
AI_PROVIDER=openai
AI_BASE_URL=https://api.openai.com/v1
AI_API_KEY=...
AI_MODEL=gpt-4o-mini
AI_TIMEOUT_SECONDS=20
```

Any OpenAI-compatible gateway works. The client sends a system prompt, the
question and tool results. Tool results are inserted with the `tool` role,
never as system instructions, and are truncated. The system prompt tells the
model to treat tool output as **untrusted data** and ignore instructions inside
it.

The model may request a tool by returning JSON such as
`{"tool": "low_stock", "args": {}}`. The server validates the tool name and
arguments, re-checks permissions, executes the read-only handler and returns the
result. This repeats for at most `AI_MAX_TOOL_ROUNDS` (default 3), after which a
final answer is requested.

If the provider is not configured, times out, returns an error or returns
malformed data, the workspace falls back to the local assistant. The exchange is
stored with status `unavailable` and the fallback answer is shown. Core business
operations are untouched.

### Keys

`AI_API_KEY` is environment-only. It is never written to the database, rendered
on a page, included in backups created from the binary (it is not a database
column) or logged by the AI service. The system status page shows only whether a
provider is configured, not the key.

---

## 3. Audit and privacy

Every question is stored in the append-only `ai_interactions` table:

* `created_at`, `user_id`, `question`, `answer`;
* `provider` (`local`/`openai`), `model`;
* `status` (`ok`/`unavailable`/`refused`/`error`);
* `tool_calls` (JSON names, success flags and short summaries — not full result
  sets), `latency_ms`, `error_key`.

An `ai_query` audit event is also written through the normal audit log. The
table is not edited by the application. Deleting an interaction is a database
operator action, not a UI feature.

Questions and answers are business data. If an external provider is used, the
question text, the conversation and the tool results sent to the provider leave
the server. The operator must decide whether that is acceptable for their data.
The local provider sends nothing.

Do not paste passwords, API keys or personal data into the question box. There
is no automatic secret redaction.

---

## 4. Configuration

| Variable | Default | Meaning |
|---|---|---|
| `FEATURE_AI` | `1` | Global feature switch |
| `AI_PROVIDER` | `local` | `local` or an external OpenAI-compatible provider |
| `AI_BASE_URL` | `https://api.openai.com/v1` | Provider base URL |
| `AI_API_KEY` | empty | Provider key; empty means external provider is not configured |
| `AI_MODEL` | `gpt-4o-mini` | Model name sent to the provider |
| `AI_TIMEOUT_SECONDS` | `20` | Per-request network timeout |
| `AI_MAX_QUESTION_CHARS` | `1000` | Questions are truncated to this length |
| `AI_MAX_TOOL_ROUNDS` | `3` | Maximum provider tool rounds |
| `AI_RATE_LIMIT_PER_HOUR` | `30` | Per-user POST limit on `/ai/ask` |

The per-business switch is `BusinessSettings.enable_ai`; it is editable in
**Settings**. Disabling it hides the workspace and redirects `/ai`, but changes
no business data. The feature is also hidden when `FEATURE_AI=0`.

---

## 5. UI and accessibility

The workspace is Arabic-first and RTL, with French and English catalogues. It
uses the existing form macros, table styles and badges; no inline scripts or
styles, so the strict CSP is unchanged. The question form is a normal WTForms
form with CSRF. History lists the signed-in user's own interactions only.
Provider status is shown as local/external/not configured.

---

## 6. Limitations

* The local assistant is **not** a general-purpose language model. It routes a
  limited set of business questions; unknown questions fall back to an overview.
* Tool output is truncated and answers are bounded; a very large result set is
  summarised, not exported.
* The assistant does not answer legal, tax or credit-risk questions and must not
  be used as the sole basis for a financial decision.
* There is no conversation memory across page loads; every question is
  independent.
* There is no automatic PII/secret redaction.
* An external provider's own retention, residency and training policies apply;
  the application cannot control them.
* There is no write approval workflow because there are no write tools.

These limits are deliberate. A read-only assistant that cannot touch the ledger
is safer than an autonomous agent with database credentials.
