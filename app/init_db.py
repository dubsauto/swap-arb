# app/init_db.py

from sqlalchemy.orm import sessionmaker
from app.model import User, UserPermission, ReferralCode, Base
from app.auth import hash_password
from app.database import engine
from datetime import datetime
import asyncio

SessionLocal = sessionmaker(bind=engine)


async def create_default_admins(db):
    admins = [
        {
            "username":   "alessandro",
            "first_name": "Alessandro",
            "last_name":  "Admin",
            "password":   "alessandro_dashboard_786786",
        },
        {
            "username":   "adabs",
            "first_name": "Adabs",
            "last_name":  "Admin",
            "password":   "hedge001",
        },
    ]

    for admin in admins:
        existing = db.query(User).filter(User.username == admin["username"]).first()

        if existing:
            print(f"⚠️  Admin '{admin['username']}' already exists — skipping")
            continue

        user = User(
            username         = admin["username"],
            first_name       = admin["first_name"],
            last_name        = admin["last_name"],
            email            = None,
            password_hash    = hash_password(admin["password"]),
            role             = "admin",
            approval_status  = "approved",
            onboarding_step  = "approved",   # admins skip the onboarding flow
            approved_at      = datetime.utcnow(),
            created_at       = datetime.utcnow(),
        )

        db.add(user)
        db.flush()   # populate user.id before creating related rows

        permission = UserPermission(
            user_id            = user.id,
            can_trade          = True,
        )
        db.add(permission)

        print(f"✅ Created admin: {admin['username']}")

    db.commit()


async def create_default_referral_codes(db):
    """
    Seed a small set of invite codes for the initial launch.
    Add / remove codes here as needed; existing codes are never
    overwritten so this is safe to re-run.
    """
    codes = [
        {"code": "LAUNCH2024", "max_uses": None},   # unlimited
        {"code": "BETA50",     "max_uses": 50},
        {"code": "VIP10",      "max_uses": 10},
    ]

    for entry in codes:
        existing = db.query(ReferralCode).filter(
            ReferralCode.code == entry["code"]
        ).first()

        if existing:
            print(f"⚠️  Referral code '{entry['code']}' already exists — skipping")
            continue

        ref = ReferralCode(
            code       = entry["code"],
            max_uses   = entry["max_uses"],
            use_count  = 0,
            is_active  = True,
            created_at = datetime.utcnow(),
        )
        db.add(ref)
        print(f"✅ Created referral code: {entry['code']}")

    db.commit()


async def init_database():
    print("🚀 Initializing database...")

    Base.metadata.create_all(bind=engine)
    print("✅ All tables created or already exist.")

    db = SessionLocal()
    try:
        await create_default_admins(db)
        await create_default_referral_codes(db)
    finally:
        db.close()

    print("✅ Database ready.")


def init_database_sync():
    """Use this only if needed from a non-async context."""
    asyncio.run(init_database())

# if __name__ == "__main__":
#     init_database()
