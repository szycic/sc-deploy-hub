"""REST API routes for sc-deploy-hub (versioned under ``/api/v1``).

All routes are registered on :data:`api_v1_router` and mounted by
:mod:`sc_deploy_hub.main`.  The router is self-contained — it owns its own
prefix so that it can be included with a bare ``app.include_router()`` call.

Endpoints summary
-----------------
POST /api/v1/webhook
    Receive and authenticate GitHub push webhooks, then queue a deployment.

GET  /api/v1/services
    List every configured repository with its current systemd status and last
    deployment summary.

POST /api/v1/services/{name}/deploy
    Manually trigger a deployment for the named repository.

POST /api/v1/services/{name}/control
    Send a start/stop/restart command to the associated systemd service.

GET  /api/v1/services/{name}/journal
    Return the last 100 lines of the systemd journal for the service.

GET  /api/v1/deployments
    Paginated list of historical deployment records (no log body).

GET  /api/v1/deployments/{id}/logs
    Full log output for a completed deployment.

GET  /api/v1/deployments/{id}/logs/stream
    Server-Sent Events stream of live log output for a running deployment;
    replays any buffered output first, then follows in real time.

GET  /api/v1/config
    Return the raw YAML content of ``config.yaml``.

POST /api/v1/config
    Validate and overwrite ``config.yaml`` with new YAML content.
"""

import asyncio
import hashlib
import hmac
import os
from contextlib import closing
from typing import Optional

import yaml as _yaml
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from sc_deploy_hub.config import CONFIG_PATH, AppConfig, RepositoryConfig, load_config
from sc_deploy_hub import db
from sc_deploy_hub import deployer

api_v1_router = APIRouter(prefix="/api/v1")


async def _verify_github_signature(request: Request, secret: str, signature: str) -> None:
    """Verify the HMAC-SHA256 signature on an incoming GitHub webhook request.

    Raises :class:`fastapi.HTTPException` (401/400/403) if the signature is
    missing, malformed, or does not match the computed digest.

    Args:
        request: The raw FastAPI request (body will be read for hashing).
        secret: The shared secret configured in GitHub and ``config.yaml``.
        signature: Value of the ``X-Hub-Signature-256`` header.
    """
    if not signature:
        raise HTTPException(status_code=401, detail="X-Hub-Signature-256 header missing")
    body = await request.body()
    parts = signature.split("=")
    if len(parts) != 2 or parts[0] != "sha256":
        raise HTTPException(status_code=400, detail="Invalid signature format")
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, parts[1]):
        raise HTTPException(status_code=403, detail="Signature verification failed")


async def _deploy_task_worker(deployment_id: int, repo_name: str, repo_config: RepositoryConfig) -> None:
    """Background task wrapper that acquires the per-repo lock before deploying.

    Ensures that simultaneous webhook or manual triggers for the same
    repository are serialised rather than run in parallel.

    Args:
        deployment_id: Primary key of the deployment record to update.
        repo_name: Repository identifier used to look up the shared lock.
        repo_config: Full configuration for the repository being deployed.
    """
    async with deployer.get_lock(repo_name):
        await deployer.execute_deploy(deployment_id, repo_name, repo_config)


def _format_sse(data: str) -> str:
    """Format a multi-line string as one or more SSE ``data:`` frames.

    Each line of *data* becomes its own ``data: <line>`` field, and the
    event block is terminated with a blank line as required by the SSE spec.

    Args:
        data: Raw text to encode.

    Returns:
        SSE-formatted string ready to be yielded from a streaming response.
    """
    return "".join(f"data: {line}\n" for line in data.splitlines()) + "\n"


class ControlAction(BaseModel):
    """Request body for the service control endpoint.

    Attributes:
        action: The systemd action to perform — one of ``start``, ``stop``,
            or ``restart``.
    """

    action: str


class ConfigBody(BaseModel):
    """Request body for the config save endpoint.

    Attributes:
        content: Full YAML text to write to ``config.yaml``.
    """

    content: str


@api_v1_router.post("/webhook")
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature_256: Optional[str] = Header(None),
    x_github_event: Optional[str] = Header(None),
):
    """Receive a GitHub webhook and queue a deployment for push events.

    Non-push events are acknowledged and ignored.  Push events targeting a
    different branch than the one configured for the repository are also
    silently ignored.  When a ``github_secret`` is configured the signature is
    verified before any work is queued.

    Returns a JSON body with ``status: "queued"`` and the new ``deployment_id``
    on success, or ``status: "ignored"`` with a reason when skipped.
    """
    if x_github_event != "push":
        return {"status": "ignored", "reason": f"Unsupported event: {x_github_event}"}

    payload = await request.json()
    repo_name = payload.get("repository", {}).get("name")
    ref = payload.get("ref", "")

    if not repo_name or not ref:
        raise HTTPException(status_code=400, detail="Invalid payload: missing repository name or ref")

    config_data = load_config()
    if repo_name not in config_data.repositories:
        return {"status": "ignored", "reason": f"Repository '{repo_name}' not configured"}

    repo_config = config_data.repositories[repo_name]
    expected_ref = f"refs/heads/{repo_config.branch}"
    if ref != expected_ref:
        return {"status": "ignored", "reason": f"Branch '{ref}' does not match configured '{expected_ref}'"}

    if config_data.webhooks.github_secret:
        await _verify_github_signature(request, config_data.webhooks.github_secret, x_hub_signature_256)

    head_commit = payload.get("head_commit", {})
    dep_id = db.create_deployment(
        repo_name=repo_name,
        trigger_type="webhook",
        commit_sha=head_commit.get("id"),
        commit_message=head_commit.get("message"),
        author=head_commit.get("author", {}).get("name") or payload.get("pusher", {}).get("name"),
    )
    background_tasks.add_task(_deploy_task_worker, dep_id, repo_name, repo_config)
    return {"status": "queued", "deployment_id": dep_id}


@api_v1_router.get("/services")
async def get_services():
    """Return all configured repositories with live status and last deployment.

    Optimized to query systemd states concurrently via asyncio.gather and
    retrieve the latest deployment details for all repositories in a single
    optimized database query using a window partition function.
    """
    config_data = load_config()
    repo_names = list(config_data.repositories.keys())

    # Query all statuses/details concurrently
    status_tasks = [
        deployer.get_service_details(config_data.repositories[name].service_name)
        for name in repo_names
    ]
    details_list = await asyncio.gather(*status_tasks)

    # Fetch last deployments for all repos in a single query
    last_deployments = {}
    with closing(db.get_db_connection()) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT repo_name, id, status, started_at, completed_at, commit_sha, commit_message, author
            FROM (
                SELECT *, ROW_NUMBER() OVER (PARTITION BY repo_name ORDER BY id DESC) as rn
                FROM deployments
            )
            WHERE rn = 1
            """
        )
        for row in cursor.fetchall():
            row_dict = dict(row)
            repo_name = row_dict.pop("repo_name")
            last_deployments[repo_name] = row_dict

    result = []
    for name, details in zip(repo_names, details_list):
        repo_config = config_data.repositories[name]
        result.append({
            "name": name,
            "path": repo_config.path,
            "branch": repo_config.branch,
            "service_name": repo_config.service_name,
            "deploy_steps": repo_config.deploy_steps,
            "restart_async": getattr(repo_config, "restart_async", False),
            "status": details["status"],
            "details": details,
            "last_deployment": last_deployments.get(name),
        })
    return result


@api_v1_router.post("/services/{name}/deploy")
async def trigger_deploy(name: str, background_tasks: BackgroundTasks):
    """Manually trigger a deployment for the named repository.

    Creates a ``manual`` deployment record and queues the deploy pipeline in
    the background.  Returns the new ``deployment_id`` immediately so the
    caller can subscribe to the log stream.

    Args:
        name: Repository key as defined in ``config.yaml``.
    """
    config_data = load_config()
    if name not in config_data.repositories:
        raise HTTPException(status_code=404, detail="Repository not configured")

    repo_config = config_data.repositories[name]
    dep_id = db.create_deployment(
        repo_name=name,
        trigger_type="manual",
        commit_sha="Manual Run",
        commit_message="Triggered via Deploy Web UI",
        author="Admin",
    )
    background_tasks.add_task(_deploy_task_worker, dep_id, name, repo_config)
    return {"status": "queued", "deployment_id": dep_id}


@api_v1_router.post("/services/{name}/control")
async def service_control(name: str, control: ControlAction):
    """Send a systemd control command (start/stop/restart) to a service.

    Args:
        name: Repository key as defined in ``config.yaml``.
        control: JSON body containing the ``action`` field.
    """
    config_data = load_config()
    if name not in config_data.repositories:
        raise HTTPException(status_code=404, detail="Repository not configured")

    repo_config = config_data.repositories[name]
    success = await deployer.control_service(repo_config.service_name, control.action)
    if not success:
        raise HTTPException(status_code=500, detail=f"Failed to execute '{control.action}' on service")
    return {"status": "success"}


@api_v1_router.get("/services/{name}/journal")
async def get_service_journal(name: str):
    """Return the last 100 lines of the systemd journal for a service.

    Args:
        name: Repository key as defined in ``config.yaml``.
    """
    config_data = load_config()
    if name not in config_data.repositories:
        raise HTTPException(status_code=404, detail="Service not configured")

    repo_config = config_data.repositories[name]
    try:
        process = await asyncio.create_subprocess_exec(
            "journalctl", "-u", repo_config.service_name, "-n", "100", "--no-pager",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        return {"logs": stdout.decode(errors="replace") + stderr.decode(errors="replace")}
    except Exception as e:
        return {"logs": f"Failed to retrieve service journal logs: {e}"}


@api_v1_router.get("/deployments")
def get_deployments(limit: int = 50, offset: int = 0):
    """Return a paginated list of deployment records, newest first.

    Log bodies are excluded from this response; use ``/deployments/{id}/logs``
    to retrieve the full output for a specific run.

    Args:
        limit: Maximum number of records to return (default 50).
        offset: Number of records to skip for pagination.
    """
    return db.get_deployments(limit=limit, offset=offset)


@api_v1_router.get("/deployments/{id}/logs")
def get_deployment_logs(id: int):
    """Return the complete log output for a finished deployment.

    Args:
        id: Primary key of the deployment.
    """
    dep = db.get_deployment(id)
    if not dep:
        raise HTTPException(status_code=404, detail="Deployment not found")
    return {"logs": dep.get("logs", "")}


@api_v1_router.get("/deployments/{id}/logs/stream")
def stream_logs(id: int):
    """Stream live log output for a deployment as Server-Sent Events.

    Any log text already stored in the database is replayed immediately on
    connection.  If the deployment is still ``running`` the endpoint then
    subscribes to the :data:`~sc_deploy_hub.deployer.broadcaster` and forwards
    every subsequent chunk until a ``None`` sentinel signals completion.

    Args:
        id: Primary key of the deployment to stream.
    """
    dep = db.get_deployment(id)
    if not dep:
        raise HTTPException(status_code=404, detail="Deployment not found")

    async def event_generator():
        if dep["logs"]:
            yield _format_sse(dep["logs"])
        if dep["status"] != "running":
            yield "data: [DONE]\n\n"
            return
        queue = deployer.broadcaster.subscribe(id)
        try:
            while True:
                try:
                    chunk = await queue.get()
                    if chunk is None:
                        yield "data: [DONE]\n\n"
                        break
                    yield _format_sse(chunk)
                except asyncio.CancelledError:
                    break
        finally:
            deployer.broadcaster.unsubscribe(id, queue)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@api_v1_router.get("/config")
def get_config_raw():
    """Return the raw YAML content of ``config.yaml`` as a string.

    Returns an empty string when the file does not yet exist.
    """
    if not os.path.exists(CONFIG_PATH):
        return {"content": ""}
    with open(CONFIG_PATH, "r") as f:
        return {"content": f.read()}


@api_v1_router.post("/config")
def save_config_raw(body: ConfigBody):
    """Validate and persist new YAML content to ``config.yaml``.

    The submitted text is parsed and validated against :class:`AppConfig`
    before writing to disk, so a malformed or schema-violating payload is
    rejected without corrupting the existing file.

    Args:
        body: JSON body containing the full YAML text in the ``content`` field.

    Raises:
        HTTPException 400: YAML syntax is invalid.
        HTTPException 422: YAML is valid but does not conform to the
            :class:`AppConfig` schema.
    """
    try:
        parsed = _yaml.safe_load(body.content) or {}
    except _yaml.YAMLError as e:
        raise HTTPException(status_code=400, detail=f"YAML parse error: {e}")
    try:
        AppConfig.model_validate(parsed)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Config validation error: {e}")
    with open(CONFIG_PATH, "w") as f:
        f.write(body.content)
    return {"status": "saved"}
