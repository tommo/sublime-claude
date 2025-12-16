"""Centralized notalone instance registry.

All notalone instances register themselves in a shared SQLite database.
This allows instances to discover each other without hardcoded URLs.
"""
import sqlite3
import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Optional, Dict, List
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Default registry location
DEFAULT_REGISTRY_PATH = Path.home() / ".notalone" / "registry.sqlite"


class NotaloneRegistry:
    """Centralized registry for notalone instances."""

    def __init__(self, db_path: Optional[Path] = None):
        """
        Initialize registry.

        Args:
            db_path: Path to registry database. Defaults to ~/.notalone/registry.sqlite
        """
        self.db_path = db_path or DEFAULT_REGISTRY_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _db_connection(self):
        """Context manager for database connections."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self):
        """Initialize registry database schema."""
        with self._db_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS instances (
                    instance_id TEXT PRIMARY KEY,
                    instance_type TEXT NOT NULL,
                    callback_endpoint TEXT NOT NULL,
                    registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()
            logger.info(f"Registry initialized at {self.db_path}")

    def register(self, instance_id: str, instance_type: str, callback_endpoint: str):
        """
        Register an instance in the registry.

        Args:
            instance_id: Unique instance ID (e.g., "sublime.session-123")
            instance_type: Type of instance (e.g., "sublime", "kanban")
            callback_endpoint: RPC callback URL
        """
        with self._db_connection() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO instances (instance_id, instance_type, callback_endpoint, last_seen)
                VALUES (?, ?, ?, ?)
            """, (instance_id, instance_type, callback_endpoint, datetime.utcnow().isoformat()))
            conn.commit()
            logger.info(f"Registered {instance_id} ({instance_type}) at {callback_endpoint}")

    def unregister(self, instance_id: str):
        """Unregister an instance."""
        with self._db_connection() as conn:
            conn.execute("DELETE FROM instances WHERE instance_id = ?", (instance_id,))
            conn.commit()
            logger.info(f"Unregistered {instance_id}")

    def get_instance(self, instance_id: str) -> Optional[Dict]:
        """Get instance by ID."""
        with self._db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                "SELECT * FROM instances WHERE instance_id = ?",
                (instance_id,)
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def find_instances(self, instance_type: Optional[str] = None) -> List[Dict]:
        """
        Find instances by type.

        Args:
            instance_type: Filter by type (e.g., "sublime", "kanban"). None = all instances.

        Returns:
            List of instance records
        """
        with self._db_connection() as conn:
            conn.row_factory = sqlite3.Row
            if instance_type:
                cursor = conn.execute(
                    "SELECT * FROM instances WHERE instance_type = ? ORDER BY last_seen DESC",
                    (instance_type,)
                )
            else:
                cursor = conn.execute("SELECT * FROM instances ORDER BY last_seen DESC")
            return [dict(row) for row in cursor.fetchall()]

    def heartbeat(self, instance_id: str):
        """Update last_seen timestamp for an instance."""
        with self._db_connection() as conn:
            conn.execute(
                "UPDATE instances SET last_seen = ? WHERE instance_id = ?",
                (datetime.utcnow().isoformat(), instance_id)
            )
            conn.commit()

    def cleanup_stale_instances(self, max_age_minutes: int = 60) -> int:
        """
        Remove instances that haven't been seen in the last N minutes.

        Args:
            max_age_minutes: Remove instances not seen in this many minutes

        Returns:
            Number of instances removed
        """
        cutoff = datetime.utcnow() - timedelta(minutes=max_age_minutes)

        with self._db_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM instances WHERE last_seen < ?",
                (cutoff.isoformat(),)
            )
            removed = cursor.rowcount
            conn.commit()
            if removed > 0:
                logger.info(f"Cleaned up {removed} stale instance(s) not seen since {cutoff}")
            return removed
