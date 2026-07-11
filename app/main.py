from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.core.config import settings
from app.core.logging_config import configure_logging

configure_logging()

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="Backend API for the Robinhood Rug Analyzer project architecture.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)
app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")
