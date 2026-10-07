import os
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import IntegrityError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.router import api_router
from app.api.ws.connection_manager import manager
from app.infrastructure.config import get_settings
from app.infrastructure.observability.logging import (
    bind_context,
    clear_context,
    configure_logging,
    get_logger,
)
from app.infrastructure.redis.pubsub import get_event_bus

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start and stop the Redis pub/sub bus alongside the app.

    Without the bus running, every broadcast is confined to the process that
    produced it.
    """
    bus = get_event_bus()
    manager.bind_bus(bus)
    await bus.start()
    logger.info("app.started", env=get_settings().app_env)
    try:
        yield
    finally:
        await bus.stop()
        logger.info("app.stopped")


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging()

    app = FastAPI(
        title=settings.app_name,
        debug=settings.debug,
        version="0.1.0",
        lifespan=lifespan,
    )

    # Cookie-authenticated requests need an explicit origin allowlist; a
    # wildcard is incompatible with credentialed CORS.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )

    app.include_router(api_router, prefix=settings.api_prefix)

    backend_dir = os.path.dirname(os.path.dirname(__file__))

    # Maps we author, served to the client. Read-only and committed to the
    # repo, so they survive redeploys and are identical on every replica.
    assets_dir = os.path.join(backend_dir, "assets")
    os.makedirs(assets_dir, exist_ok=True)
    app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    uploads_dir = os.path.join(backend_dir, "uploads")
    os.makedirs(uploads_dir, exist_ok=True)
    app.mount("/uploads", StaticFiles(directory=uploads_dir), name="uploads")

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        clear_context()
        bind_context(request_id=request_id, path=request.url.path, method=request.method)
        try:
            response = await call_next(request)
        finally:
            clear_context()
        response.headers["x-request-id"] = request_id
        return response

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        code = getattr(exc, "code", "HTTP_ERROR")
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": code, "message": str(exc.detail)}},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "Invalid request parameters.",
                }
            },
        )

    @app.exception_handler(IntegrityError)
    async def integrity_error_handler(request: Request, exc: IntegrityError):
        logger.warning("db.integrity_error", exc_info=True)
        return JSONResponse(
            status_code=409,
            content={"error": {"code": "CONFLICT", "message": "Database conflict occurred."}},
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        logger.error("http.unhandled_error", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "INTERNAL_ERROR", "message": "Something went wrong."}},
        )

    return app


app = create_app()
