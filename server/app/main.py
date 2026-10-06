"""FastAPI entry point. Run with: uvicorn app.main:app (from the server/ folder)."""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .db import get_db, init_db
from .logging_setup import setup_logging

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    init_db()
    log.info("Salty Island Run Club started")
    yield


app = FastAPI(title="Salty Island Run Club", lifespan=lifespan)


@app.get("/healthz")
def healthz() -> dict:
    with get_db() as conn:
        conn.execute("SELECT 1")
    return {"ok": True}
