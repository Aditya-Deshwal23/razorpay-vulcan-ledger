"""FastAPI entry point for the Vulcan Ledger operations API."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from agents.graph import reconciliation_graph
from api.reconciliation import router as reconciliation_router
from config.settings import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Keep one Postgres-backed graph/checkpointer alive for review decisions."""
    async with reconciliation_graph() as graph:
        app.state.reconciliation_graph = graph
        yield


app = FastAPI(
    title="Vulcan Ledger API",
    version="1.0.0",
    description="Audit-grade Razorpay settlement reconciliation operations API.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origin_list,
    allow_credentials=False,
    # POST covers both JSON payloads and multipart/form-data file uploads.
    allow_methods=["GET", "POST", "DELETE"],
    # Allow Content-Type (JSON), multipart/form-data, and cache control headers.
    allow_headers=["Content-Type", "Cache-Control", "X-Requested-With"],
    expose_headers=["Content-Disposition", "X-Batch-ID", "X-Row-Count"],
)

app.include_router(reconciliation_router)
