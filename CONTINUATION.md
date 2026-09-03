# CONTINUATION.md

Read this first. Full detail in `PROJECT_CONTEXT.md`.

## Current State

Backend Phases 1–5 are real and working, integrated with `core/matcher.py` for multi-candidate detection. Throughput metrics are live in `seed_and_evaluate.py`.
The REST API (FastAPI) and the Phase 6 Next.js dashboard exist and are functional.
Database is migrated up to 006.

## Last Verified — 2026-09-03

- `cd backend && .venv/bin/python -m pytest -q` -> **59 passed**
- The live evaluation runs properly against `vulcan-postgres` and tracks records/sec.
- Real Gemini successfully auto-approves routine operational delays based on the updated prompt guidance.

## Current Task

Audit remediation (completed).

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

## Verified Live Reality

1. **Frontend exists and is solid:** The `frontend/` directory is built with Next.js, uses `tsc --noEmit` and `eslint` cleanly, and follows the visual design system.
2. **Database guarantees work:** The migration sequence (001 to 006) ensures invariants, and the `numeric_variance` cross-checks keep the AI honest.
3. **AI safety is proven, and AI usefulness is demonstrated:** Gemini correctly flags unresolvable records (HITL) and successfully auto-approves safe operational delays (missing UTR / delayed webhooks).
4. **Matcher is active:** `core/matcher.py` is successfully integrated in the live production pipeline to automatically catch multi-candidate ambiguous credits.
5. **Throughput metrics added:** `time.monotonic()` properly tracks the records/sec for the benchmark script.
6. **Robust test suite:** 59 tests cover edge cases (idempotency, concurrency, HITL workflow).
7. **Git history initialized:** The `pre-audit baseline` commit was made to track project state.

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
