
🏗️ Vulcan Ledger: System Architecture

Vulcan Ledger is designed around a strictly segregated, high-throughput pipeline. It uses Next.js for the frontend, FastAPI for the backend, PostgreSQL for immutable state, and LangGraph (Gemini) for anomaly resolution.

🔄 The Reconciliation Pipeline

The system processes settlements via a highly optimized 4-step pipeline:

1. Ingestion & Parsing

Settlement files (CSV/JSON) are parsed into memory. To prevent silent failures, malformed rows are preserved and automatically flagged as PENDING_HITL_REVIEW rather than being dropped.

2. The Deterministic Fast-Path

Before any LLM API is invoked, a deterministic Python/SQL engine evaluates the batch. If a Bank record and a Gateway record match perfectly on amount, transaction_id, and status, it is instantly resolved. This reduces unnecessary LLM latency to zero.

3. Chunked AI Evaluation (LangGraph)

Only actual exceptions (monetary variances, missing references, status clashes) are passed to the AI.

To hit sub-30-second throughput, records are batched into chunks (e.g., 10-15 records per prompt).
These chunks are evaluated concurrently using Python's asyncio.gather, maximizing network bandwidth and model utilization.
4. Bulk Persistence & Live State

Database bottlenecks are eliminated using asynchronous SQLAlchemy bulk inserts (session.add_all()). After every chunk is evaluated, the intermediate state is committed to the BatchRegistry. This allows the Next.js frontend to poll the /summary endpoint and render a live, incrementally ticking progress bar.

🗄️ Core Data Models
BatchRegistry: Tracks the macro-state of an upload, including total_records and resolved_count for real-time UI polling.
Ledger: The operational state of the settlements, storing the gateway expectations vs. bank realities, including computed variance_amounts.
AuditEvents: An append-only, immutable record. Every automated rule match or HITL decision is recorded here with a cryptographic fingerprint.
