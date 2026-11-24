from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from cairn import __version__
from cairn.server import auth, db
from cairn.server.routers import (
    capabilities,
    events,
    export,
    facts,
    hints,
    intents,
    locks,
    projects,
    runs,
    secrets,
    settings,
)

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.configure(db.DEFAULT_DB)
    auth.warn_if_disabled()
    yield


app = FastAPI(
    title="Cairn",
    description="Fact-graph based collaborative exploration protocol",
    version=__version__,
    lifespan=lifespan,
)

app.middleware("http")(auth.auth_middleware)

app.include_router(settings.router)
app.include_router(projects.router)
app.include_router(hints.router)
app.include_router(intents.router)
app.include_router(export.router)
app.include_router(runs.router)
app.include_router(facts.router)
app.include_router(capabilities.router)
app.include_router(events.router)
app.include_router(locks.router)
app.include_router(secrets.router)


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
