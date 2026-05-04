"""Create initial admin user from environment variables."""

import os
import sys

from sqlalchemy.orm import Session

# Ensure project root on path when run as script
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import get_settings
from core.security import hash_password
from db.models import User, UserRole
from db.session import SessionLocal, init_db


def main() -> None:
    init_db()
    settings = get_settings()
    db: Session = SessionLocal()
    try:
        existing = db.query(User).filter(User.username == settings.ADMIN_USERNAME).first()
        if existing:
            print("admin user already exists, skipping")
            return
        u = User(
            username=settings.ADMIN_USERNAME,
            email=settings.ADMIN_EMAIL,
            hashed_password=hash_password(settings.ADMIN_PASSWORD),
            role=UserRole.admin,
        )
        db.add(u)
        db.commit()
        print("admin user created:", settings.ADMIN_USERNAME)
    finally:
        db.close()


if __name__ == "__main__":
    main()
