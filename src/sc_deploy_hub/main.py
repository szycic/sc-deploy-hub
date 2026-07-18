"""Application entry point for sc-deploy-hub.

Constructs the :class:`fastapi.FastAPI` application, wires up middleware,
static file serving, Jinja2 templates, and mounts the versioned API router.
The single non-API route ``GET /`` renders the server-side dashboard page.
"""

import asyncio
import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from sc_deploy_hub.api import api_v1_router
from sc_deploy_hub.config import load_config
from sc_deploy_hub import db
from sc_deploy_hub import deployer

app = FastAPI(title="SC Deploy Hub", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(CURRENT_DIR, "templates"))
app.mount("/static", StaticFiles(directory=os.path.join(CURRENT_DIR, "static")), name="static")

app.include_router(api_v1_router)


@app.on_event("startup")
async def startup_event() -> None:
    """Initialise the SQLite database on application startup.

    Creates the ``deployments`` table if it does not already exist.
    This is idempotent and safe to run on every restart.
    """
    db.init_db()


@app.get("/")
async def root_redirect():
    """Redirect root requests to the canonical /dashboard URL."""
    return RedirectResponse(url="/dashboard")


@app.get("/dashboard")
@app.get("/history")
@app.get("/config")
async def serve_index(request: Request):
    """Render the dashboard as a server-side HTML page.

    Collects the current systemd status for every configured repository
    concurrently and passes the resulting list to the Jinja2 template so the
    initial page load is fully populated without requiring multiple client-side
    API round-trips.

    Args:
        request: The incoming HTTP request.
    """
    config_data = load_config()
    repo_names = list(config_data.repositories.keys())

    # Query all statuses/details concurrently to optimize initial load time
    status_tasks = [
        deployer.get_service_details(config_data.repositories[name].service_name)
        for name in repo_names
    ]
    details_list = await asyncio.gather(*status_tasks)

    services_list = []
    for name, details in zip(repo_names, details_list):
        repo_config = config_data.repositories[name]
        services_list.append({
            "name": name,
            "path": repo_config.path,
            "branch": repo_config.branch,
            "service_name": repo_config.service_name,
            "deploy_steps": repo_config.deploy_steps,
            "restart_async": getattr(repo_config, "restart_async", False),
            "status": details["status"],
            "details": details,
        })

    return templates.TemplateResponse(request, "index.html", {
        "repositories": services_list,
    })
