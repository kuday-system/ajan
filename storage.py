import json
import sqlite3
from pathlib import Path
from typing import Optional

from config import DATA_DIR, DB_PATH
from models import AgentPlan, LockedPlan, PlanReview, SimulationResult


def _enforce_path(path: Path) -> Path:
    """Verilen path DATA_DIR içinde mi kontrol eder, değilse hata verir."""
    resolved = path.resolve()
    data_dir_resolved = DATA_DIR.resolve()

    # Python 3.9 uyumlu is_relative_to alternatifi
    try:
        resolved.relative_to(data_dir_resolved)
    except ValueError:
        raise PermissionError(
            f"Güvenlik ihlali: '{resolved}' DATA_DIR dışında."
        )
    return resolved


class Storage:
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = _enforce_path(db_path)
        self._init_db()

    def _connect(self):
        return sqlite3.connect(self.db_path)

    def _init_db(self):
        # DATA_DIR yoksa oluştur
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_text TEXT NOT NULL,
                    plan_json TEXT,
                    review_json TEXT,
                    plan_hash TEXT,
                    canonical_plan_json TEXT,
                    simulation_json TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()

    def save_run(
        self,
        user_text: str,
        plan: AgentPlan,
        review: PlanReview,
        locked: Optional[LockedPlan] = None,
        simulation: Optional[SimulationResult] = None
    ):
        plan_json = json.dumps(plan.model_dump(), ensure_ascii=False)
        review_json = json.dumps(review.model_dump(), ensure_ascii=False)

        # LockedPlan içindeki canonical_plan_json zaten JSON string, tekrar dump etme
        plan_hash = locked.plan_hash if locked else None
        canonical_plan_json = locked.canonical_plan_json if locked else None

        simulation_json = (
            json.dumps(simulation.model_dump(), ensure_ascii=False)
            if simulation else None
        )

        with self._connect() as conn:
            conn.execute("""
                INSERT INTO runs (
                    user_text,
                    plan_json,
                    review_json,
                    plan_hash,
                    canonical_plan_json,
                    simulation_json
                )
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                user_text,
                plan_json,
                review_json,
                plan_hash,
                canonical_plan_json,
                simulation_json
            ))
            conn.commit()