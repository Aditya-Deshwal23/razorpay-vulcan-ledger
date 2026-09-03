# Vulcan Ledger frontend implementation specification

## Product thesis and demo story

Vulcan Ledger gives a Finance Controller a defensible answer to one operational
question: **which settlement exception needs a decision, why, and what evidence
supports it?**

The product's strongest end-to-end story is deliberately narrow:

1. A reconciliation run has mostly cleared automatically, but a small number of
   settlements remain unsafe to book.
2. The controller opens the highest-impact outstanding item and sees the
   deterministic arithmetic, the model's separate classification, the original
   bank evidence, and the audit fingerprint in one place.
3. They approve or reject it. The decision is written atomically, the parked
   LangGraph run is resumed where available, and an immutable audit event is
   visible immediately.

The UI will therefore orient every route around **exception → evidence →
decision → audit record**, rather than around generic analytics. This is the
story a Razorpay Buildathon judge should understand without a spoken tour.

## Foundation verified before UI work

The repository contains a hardened Decimal-only PostgreSQL reconciliation
engine. The current database applies migrations `001_baseline` and
`002_hardening`; it has the closed reconciliation-state check, bank-credit
dedupe/hash guards, exclusive bank-credit claim, accounting equation check,
batch provenance, idempotent writes, and guarded HITL decisions. The full
backend suite passes (58 tests).

Two frontend-critical gaps were found and will be repaired before integration:

- No FastAPI application or HTTP contract exists, despite FastAPI being a
  dependency.
- A reconciliation row's `cryptographic_state_hash` was created for its initial
  state and not updated after a human decision. There was also no append-only
  event history for the proposed Audit route. A migration will preserve canonical
  evidence text, backfill historical event snapshots, and add an immutable
  reconciliation-event ledger. New reconciliation and decision helpers will
  write event records and keep the current state fingerprint consistent.

The stale `scripts/verify_phase2.py` utility conflicts with current UTR
semantics, but it is not invoked by the product or the UI. It is excluded from
the demo runbook rather than being used as a source of truth; the maintained
pytest suite and migration status are the verification route.

## Backend contract

The API is served by `backend/main.py` at `http://localhost:8000` and mounted
under `/api`. All monetary values are canonical strings (`"976.40"`), never JSON
floats. Errors are typed JSON with a stable machine-readable `code` and a
human-readable `detail`.

Required endpoints:

| Endpoint | Purpose |
| --- | --- |
| `GET /api/health` | Database/app health for Settings and failure states. |
| `GET /api/batches` | Recent run history with state counts and auto-match rate. |
| `GET /api/batches/{id}/summary` | Overview metrics scoped to one true `batch_run_id`. |
| `GET /api/batches/{id}/review` | Pending HITL items, joined with settlement and bank evidence. |
| `GET /api/settlements` | Paginated ledger explorer with search and state/bank/batch filters. |
| `GET /api/settlements/{id}` | Full settlement → bank credit → reconciliation chain. |
| `GET /api/audit` | Timestamp-descending immutable reconciliation event stream. |
| `POST /api/settlements/{id}/decision` | Validated `APPROVED`/`REJECTED` human decision. It resumes the persisted graph thread when possible, commits the guarded verdict, and returns the resolved state. |

The frontend must not invent a "run batch" action. A run currently calls a
real Gemini-backed evaluator and may incur cost; there is no safe background-job
contract yet. Batches presents truthful history and links to a run instead.

## Frontend architecture

Create a Next.js App Router application in `frontend/`, TypeScript strict mode,
with plain CSS modules/global tokens and a small dependency set: React,
Next, and Lucide React. No chart library is required: the only chart is a
semantic reconciliation progress rail made from CSS so it stays precise,
lightweight, and screen-reader describable.

```
frontend/
  app/                 routes, shared layout, loading/error boundaries
  components/          domain and interface components
  lib/                 typed API client, formatters, route/query helpers
  types/               API response contracts
  styles/              global tokens and component/page styles
```

The browser reads `NEXT_PUBLIC_API_URL`, defaulting to `http://localhost:8000`.
Server-side page data is deliberately not required: pages are client-interactive
because a decision must refresh the live queue without a full navigation. The
API client centralizes `fetch`, JSON parsing, abort support, and typed error
normalization. Page-level hooks own loading/error/refetch state; no global state
library is warranted for this small operational console.

## Routes and navigation

The persistent left rail has five destinations and one contextual batch picker.
The current batch is carried in `?batch=<batch_run_id>` so a copied URL preserves
the exact demo state.

| Route | Job |
| --- | --- |
| `/` | **Overview**: identify the selected run's auto-match rate and the exact number needing human action. CTA opens review. |
| `/review?batch=` | **Review**: the hero workflow, one evidence-rich exception at a time. |
| `/batches` | **Batches**: run history; selecting a row scopes Overview. No fake rerun control. |
| `/ledger?batch=&state=&q=` | **Ledger**: searchable, filterable source-of-truth explorer. |
| `/audit?batch=` | **Audit**: unstyled-by-design immutable event stream and hash copies. |
| `/settings` | **Settings**: API/database health and the configured model identifier when the backend exposes it safely. |

Routes must remain useful without records: Overview says no completed batches are
available; Review says the queue is clear; Ledger directs the operator to choose
a batch; Audit explains that no event has been recorded yet.

## Major UI components

`AppShell` contains the responsive navigation, batch picker, and application
status. `StateBadge` maps raw enum values to accessible names and a distinct
shape/icon. `Money` renders the rupee symbol at a reduced visual weight and the
exact string value in tabular IBM Plex Mono. `HashFingerprint` has a copy action
and exposes the full hash in its accessible label. `ApiState` renders skeleton,
empty, or actionable error states consistently.

Overview uses:

- `CommandHeader`, containing the selected batch and compact run timestamp.
- `MatchRate` as the primary verdict: auto-reconciled items over the batch total.
- `NeedsYou` as an equally prominent counter and the only primary action:
  “Review exceptions”.
- `ReconciliationRail`, a readable state distribution rather than decorative
  charting. Each segment is labelled in the accessible summary.
- `RecentEvidence`, the three most recent audit events with direct destinations.

Review uses a two-panel desktop layout:

- `ReviewQueue`: clickable settlement identifiers, current selection, magnitude,
  and state. It stays visible while evidence changes.
- `ReviewEvidence`: discrepancy classification, confidence meter, deterministic
  variance and AI-reported variance side by side, expected net versus bank
  credit, raw narration, UTR availability, and the current state hash.
- `DecisionControls`: equal-weight approve/reject controls. A required reviewer
  identity field defaults to `finance.controller` locally, remains editable,
  and validates between 2 and 100 non-whitespace characters. During a POST only
  the decision controls enter `Confirming…`; after success, the list refetches
  and selects the next record. A 409 reports the existing decision exactly;
  network and 5xx errors stay inline with a retry action.

Ledger uses a debounced text filter (settlement ID, UTR, or exact amount), state
and bank selects, and server pagination. Selecting a row opens an in-place
detail panel with accounting components, bank evidence, reconciliation fields,
and a link to Audit. It does not pretend parser-rule metadata exists when it
does not.

Audit uses a dense, semantic table: timestamp, event, settlement, state
transition, actor, deterministic variance, and cryptographic fingerprint.
Hashes copy with a confirmation status. This screen intentionally has no
aggregate charts or action buttons.

## Visual system

This is desktop-first audit software on a 1440px canvas. Its material reference
is a forged ledger: raw bank text becomes a load-bearing financial record.

| Token | Hex / use |
| --- | --- |
| Iron Ink | `#171716`, page ground |
| Graphite | `#22211F`, panels and navigation |
| Tempered Steel | `#5B6265`, hairlines and inactive controls |
| Parchment | `#F1EBDD`, primary text |
| Ash | `#A89F91`, secondary copy |
| Forge Ember | `#D86B35`, active navigation, focus, one primary action |
| Verdigris | `#6BB29D`, deterministic match / approval |
| Molten Gold | `#D7A544`, AI-resolved / in-progress |
| Rust | `#C96852`, review / rejected / errors |

Fraunces is reserved for page titles and one hero measure. IBM Plex Sans handles
every operational label and sentence. IBM Plex Mono handles all money, IDs,
confidence percentages, and hashes with tabular figures. Fonts load through
`next/font/google`; no Inter, gradients, glass effects, pills-as-layout, bank
logos, emojis, or dashboard-card grids are allowed.

The 4px spacing scale is `4, 8, 12, 16, 24, 32, 48, 64`; controls use 6px
radii and panels use 10px. Hairline dividers and a near-imperceptible grain
texture create depth. Semantic color is always paired with readable copy and a
state-specific line icon. Motion is restrained to an 180ms state transition;
`prefers-reduced-motion` disables it.

The one signature element is the **evidence braid** in Review: a narrow,
three-part vertical/horizontal rail that physically links “Razorpay expected
net”, “bank credit”, and “controller decision” to the state hash. It makes the
system's central claim—math, AI output, and human accountability do not blur
together—visible at a glance. It replaces the reference's over-broad activity
feed as the product's memorable visual.

## State, validation, and accessibility

Every route needs skeletons shaped like final content, concrete empty copy,
retryable service-error panels, focus-visible controls, keyboard-reachable row
selection, and `aria-live` outcome text. The UI will preserve exact backend
error text after a short action-focused explanation, never hide a conflict
behind “Something went wrong.” Browser confirmation is not used for decisions:
the deliberate two equal controls plus inline pending state make intent clear
without adding a disruptive step.

At widths below 1080px the rail collapses to icons and Review's queue becomes a
horizontal strip above evidence. At widths below 760px tables retain their
columns inside a horizontal scroller, and controls stack without losing their
labels. The product remains fully usable, though its primary target is a
laptop or wider analyst workstation.

## Implementation order and acceptance checks

1. Add migration-backed evidence/event integrity and an API with backend tests.
2. Bootstrap the typed Next application and global design tokens.
3. Build Review first, then Overview, followed by Ledger, Batches, Audit, and
   Settings.
4. Validate live API data in the browser, decision conflict handling, all empty
   and service-error states, keyboard paths, and the desktop/tablet layouts.
5. Run backend tests, frontend typecheck/lint/build, and capture desktop/mobile
   screenshots for a visual judge-style critique. Remove anything that obscures
   the exception-to-audit story before delivery.

The demo starts at Overview, moves directly to Review, applies one decision,
then opens Audit to prove the event was persisted. Batches and Ledger support
the demonstration; they do not compete with it.
