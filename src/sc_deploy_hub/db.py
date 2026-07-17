"""SQLite persistence layer for sc-deploy-hub.

Manages a single ``deployments`` table that records every deployment run,
its metadata (repository, commit, author, trigger), live-appended log output,
and final status.  The database file lives at ``data/deployments.db`` inside
the project root, or at the path given by the ``DB_PATH`` environment variable.

The ``data/`` directory is created automatically on first import. All database
connections are managed via context managers to guarantee clean connection
release and transaction integrity.
"""

import os
import sqlite3
from dotenv import load_dotenv
from contextlib import closing
from datetime import datetime
from typing import Dict, List, Optional

load_dotenv()

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
DB_PATH = os.getenv("DB_PATH", os.path.join(DATA_DIR, "deployments.db"))

os.makedirs(DATA_DIR, exist_ok=True)


def get_db_connection() -> sqlite3.Connection:
    """Open a new SQLite connection with ``Row`` factory enabled.

    Returns:
        An open :class:`sqlite3.Connection` whose rows behave like dicts.
        The caller is responsible for closing it.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create the ``deployments`` table if it does not already exist.

    Safe to call on every application startup — uses ``CREATE TABLE IF NOT
    EXISTS`` so it is idempotent.
    """
    with closing(get_db_connection()) as conn:
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS deployments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    repo_name TEXT NOT NULL,
                    commit_sha TEXT,
                    commit_message TEXT,
                    author TEXT,
                    status TEXT NOT NULL,
                    trigger_type TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    logs TEXT DEFAULT ''
                )
                """
            )


def create_deployment(
    repo_name: str,
    trigger_type: str,
    commit_sha: Optional[str] = None,
    commit_message: Optional[str] = None,
    author: Optional[str] = None,
) -> int:
    """Insert a new deployment record with status ``running``.

    Args:
        repo_name: Name of the repository being deployed.
        trigger_type: How the deployment was initiated (``"webhook"`` or
            ``"manual"``).
        commit_sha: Full SHA of the triggering commit, if available.
        commit_message: Commit message, if available.
        author: Display name of the commit author or pusher.

    Returns:
        The auto-assigned integer primary key of the new row.
    """
    started_at = datetime.now().isoformat()
    with closing(get_db_connection()) as conn:
        with conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO deployments
                    (repo_name, commit_sha, commit_message, author, status, trigger_type, started_at)
                VALUES (?, ?, ?, ?, 'running', ?, ?)
                """,
                (repo_name, commit_sha, commit_message, author, trigger_type, started_at),
            )
            return cursor.lastrowid


def append_logs(deployment_id: int, log_line: str) -> None:
    """Append *log_line* to the ``logs`` column of the given deployment.

    Called repeatedly during execution to build up a complete transcript of
    the deployment's stdout/stderr output.

    Args:
        deployment_id: Primary key of the target deployment row.
        log_line: Text chunk to concatenate onto the existing log.
    """
    with closing(get_db_connection()) as conn:
        with conn:
            conn.execute(
                "UPDATE deployments SET logs = logs || ? WHERE id = ?",
                (log_line, deployment_id),
            )


def complete_deployment(deployment_id: int, status: str) -> None:
    """Mark a deployment as finished by setting its *status* and timestamp.

    Args:
        deployment_id: Primary key of the target deployment row.
        status: Final status string — typically ``"success"`` or ``"failed"``.
    """
    completed_at = datetime.now().isoformat()
    with closing(get_db_connection()) as conn:
        with conn:
            conn.execute(
                "UPDATE deployments SET status = ?, completed_at = ? WHERE id = ?",
                (status, completed_at, deployment_id),
            )


def get_deployments(limit: int = 50, offset: int = 0) -> List[Dict]:
    """Return a paginated list of deployment records, newest first.

    Log output is intentionally excluded from this query to keep list
    responses small; use :func:`get_deployment` to fetch the full record.

    Args:
        limit: Maximum number of rows to return.
        offset: Number of rows to skip (for pagination).

    Returns:
        List of dicts, each representing one deployment row.
    """
    with closing(get_db_connection()) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, repo_name, commit_sha, commit_message, author,
                   status, trigger_type, started_at, completed_at
            FROM deployments
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        )
        return [dict(row) for row in cursor.fetchall()]


def get_deployment(deployment_id: int) -> Optional[Dict]:
    """Fetch a single deployment record including its full log output.

    Args:
        deployment_id: Primary key of the deployment to retrieve.

    Returns:
        A dict of all columns, or ``None`` if no row with that ID exists.
    """
    with closing(get_db_connection()) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM deployments WHERE id = ?", (deployment_id,))
        row = cursor.fetchone()
        return dict(row) if row else None
