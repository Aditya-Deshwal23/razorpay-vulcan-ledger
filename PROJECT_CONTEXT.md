# PROJECT_CONTEXT.md — Razorpay Vulcan Ledger

Engineering reference for the repository at `/Users/adityadeshwal/razorpay-vulcan-ledger`.
Verified against the filesystem, the live Postgres instance, and a full test run on
2026-08-26. Where historical notes disagreed with the repository, the repository wins and
the discrepancy is recorded in "Known bugs / gotchas".

## 1. Purpose

Razorpay AI Buildathon 2026, Track 04 (AI Finance Controller). Reconciles Razorpay
settlement data against bank statement credits. Deterministic Decimal math resolves the
easy majority; a sandboxed LangGraph + Gemini agent classifies only what deterministic
logic could not resolve; a human reviewer handles what the agent will not commit to.
Every decision lands in an auditable ledger row with a tamper-evident hash.

AI is explicitly not the reconciliation engine. It is the exception handler.

## 2. Architecture

Backend: Python 3.14.6, FastAPI 0.141.1 (**in requirements, not yet used**),
SQLAlchemy 2.0.52 async + asyncpg, Pydantic 2.13.4, LangGraph 1.2.11,
langgraph-checkpoint-postgres 3.1.2, langchain-google-genai 4.3.5.

Database: PostgreSQL in Docker container `vulcan-postgres`, port 5432,
database `razorpay_vulcan_ledger`. Two connection strings on purpose:
`DATABASE_URL` (asyncpg, app ORM) and `CHECKPOINTER_DATABASE_URL` (psycopg3, LangGraph).

AI: Google Gemini, model from `GEMINI_MODEL` (currently `gemini-3.6-flash`).

Frontend: Next.js 14 + Tailwind — **planned, `frontend/` is an empty directory.**

## 3. Directory map

```
.env                     real secrets, gitignored
.env.example             12 documented keys
backend/
  config/settings.py     pydantic-settings, lru_cached get_settings()
  config/database.py     async engine, 3 ORM models, idempotent write helpers
  database/ddl.sql       hand-applied DDL (no migration tool)
  parsers/mt940_processor.py   LegacyBankParser.extract_utr()
  core/rules_engine.py   DeterministicRulesEngine.evaluate_match()
  agents/schemas.py      ExceptionClassification (LLM output contract)
  agents/graph.py        build_graph(), reconciliation_graph()
  evaluation/seed_and_evaluate.py   52-record benchmark runner
  scripts/verify_phase2.py          live DB smoke test (7 checks)
  scripts/diagnose_gemini.py        manual Gemini A/B diagnostic (billed calls)
  tests/                4 files, 30 tests
frontend/                EMPTY
exceptions_manifest.json gitignored, overwritten by the test suite
```

No `backend/main.py`, no `backend/api/`, no routers. There is no HTTP surface at all.

## 4. Database schema

Live schema matches `backend/database/ddl.sql` column-for-column (verified with `\d`).

`t_razorpay_settlements` — `internal_id` PK, `settlement_id` UNIQUE, `status`,
`gross_amount`/`fees`/`taxes`/`refunds`/`adjustments`/`net_settlement` all
`DECIMAL(15,2)`, `utr_reference`, `created_at`, `updated_at`.
`CHECK (>= 0)` on gross/fees/taxes/refunds. BEFORE UPDATE trigger
`set_updated_at_on_settlements` keeps `updated_at` honest even for raw SQL.

`t_bank_ledger` — `entry_id` PK, `bank_name`, `transaction_date`, `credit_amount`,
`raw_narration`, `extracted_utr`, `is_reconciled`.
`UNIQUE (bank_name, extracted_utr, credit_amount)` = `unique_bank_entry`.
Partial index on unreconciled rows.

`t_reconciliation_ledger` — `recon_id` PK, `settlement_id` UNIQUE + FK RESTRICT,
`bank_entry_id` FK RESTRICT nullable, `recon_state VARCHAR(30)`,
`numeric_variance DECIMAL(15,2)`, `ai_classification_reason VARCHAR(100)`,
`cryptographic_state_hash VARCHAR(64)`, `resolved_at`.
`UNIQUE(settlement_id)` makes a pipeline re-run a database-level no-op.

LangGraph's own `checkpoints`, `checkpoint_blobs`, `checkpoint_writes`,
`checkpoint_migrations` live in the same database, created by
`AsyncPostgresSaver.setup()`, deliberately not in `ddl.sql`.

`recon_state` values in use: `DETERMINISTIC_MATCH`, `AI_RESOLVED`, `PENDING_HITL_REVIEW`.
There is no state column, reviewer column, or `thread_id` column for a *completed* human
review — see Known bugs.

## 5. Data flow

```
SyntheticRecord (or, later, a real settlement + bank statement row)
  -> LegacyBankParser.extract_utr(bank_name, narration)        -> Optional[str]
  -> DeterministicRulesEngine.evaluate_match(6 Decimals)       -> ReconciliationResult
  -> get_or_create_bank_entry(...)                             -> (entry, created)
  -> settlement INSERT (flush)
  |
  |-- math balanced AND utr is not None
  |     -> recon_state = DETERMINISTIC_MATCH, no LLM call, commit
  |
  `-- otherwise: build sanitized_context string (no DB rows, no SQL, no PII)
        -> graph.ainvoke(state, thread_id="eval-<settlement_id>")
        -> ExceptionClassification
        -> "__interrupt__" present ? PENDING_HITL_REVIEW : AI_RESOLVED
        -> commit
```

The UTR condition is load-bearing: a coincidentally balanced amount with no UTR to prove
it belongs to this settlement is **not** a deterministic match. That is why the
`chargeback` and `delayed_webhook` edge cases have variance 0.00 yet still reach the agent.

## 6. API contracts

None. No FastAPI app, no routes, no request/response models exist yet. `get_db_session()`
in `config/database.py` is written as a FastAPI dependency and is currently unused.
`settings.razorpay_*` keys exist but no Razorpay SDK call or webhook verifier is
implemented anywhere.

## 7. AI / LangGraph flow

`build_graph(llm, checkpointer=None)` — dependency-injected, so tests pass a fake LLM and
`InMemorySaver`; nothing network- or DB-touching happens at import time.

Nodes: `fetch_context` (sandbox boundary, pass-through) -> `llm_reasoning` ->
conditional edge -> `human_in_the_loop` or `END`.

`llm_reasoning_node` retries `llm.ainvoke` up to `MAX_LLM_ATTEMPTS = 2`. On total failure
it returns a deterministic fallback (`UNKNOWN_UNRESOLVABLE`, `ROUTE_TO_HITL_PANEL`,
`confidence_score=0.0`) and appends to `error_log`. It never raises into the pipeline.

`human_in_the_loop_node` calls `interrupt()` with settlement_id, reason, variance and
confidence. Resume with `graph.ainvoke(Command(resume={"approved": bool}), config)` using
the same `thread_id`. Proven across two independently built graph objects sharing one real
`AsyncPostgresSaver` (`test_checkpointer_persists_across_separate_graph_instances`).

`ExceptionClassification` carries variance as `variance_str` (a string) with a
`field_validator` that rejects anything `Decimal()` cannot parse, and exposes
`.variance -> Decimal`. This keeps float representation entirely outside the process — no
JSON number ever becomes a monetary value.

Gemini configuration: `reconciliation_graph()` builds `ChatGoogleGenerativeAI(model=...,
google_api_key=...)` and passes **no** `temperature`. That is the correct form of the
historical fix: langchain-google-genai 4.3.5 sets `self.temperature = None` itself for
Gemini 3+ when temperature is not in `model_fields_set`
(`chat_models.py:2668-2671`). Passing `temperature=0.7` explicitly is what broke.
Never add a sampling parameter back to that constructor. The model name is
config-driven (`GEMINI_MODEL`) so a fallback model needs no code change.

## 8. Current phase / status

Phases 1–5 are real and verified. Phase 6 (dashboard) has **not** started —
`frontend/` is an empty directory and there is no API for it to consume. Throughput
measured at ~1.8s per non-deterministic record (including network latency to Gemini).

## 9. Test status

- `cd backend && .venv/bin/python -m pytest -q` -> **30 passed in 0.74s** (2026-08-26).
- `.venv/bin/python -m scripts.verify_phase2` -> **7/7 OK**.
Tests requiring live Postgres pass against the running `vulcan-postgres` container.
No test makes a real Gemini call; every LLM in the suite is an injected fake.

## 10. Known bugs / gotchas

1. **`unique_bank_entry` does not dedupe NULL UTRs.** Postgres treats NULLs as distinct in
   a UNIQUE constraint, so a UTR-less credit re-imports forever. Live DB currently holds 24
   NULL-UTR bank rows across only 3 distinct `(bank, amount)` shapes. Fix needs
   `NULLS NOT DISTINCT` (PG15+) or a sentinel/derived key — a schema change.
2. **The test suite writes to the real `exceptions_manifest.json`** at the project root, so
   the artifact on disk reflects the last *fake-LLM test run*, not the last real pipeline
   run. It must not be treated as evidence of anything.
3. **`numeric_variance` is populated from the LLM's self-reported variance**
   (`classification.variance`) for non-deterministic rows, not from the deterministic
   `math_result.variance`. An LLM-emitted number is being stored in a monetary ledger
   column. The deterministic variance is already computed and should be preferred.
4. **HITL is unreachable outside tests.** `PENDING_HITL_REVIEW` rows can never advance:
   there is no endpoint to deliver `Command(resume=...)`, no ledger column for the human
   decision, and no persisted `thread_id` (the `eval-<settlement_id>` convention is
   hardcoded in `seed_and_evaluate.py`).
5. **Test cleanup leaks bank rows.** `DELETE ... WHERE extracted_utr LIKE :pattern` never
   matches NULL, so 4 rows per run survive. Related to (1).
6. **Stale partial runs in the database.** Runs `C57E9121`, `59E9CC02`, `6FA4D090`,
   `83527E54` each have 48 settlements and 0 edge rows — the 4 chaotic records failed
   (Gemini-era errors) and were only recorded in a since-overwritten manifest.
7. `MAX_LLM_ATTEMPTS` retries the *identical* prompt with no backoff and without feeding
   the validation error back. It is a retry, not true self-correction — describe it
   honestly.
8. No `asyncio.wait_for` around `llm.ainvoke` in `graph.py`, despite historical notes
    claiming per-attempt timeouts. Only `diagnose_gemini.py` bounds its calls.
9. No migration tool (no Alembic). `ddl.sql` is applied by hand; ORM-declared
   `CheckConstraint` names do not exist in the live DB.
10. `settings.py` defaults `gemini_model` to `gemini-3.7-flash` while `.env` and
    `.env.example` both use `gemini-3.6-flash`.
11. Imports are rootless (`from config.settings import ...`), so anything must run with
    `backend/` as the working directory / rootdir.
12. `config/database.py` calls `get_settings()` and builds the engine at import time, so
    importing it requires a valid `.env`.

## 11. Environment requirements

macOS (Apple Silicon), Python 3.14.6 in `backend/.venv`, Docker running
`vulcan-postgres` on 5432 with `ddl.sql` already applied, a `.env` at the repo root with
all 12 keys from `.env.example`, and a valid `GOOGLE_API_KEY` for real agent runs only
(the test suite needs none). Never print `.env` values.

## 12. Design decisions worth preserving

- **Decimal-only currency.** `_ensure_decimal` in `rules_engine.py` raises `TypeError` on a
  float — including a float that looks exact like `1000.0` — rather than rescuing it with
  `Decimal(str(v))`. The fix belongs where the float was created.
- **`MATCH_TOLERANCE = Decimal("0.02")`** in `core/rules_engine.py:28`. Single source of
  truth. Never widen it to make a test pass.
- **Deterministic match requires math balance AND a UTR.** Honesty over match rate.
- **The LLM is structurally sandboxed** — it receives one sanitized string and returns one
  validated Pydantic object. It never sees a row, a connection string, or SQL.
- **Variance crosses the LLM boundary as a string**, never a JSON number.
- **Dependency injection in `build_graph`** is what makes the agent testable without a key.
- **`ON CONFLICT` at the database level**, not `except IntegrityError` after the fact.
- **`SELECT ... FOR UPDATE`** via `get_settlement_for_update`, with the documented rule that
  the lock must not be held across a slow LLM call.
- **Failures degrade to "ask a human"**, never to a fabricated success.

## 13. Next recommended implementation step

Build the FastAPI application and a read-only reconciliation API under `backend/api/`,
mounted by a new `backend/main.py`. Rationale: it is the single thing blocking Phase 6, it
requires no schema change and no edit to any verified module, and having the summary
endpoint aggregate directly from `t_reconciliation_ledger` structurally retires
`exceptions_manifest.json` as a source of truth (Known bug 3).

Endpoints: batch summary + match rate, exception list with variance and reason,
settlement-level detail. The HITL resume endpoint comes after, because it needs a schema
change (persisted `thread_id`, human-decision columns) and therefore a migration decision.

## 14. Commands

```bash
cd /Users/adityadeshwal/razorpay-vulcan-ledger/backend

.venv/bin/python -m pytest -q                        # full suite (30 tests)
.venv/bin/python -m pytest tests/test_graph.py -q    # agent only
.venv/bin/python -m scripts.verify_phase2            # live DB smoke test
.venv/bin/python -m evaluation.seed_and_evaluate     # REAL Gemini, billed, writes rows
.venv/bin/python -m scripts.diagnose_gemini          # REAL Gemini A/B, billed

docker start vulcan-postgres
docker exec vulcan-postgres psql -U postgres -d razorpay_vulcan_ledger \
  -c "SELECT recon_state, COUNT(*) FROM t_reconciliation_ledger GROUP BY 1;"
```

