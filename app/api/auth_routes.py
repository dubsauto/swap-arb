# app/api/auth_routes.py
import os
from fastapi import APIRouter, Depends, HTTPException, Header
from sqlalchemy.orm import Session
from datetime import datetime
from dotenv import load_dotenv
from typing import Optional

from app.database import get_db
from app.model import User, UserPermission, ReferralCode
from app.auth import verify_password, create_access_token, hash_password, decode_token

load_dotenv()

router = APIRouter(prefix="", tags=["Authentication"])


# =========================
# HELPERS
# =========================

def get_current_user(
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
) -> User:
    """
    Resolves the logged-in user from the Bearer token in the
    Authorization header. Raises 401 if missing or invalid.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")

    token   = authorization.split(" ", 1)[1]
    payload = decode_token(token)

    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user = db.query(User).filter(User.id == payload.get("user_id")).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    return user


# =========================
# LOGIN
# =========================

@router.post("/login")
async def login(
    data: dict,
    db: Session = Depends(get_db),
):
    identifier = data.get("identifier")
    password   = data.get("password")

    if not identifier or not password:
        raise HTTPException(status_code=400, detail="Missing credentials")

    user = db.query(User).filter(
        (User.username == identifier) | (User.email == identifier)
    ).first()

    if not user or not verify_password(password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Rejected accounts cannot log in at all
    if user.approval_status == "rejected":
        raise HTTPException(
            status_code=403,
            detail="Your account application was not approved. Please contact support.",
        )

    # ── Pending AND approved users both receive a token ──────────
    # The frontend calls /me after login and uses approval_status +
    # onboarding_step to decide whether to go to /onboarding or /app.
    # We no longer block pending users here.

    token = create_access_token({"user_id": user.id, "role": user.role})

    return {
        "access_token": token,
        "role":         user.role,
    }


# =========================
# SIGNUP
# =========================

@router.post("/signup")
async def signup(
    data: dict,
    db: Session = Depends(get_db),
):
    first_name    = (data.get("first_name")    or "").strip()
    last_name     = (data.get("last_name")     or "").strip()
    email         = (data.get("email")         or "").strip().lower()
    password      =  data.get("password")      or ""
    referral_code = (data.get("referral_code") or "").strip() or None

    # ── validation ───────────────────────────────────────────────
    if not first_name or not last_name:
        raise HTTPException(status_code=400, detail="First name and last name are required")
    if not email:
        raise HTTPException(status_code=400, detail="Email is required")
    if not password or len(password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

    # ── duplicate check ──────────────────────────────────────────
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(status_code=400, detail="An account with this email already exists")

    # ── referral code (optional) ─────────────────────────────────
    if referral_code:
        ref = db.query(ReferralCode).filter(
            ReferralCode.code      == referral_code,
            ReferralCode.is_active == True,
        ).first()
        if not ref:
            raise HTTPException(status_code=400, detail="Referral code is invalid or has expired")
        if ref.max_uses is not None and ref.use_count >= ref.max_uses:
            raise HTTPException(status_code=400, detail="Referral code has reached its usage limit")
        ref.use_count += 1

    # ── derive a unique username from the email prefix ───────────
    base     = email.split("@")[0].replace(".", "_").replace("-", "_")
    username = base
    counter  = 1
    while db.query(User).filter(User.username == username).first():
        username = f"{base}_{counter}"
        counter += 1

    # ── create user ──────────────────────────────────────────────
    new_user = User(
        username        = username,
        first_name      = first_name,
        last_name       = last_name,
        email           = email,
        password_hash   = hash_password(password),
        referral_code   = referral_code,
        role            = "user",
        approval_status = "pending",
        onboarding_step = "registered",
        created_at      = datetime.utcnow(),
    )
    db.add(new_user)
    db.flush()

    db.add(UserPermission(
        user_id            = new_user.id,
        can_trade          = True,
    ))
    db.commit()

    return {
        "message": "Account created successfully! Please sign in to continue setup.",
        "user_id": new_user.id,
    }


# =========================
# CURRENT USER PROFILE  (/me)
# =========================

@router.get("/me")
async def get_me(
    current_user: User = Depends(get_current_user),
):
    """
    Returns the logged-in user's profile.

    The frontend uses this immediately after login to route:
      approval_status == "approved"  →  /app         (dashboard)
      anything else                  →  /onboarding  (setup flow)

    onboarding.html also calls this on load to resume at the correct step.
    """
    return {
        "id":                   current_user.id,
        "username":             current_user.username,
        "first_name":           current_user.first_name,
        "last_name":            current_user.last_name,
        "email":                current_user.email,
        "role":                 current_user.role,
        "approval_status":      current_user.approval_status,
        "onboarding_step":      current_user.onboarding_step,
        "notification_contact": current_user.notification_contact,
    }


# =========================
# ONBOARDING PROGRESS
# =========================

_VALID_STEPS = {"brokers_registered", "vps_activated", "accounts_submitted"}


@router.patch("/onboarding/step")
async def update_onboarding_step(
    data: dict,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Records which onboarding step the logged-in user has completed
    and optionally saves their notification contact.

    Body:
        step                 – brokers_registered | vps_activated | accounts_submitted
        notification_contact – str (optional) Telegram handle or email
    """
    step                 = (data.get("step")                 or "").strip()
    notification_contact = (data.get("notification_contact") or "").strip() or None

    if not step:
        raise HTTPException(status_code=400, detail="step is required")

    if step not in _VALID_STEPS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid step. Must be one of: {', '.join(sorted(_VALID_STEPS))}",
        )

    if current_user.approval_status == "approved":
        raise HTTPException(status_code=400, detail="Onboarding already complete")

    current_user.onboarding_step = step
    if notification_contact:
        current_user.notification_contact = notification_contact

    db.commit()

    return {"ok": True, "onboarding_step": step}