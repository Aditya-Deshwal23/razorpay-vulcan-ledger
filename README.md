# 🌋 Vulcan Ledger
**The AI Finance Controller for the Modern Fast-Path.**

Vulcan Ledger is a high-throughput, AI-driven reconciliation engine built for **Razorpay Buildathon (Track 04: AI Finance Controller)**. It closes the finance-ops loop by automatically reconciling multi-source settlements against gateway expectations, routing only genuine anomalies to a human-in-the-loop (HITL) controller.

## 🎯 Hitting the Track 04 Bar
The 2026 builder consensus is clear: verification capacity is the bottleneck. Here is how Vulcan Ledger solves it:
* **⚡ Throughput (Sub-30s Execution):** By leveraging concurrent chunking (`asyncio.gather`) and bulk SQLAlchemy database inserts, Vulcan Ledger processes a 60-record settlement batch end-to-end in under 30 seconds.
* **🎯 Measured Accuracy:** We do not waste LLM latency on perfect data. A "Deterministic Fast-Path" instantly clears perfect matches (0ms latency), while a LangGraph-powered AI agent exclusively analyzes actual anomalies in packed chunks.
* **🔎 Honest Exceptions:** Anomalies are pushed to a Controller Review Queue. The UI exposes the exact monetary variance (e.g., `-₹25.50 Variance`) and the deterministic mismatch reason (e.g., `Status/Data mismatch`), keeping human boundaries strictly separated from AI-generated models.

## ✨ Core Features
- **Dynamic Control Tower:** Real-time polling metrics and progress bars track the active AI run live.
- **Immutable Audit Trail:** Every AI or Controller decision creates an immutable ledger row, exportable instantly as `audited_{original_filename}.csv`.
- **Intelligent Fallbacks:** Seamless handling of missing CSV rows, UI UUID masking, and complex edge cases like Refund vs. Capture clashes.

## 🚀 Quick Start (Docker)
Vulcan Ledger is fully containerized. To spin up the stack:

```bash
docker compose down -v --remove-orphans
docker compose up -d --build

Access the application at: http://localhost:3000

For a detailed breakdown, please see the /docs directory.
