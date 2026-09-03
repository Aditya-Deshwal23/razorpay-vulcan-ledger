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
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)

app.include_router(reconciliation_router)
