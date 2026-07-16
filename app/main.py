import asyncio
import contextlib
import logging
import mimetypes
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.core.config import settings
from app.core.logging_config import configure_logging
from app.services import http, wallet_intel

# On Windows, StaticFiles derives Content-Type from the system registry, which often
# maps .css/.js to text/plain. Browsers then refuse to apply the stylesheet, leaving an
# unstyled "text only" page. Force the correct types so it never depends on the machine.
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("text/javascript", ".js")

configure_logging()
logger = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


async def _watchlist_refresh_loop() -> None:
    """Periodically refresh watchlisted wallets' recent buys.

    Bounded per-cycle and interval-gated to respect the free Blockscout rate budget.
    Disable with WATCHLIST_REFRESH_ENABLED=false.
    """
    while True:
        await asyncio.sleep(settings.watchlist_refresh_seconds)
        try:
            refreshed = await wallet_intel.refresh_watchlisted(settings.watchlist_refresh_batch)
            if refreshed:
                logger.info("Watchlist refresh updated %s wallet(s).", refreshed)
        except Exception as exc:  # never let the loop die
            logger.warning("Watchlist refresh cycle failed: %s", exc)


@asynccontextmanager
async def lifespan(_: FastAPI):
    task: asyncio.Task | None = None
    if settings.watchlist_refresh_enabled:
        task = asyncio.create_task(_watchlist_refresh_loop())
    try:
        yield
    finally:
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await http.aclose()


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="Backend API for the Robinhood Rug Analyzer project architecture.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)
app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
