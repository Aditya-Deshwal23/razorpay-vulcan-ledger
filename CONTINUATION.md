# CONTINUATION.md

Read this first. Full detail in `PROJECT_CONTEXT.md`.

## Current State

Backend Phases 1–5 are real and working: async SQLAlchemy layer over Postgres
(`vulcan-postgres`), UTR parser, Decimal-only deterministic rules engine, sandboxed
LangGraph + Gemini exception agent with Postgres checkpointing and `interrupt()` HITL, and a
52-record evaluation runner that persists through the real pipeline.

Not started: **any HTTP surface** (no `main.py`, no routes — FastAPI is in requirements and
unused) and **the frontend** (`frontend/` is an empty directory).

## Last Verified — 2026-08-26

- `cd backend && .venv/bin/python -m pytest -q` -> **30 passed in 0.74s**
- `.venv/bin/python -m scripts.verify_phase2` -> **7/7 OK**
- Live DB: 244 settlements, 244 recon rows — 240 `DETERMINISTIC_MATCH`,
  4 `PENDING_HITL_REVIEW`, **0 `AI_RESOLVED`**.
- The only real-Gemini run (`B2E7EB3C`) was 48 `DETERMINISTIC_MATCH` + 4
  `PENDING_HITL_REVIEW`. The historical "48/3/1" split is the **fake test LLM's** output,
  not the real agent's. Read demo metrics from Postgres.

## Current Task

FastAPI app + read-only reconciliation API: `backend/main.py` and `backend/api/`.
Batch summary with match rate, exception list (variance, reason, confidence), settlement
detail. Aggregate from `t_reconciliation_ledger` — not from `exceptions_manifest.json`.
Touches only new files. No schema change. No edits to verified modules.

## Next Task

HITL resume endpoint. Needs a schema change first: persist the LangGraph `thread_id` on the
recon ledger and add human-decision columns, so a `PENDING_HITL_REVIEW` row can actually
advance via `Command(resume=...)`. Decide on a migration mechanism (no Alembic today).
Then Phase 6 Next.js dashboard.

## Important Decisions

- Currency is `decimal.Decimal` built from strings, always. `_ensure_decimal`
  (`core/rules_engine.py`) raises `TypeError` on any float, even `1000.0`.
- `MATCH_TOLERANCE = Decimal("0.02")`, `core/rules_engine.py:28`. Never widen it.
- A deterministic match requires **balanced math AND an extracted UTR**. A coincidental
  balance with no UTR still goes to the agent. Honesty over match rate.
- Never pass `temperature` (or any sampling param) to `ChatGoogleGenerativeAI`.
  langchain-google-genai 4.3.5 nulls it itself for Gemini 3+; passing `0.7` is what broke.
- The LLM gets one sanitized string and returns one validated Pydantic object — never a row,
  a connection string, or SQL. Variance crosses that boundary as a **string**.
- `build_graph(llm, checkpointer)` keeps DI so tests run with a fake LLM and no API key.
- Failures degrade to "ask a human", never to a fabricated success.
- Idempotency lives in the database (`ON CONFLICT`, `UNIQUE`, `SELECT ... FOR UPDATE`).

## Known Issues

1. **No git commits at all** — `master` has zero commits, everything untracked. No recovery
   point for Phases 1–5. Commit before further work.
2. `unique_bank_entry` doesn't dedupe NULL UTRs (Postgres NULLs are distinct in UNIQUE), so
   UTR-less credits re-import forever — 24 such rows for 3 distinct shapes. Needs
   `NULLS NOT DISTINCT` or a sentinel key.
3. The test suite overwrites the real `exceptions_manifest.json`; the file on disk is
   fake-LLM output. Not evidence.
4. `numeric_variance` is stored from the **LLM's** self-reported variance for AI/HITL rows,
   not the deterministic one. An LLM number in a monetary ledger column.
5. HITL cannot complete outside tests: no resume endpoint, no persisted `thread_id`, no
   ledger column for the decision.
6. Test cleanup leaks 4 NULL-UTR bank rows per run (`LIKE` never matches NULL).
7. Stale partial runs in the DB (`C57E9121`, `59E9CC02`, `6FA4D090`, `83527E54`): 48 rows
   each, all 4 edge records failed.
8. `MAX_LLM_ATTEMPTS=2` retries the identical prompt with no backoff and no error fed back —
   a retry, not true self-correction. Also no per-attempt timeout in `graph.py`.
9. `settings.py` defaults `gemini_model` to `gemini-3.7-flash`; `.env` uses
   `gemini-3.6-flash`.
10. No Alembic. `ddl.sql` is applied by hand.

## Important Commands

```bash
cd /Users/adityadeshwal/razorpay-vulcan-ledger/backend    # required cwd — imports are rootless

.venv/bin/python -m pytest -q                    # 30 tests, no API key needed
.venv/bin/python -m scripts.verify_phase2        # live DB smoke test
.venv/bin/python -m evaluation.seed_and_evaluate # REAL Gemini, billed, writes rows

docker start vulcan-postgres
docker exec vulcan-postgres psql -U postgres -d razorpay_vulcan_ledger \
  -c "SELECT recon_state, COUNT(*) FROM t_reconciliation_ledger GROUP BY 1;"
```
