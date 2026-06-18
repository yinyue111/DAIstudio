"""Purge generated assets past the retention window (default 30 days).

Run from backend/ with the venv active:
    python -m scripts.cleanup_expired

Schedule daily, e.g. crontab:
    0 3 * * * cd /path/to/backend && ./.venv/bin/python -m scripts.cleanup_expired
"""
from __future__ import annotations

from app.db import SessionLocal
from app.services import retention


def main():
    db = SessionLocal()
    try:
        result = retention.purge_all(db)
        print(f"[cleanup] purged {result} (asset retention="
              f"{retention.get_retention_days(db)} days)")
    finally:
        db.close()


if __name__ == "__main__":
    main()
