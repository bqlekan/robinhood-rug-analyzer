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
from app.services import http, kol_scheduler, kol_watchlist, token_monitor, wallet_intel

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


async def _token_monitor_loop() -> None:
    """Periodically re-run the reused intelligence pipeline over the token
    watchlist and record what changed (M24).

    Interval-gated to respect the free-tier API budget the analyzer consumes.
    The whole engine is opt-in: this loop only starts when TOKEN_MONITOR_ENABLED
    is set. A single cycle can never kill the loop — run_cycle is failure-isolated
    and this guards it a second time.
    """
    # Reconcile any config-driven seed watchlist once at startup.
    try:
        token_monitor.sync_from_config()
    except Exception as exc:  # seeding must never block the loop starting
        logger.warning("Token monitor seed sync failed: %s", exc)
    while True:
        await asyncio.sleep(settings.token_monitor_interval_seconds)
        try:
            report = await token_monitor.run_cycle()
            if report.changed:
                logger.info("Token monitor cycle recorded %s change(s).", report.changed)
        except Exception as exc:  # never let the loop die
            logger.warning("Token monitor cycle failed: %s", exc)


async def _kol_scheduler_loop() -> None:
    """Periodically capture follows from the enabled KOL roster and run the reused
    M23 pipeline over what changed (M25).

    Interval-gated to respect the X scraping/rate budget. Opt-in: this loop only
    starts when KOL_SCHEDULER_ENABLED is set. A single cycle can never kill the
    loop — run_cycle is failure-isolated and this guards it a second time.
    """
    # Reconcile any config-driven seed roster once at startup.
    try:
        kol_watchlist.sync_from_config()
    except Exception as exc:  # seeding must never block the loop starting
        logger.warning("KOL scheduler seed sync failed: %s", exc)
    while True:
        await asyncio.sleep(settings.kol_scheduler_interval_seconds)
        try:
            report = await kol_scheduler.run_cycle()
            if report.captured or report.failed:
                logger.info(
                    "KOL scheduler cycle: captured=%s failed=%s",
                    report.captured, report.failed,
                )
        except Exception as exc:  # never let the loop die
            logger.warning("KOL scheduler cycle failed: %s", exc)


@asynccontextmanager
async def lifespan(_: FastAPI):
    tasks: list[asyncio.Task] = []
    if settings.watchlist_refresh_enabled:
        tasks.append(asyncio.create_task(_watchlist_refresh_loop()))
    if settings.token_monitor_enabled:
        tasks.append(asyncio.create_task(_token_monitor_loop()))
    if settings.kol_scheduler_enabled:
        tasks.append(asyncio.create_task(_kol_scheduler_loop()))
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
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
