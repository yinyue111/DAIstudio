"""Seed model config and bootstrap an admin user.

Usage (from backend/ with venv active):
    python -m scripts.init_db --admin-phone 13800000000 --credits 1000
    python -m scripts.init_db --create-tables-dev-only --admin-phone 13800000000

Idempotent: safe to re-run.
"""
from __future__ import annotations

import argparse
import os
import secrets

from app.config import settings
from app.db import Base, SessionLocal, engine
from app.models import PhoneWhitelist, User
from app.security import hash_password
from app.services import credits
from app.services.config_store import seed_from_yaml


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--admin-phone", default="13800000000")
    # No fixed default password — supply via --admin-password or ADMIN_PASSWORD,
    # otherwise a strong one-time password is generated and printed once.
    ap.add_argument("--admin-password", default=None)
    ap.add_argument("--credits", type=int, default=1000)
    ap.add_argument(
        "--create-tables-dev-only",
        action="store_true",
        help="Create tables via SQLAlchemy metadata. Refused when DEBUG=false; production must run Alembic.",
    )
    args = ap.parse_args()

    provided_password = args.admin_password or os.environ.get("ADMIN_PASSWORD")
    generated = None
    if not provided_password:
        provided_password = secrets.token_urlsafe(12)
        generated = provided_password

    if args.create_tables_dev_only:
        if not settings.debug:
            raise SystemExit("--create-tables-dev-only is only allowed when DEBUG=true; run alembic upgrade head first")
        Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    try:
        seed_from_yaml(db)

        phone = args.admin_phone
        if not db.get(PhoneWhitelist, phone):
            db.add(PhoneWhitelist(phone=phone, note="bootstrap admin",
                                  department="平台"))
            db.commit()

        password_set = False
        user = db.query(User).filter(User.phone == phone).first()
        if not user:
            user = User(phone=phone, nickname="管理员", status="active",
                        is_admin=True, department="平台",
                        password_hash=hash_password(provided_password))
            db.add(user)
            db.commit()
            db.refresh(user)
            password_set = True
        else:
            user.is_admin = True
            user.status = "active"
            if not user.password_hash:
                user.password_hash = hash_password(provided_password)
                password_set = True
            db.commit()

        if args.credits > 0 and user.balance_credits < args.credits:
            credits.grant(db, user.id, args.credits - user.balance_credits,
                          note="bootstrap grant")

        balance = db.get(User, user.id).balance_credits
        print(f"[ok] admin ready: phone={phone} is_admin=True balance={balance}")
        # Only ever print a password we GENERATED, and only once. Never echo an
        # operator-supplied password back to logs.
        if generated and password_set:
            print("[ok] generated one-time admin password (save now, shown once, "
                  f"change after first login):\n      {generated}")
        elif password_set and not generated:
            print("[ok] admin password set from the provided value.")
        else:
            print("[ok] admin already had a password; left unchanged.")
        print("[ok] model_configs seeded. Edit via admin UI or models.yaml.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
