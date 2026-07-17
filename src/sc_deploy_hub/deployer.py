"""
Deployment execution engine for sc-deploy-hub.

Provides:

* :class:`LogBroadcaster` — an in-process pub/sub hub that fans out live log
  chunks to every SSE client subscribed to a given deployment.
* Per-repository async locks to serialise concurrent deploy requests.
* Helper coroutines for running shell commands, querying systemd service
  state, and orchestrating the full deploy pipeline (git fetch → custom steps
  → systemctl restart).
"""

import asyncio
import os
from typing import Dict, Set

from sc_deploy_hub.config import RepositoryConfig
from sc_deploy_hub import db


class LogBroadcaster:
    """In-process pub/sub hub for streaming deployment log output.

    Each active deployment is identified by its integer ID.  Any number of
    :class:`asyncio.Queue` consumers can subscribe to a deployment and will
    each receive every log chunk that is broadcast while they are subscribed.
    A ``None`` sentinel is published when execution finishes so that consumers
    know to close their connection.
    """

    def __init__(self) -> None:
        self.subscribers: Dict[int, Set[asyncio.Queue]] = {}

    def subscribe(self, deployment_id: int) -> asyncio.Queue:
        """Register a new consumer for *deployment_id* and return its queue.

        Args:
            deployment_id: Primary key of the deployment to subscribe to.

        Returns:
            An :class:`asyncio.Queue` that will receive every subsequent log
            chunk plus the ``None`` sentinel on completion.
        """
        if deployment_id not in self.subscribers:
            self.subscribers[deployment_id] = set()
        queue: asyncio.Queue = asyncio.Queue()
        self.subscribers[deployment_id].add(queue)
        return queue

    def unsubscribe(self, deployment_id: int, queue: asyncio.Queue) -> None:
        """Remove *queue* from the subscriber set for *deployment_id*.

        The subscriber entry is cleaned up entirely when the last consumer
        unsubscribes.

        Args:
            deployment_id: Primary key of the deployment.
            queue: The queue returned by a previous :meth:`subscribe` call.
        """
        if deployment_id in self.subscribers:
            self.subscribers[deployment_id].discard(queue)
            if not self.subscribers[deployment_id]:
                del self.subscribers[deployment_id]

    async def broadcast(self, deployment_id: int, message: str) -> None:
        """Push *message* onto every queue subscribed to *deployment_id*.

        Pass ``None`` as *message* to signal end-of-stream to consumers.

        Args:
            deployment_id: Primary key of the deployment.
            message: Log chunk string, or ``None`` to signal completion.
        """
        if deployment_id in self.subscribers:
            for queue in self.subscribers[deployment_id]:
                await queue.put(message)


locks: Dict[str, asyncio.Lock] = {}
broadcaster = LogBroadcaster()


def get_lock(repo_name: str) -> asyncio.Lock:
    """Return the per-repository :class:`asyncio.Lock`, creating it if needed.

    Holding this lock before calling :func:`execute_deploy` guarantees that
    only one deploy for a given repository runs at a time.

    Args:
        repo_name: Repository identifier (must match the key in ``config.yaml``).

    Returns:
        The shared :class:`asyncio.Lock` for that repository.
    """
    if repo_name not in locks:
        locks[repo_name] = asyncio.Lock()
    return locks[repo_name]


async def log_and_broadcast(deployment_id: int, message: str) -> None:
    """Persist *message* to the database and fan it out to live subscribers.

    Args:
        deployment_id: Primary key of the active deployment.
        message: Log line or chunk to record and stream.
    """
    db.append_logs(deployment_id, message)
    await broadcaster.broadcast(deployment_id, message)


async def run_command(cmd: str, cwd: str, deployment_id: int) -> bool:
    """Execute *cmd* in a subprocess, streaming its combined output live.

    stdout and stderr are merged and forwarded line-by-line to
    :func:`log_and_broadcast` so that the web UI terminal updates in real time.

    Args:
        cmd: Shell command string to execute.
        cwd: Working directory for the subprocess.
        deployment_id: Primary key of the owning deployment (for log routing).

    Returns:
        ``True`` if the process exited with code 0, ``False`` otherwise.
    """
    await log_and_broadcast(deployment_id, f"$ {cmd}\n")
    try:
        process = await asyncio.create_subprocess_shell(
            cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            await log_and_broadcast(deployment_id, line.decode("utf-8", errors="replace"))
        await process.wait()
        if process.returncode != 0:
            await log_and_broadcast(deployment_id, f"Command failed with exit code {process.returncode}\n")
        return process.returncode == 0
    except Exception as e:
        await log_and_broadcast(deployment_id, f"Error executing command: {e}\n")
        return False


async def get_service_status(service_name: str) -> str:
    """Query the active state of a systemd service unit.

    First tries ``systemctl is-active`` for a fast single-word answer; falls
    back to ``systemctl show -p ActiveState`` if the result is not one of the
    expected strings.

    Args:
        service_name: The systemd unit name (e.g. ``"my-app.service"``).

    Returns:
        One of ``"active"``, ``"inactive"``, ``"failed"``, or ``"unknown"``.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            "systemctl", "is-active", service_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await process.communicate()
        status = stdout.decode().strip()
        if status in ("active", "inactive", "failed"):
            return status

        process = await asyncio.create_subprocess_exec(
            "systemctl", "show", "-p", "ActiveState", service_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await process.communicate()
        line = stdout.decode().strip()
        if "=" in line:
            return line.split("=")[1]
        return "unknown"
    except Exception:
        return "unknown"


async def control_service(service_name: str, action: str) -> bool:
    """Send a start/stop/restart command to a systemd service via sudo.

    Requires that the running user has passwordless sudo access to
    ``/usr/bin/systemctl``, which ``install.sh`` configures automatically.

    Args:
        service_name: The systemd unit name to control.
        action: One of ``"start"``, ``"stop"``, or ``"restart"``.

    Returns:
        ``True`` if the command exited successfully, ``False`` otherwise.
    """
    if action not in ("start", "stop", "restart"):
        return False
    try:
        process = await asyncio.create_subprocess_exec(
            "sudo", "systemctl", action, service_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await process.communicate()
        return process.returncode == 0
    except Exception:
        return False


async def execute_deploy(deployment_id: int, repo_name: str, repo_config: RepositoryConfig) -> None:
    """Run the full deployment pipeline for a single repository.

    Steps executed in order:

    1. ``git fetch && git reset --hard origin/<branch>`` — fast-forward the
       local clone to the latest remote state, discarding any local changes.
    2. Each command in ``repo_config.deploy_steps`` — arbitrary shell steps
       such as installing dependencies or running migrations.
    3. ``sudo systemctl restart <service_name>`` — reload the service with the
       new code.

    All output is persisted to the database and broadcast live to any
    connected SSE clients.  A ``None`` sentinel is always published on the
    broadcaster when execution ends (success or failure) so that streaming
    clients can close cleanly.

    Args:
        deployment_id: Primary key of the deployment record to update.
        repo_name: Human-readable repository identifier (used in log messages).
        repo_config: Configuration block for this repository.
    """
    try:
        target_path = os.path.expanduser(repo_config.path)
        if not os.path.exists(target_path):
            await log_and_broadcast(deployment_id, f"Error: target path '{target_path}' does not exist.\n")
            db.complete_deployment(deployment_id, "failed")
            return

        await log_and_broadcast(deployment_id, f"Starting deployment of '{repo_name}' on branch '{repo_config.branch}'...\n")

        git_cmd = f"git fetch && git reset --hard origin/{repo_config.branch}"
        if not await run_command(git_cmd, target_path, deployment_id):
            await log_and_broadcast(deployment_id, "Git update failed.\n")
            db.complete_deployment(deployment_id, "failed")
            return

        for step in repo_config.deploy_steps:
            if not await run_command(step, target_path, deployment_id):
                await log_and_broadcast(deployment_id, f"Step '{step}' failed. Stopping deploy.\n")
                db.complete_deployment(deployment_id, "failed")
                return

        restart_cmd = f"sudo systemctl restart {repo_config.service_name}"
        await log_and_broadcast(deployment_id, f"Restarting systemd service: {repo_config.service_name}...\n")
        if not await run_command(restart_cmd, target_path, deployment_id):
            await log_and_broadcast(deployment_id, "Systemd restart failed.\n")
            db.complete_deployment(deployment_id, "failed")
            return

        await log_and_broadcast(deployment_id, "Deployment completed successfully!\n")
        db.complete_deployment(deployment_id, "success")
    except Exception as e:
        await log_and_broadcast(deployment_id, f"Unexpected exception during deployment: {e}\n")
        db.complete_deployment(deployment_id, "failed")
    finally:
        await broadcaster.broadcast(deployment_id, None)
