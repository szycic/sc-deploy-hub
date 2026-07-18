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
from datetime import datetime
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

    Args:
        service_name: The systemd unit name (e.g. ``"my-app.service"``).

    Returns:
        One of ``"active"``, ``"inactive"``, ``"failed"``, or ``"unknown"``.
    """
    details = await get_service_details(service_name)
    return details["status"]


async def get_service_details(service_name: str) -> dict:
    """Query extensive details of a systemd service unit.

    Args:
        service_name: The systemd unit name (e.g. ``"my-app.service"``).

    Returns:
        A dictionary containing:
            * status: One of ``"active"``, ``"inactive"``, ``"failed"``, or ``"unknown"``.
            * sub_state: Systemd sub-state (e.g. ``"running"``, ``"dead"``).
            * pid: The Main PID (int), or None.
            * memory: Human-readable current memory usage (str), or None.
            * uptime: Human-readable active uptime duration (str), or None.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            "systemctl", "show",
            "-p", "ActiveState",
            "-p", "SubState",
            "-p", "MainPID",
            "-p", "MemoryCurrent",
            "-p", "MemoryPeak",
            "-p", "ActiveEnterTimestamp",
            service_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await process.communicate()
        lines = stdout.decode(errors="replace").strip().splitlines()
        props = {}
        for line in lines:
            if "=" in line:
                k, v = line.split("=", 1)
                props[k.strip()] = v.strip()
        
        status = props.get("ActiveState", "unknown")
        sub_state = props.get("SubState", "")
        
        # Parse PID
        pid_str = props.get("MainPID", "0")
        pid = int(pid_str) if pid_str.isdigit() and pid_str != "0" else None
        
        # Parse Memory — MemoryCurrent requires MemoryAccounting=yes in the unit.
        # If disabled, systemd returns '[not set]' or the uint64 sentinel max value.
        # Fall back to MemoryPeak (available on systemd ≥ 253) when current is unavailable.
        _UINT64_MAX = 18446744073709551615

        def _parse_mem_bytes(raw: str):
            raw = raw.strip()
            if not raw or raw in ("[not set]", "infinity", ""):
                return None
            try:
                val = int(raw)
                if val <= 0 or val >= _UINT64_MAX:
                    return None
                return val
            except ValueError:
                return None

        def _fmt_mem(val: int) -> str:
            if val >= 1024 * 1024:
                return f"{val / (1024 * 1024):.1f} MB"
            elif val >= 1024:
                return f"{val / 1024:.0f} KB"
            return f"{val} B"

        memory = None
        mem_val = _parse_mem_bytes(props.get("MemoryCurrent", ""))
        if mem_val is not None:
            memory = _fmt_mem(mem_val)
        else:
            # Fallback 1: MemoryPeak (systemd ≥ 253)
            peak_val = _parse_mem_bytes(props.get("MemoryPeak", ""))
            if peak_val is not None:
                memory = f"{_fmt_mem(peak_val)} (peak)"

        # Fallback 2: read RSS from /proc/{pid}/status — always available regardless
        # of whether MemoryAccounting is enabled in the systemd unit.
        if memory is None and pid is not None:
            try:
                with open(f"/proc/{pid}/status", "r") as _f:
                    for _line in _f:
                        if _line.startswith("VmRSS:"):
                            _parts = _line.split()
                            if len(_parts) >= 2:
                                _kb = int(_parts[1])
                                memory = _fmt_mem(_kb * 1024)
                            break
            except Exception:
                pass

        # Parse Uptime from ActiveEnterTimestamp
        # Timestamp format: "Sat 2026-07-18 08:00:00 CEST"
        timestamp_str = props.get("ActiveEnterTimestamp", "")
        uptime = None
        if timestamp_str and timestamp_str not in ("n/a", "", "@0", "0"):
            parts = timestamp_str.split()
            date_part = None
            time_part = None
            for p in parts:
                if "-" in p and len(p) == 10 and p[4] == "-" and p[7] == "-":
                    date_part = p
                elif ":" in p and len(p) == 8 and p[2] == ":" and p[5] == ":":
                    time_part = p
            
            if date_part and time_part:
                try:
                    dt = datetime.strptime(f"{date_part} {time_part}", "%Y-%m-%d %H:%M:%S")
                    diff = (datetime.now() - dt).total_seconds()
                    if diff >= 0:
                        if diff < 60:
                            uptime = f"{int(diff)}s"
                        elif diff < 3600:
                            uptime = f"{int(diff // 60)}m"
                        elif diff < 86400:
                            hrs = int(diff // 3600)
                            mins = int((diff % 3600) // 60)
                            uptime = f"{hrs}h {mins}m"
                        else:
                            days = int(diff // 86400)
                            hrs = int((diff % 86400) // 3600)
                            uptime = f"{days}d {hrs}h"
                except Exception:
                    pass
        
        return {
            "status": status,
            "sub_state": sub_state,
            "pid": pid,
            "memory": memory,
            "uptime": uptime
        }
    except Exception:
        return {
            "status": "unknown",
            "sub_state": "",
            "pid": None,
            "memory": None,
            "uptime": None
        }


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

        if getattr(repo_config, "restart_async", False):
            # For asynchronous restarts (e.g. self-restart), complete the deployment first.
            # Otherwise, systemd shutting down this process will abort the task, leaving
            # the deployment marked as "failed" or "running" in the database.
            await log_and_broadcast(deployment_id, "Deployment completed successfully!\n")
            db.complete_deployment(deployment_id, "success")
            await broadcaster.broadcast(deployment_id, None)

            restart_cmd = f"sudo systemctl restart {repo_config.service_name} --no-block"
            await log_and_broadcast(deployment_id, f"Restarting systemd service asynchronously: {repo_config.service_name}...\n")
            try:
                # Fire and forget the restart command to prevent blocking/waiting on a dying process
                await asyncio.create_subprocess_shell(
                    restart_cmd,
                    cwd=target_path,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
            except Exception as e:
                await log_and_broadcast(deployment_id, f"Failed to initiate systemd restart: {e}\n")
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
