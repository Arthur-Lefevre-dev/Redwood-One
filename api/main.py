"""FastAPI application entrypoint."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from api.limits import limiter
from api.routes import admin, auth, films, series
from config import get_settings
from core.gpu_detect import refresh_encoder_cache
from db.session import init_db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logger.info("startup: init database tables")
    init_db()
    enc = refresh_encoder_cache()
    logger.info("startup: encoder %s", enc.get("vendor"))
    yield
    logger.info("shutdown")


app = FastAPI(title="Redwood Plus API", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(series.router)
app.include_router(films.router)
app.include_router(admin.router)


@app.get("/api/health")
def health():
    return {"status": "ok"}
