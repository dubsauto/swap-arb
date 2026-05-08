# app/api/admin_routes.py
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from jose import jwt, JWTError
from datetime import datetime
from app.database import get_db
import time
from app.auth import SECRET_KEY, ALGORITHM, security
from app.auth import hash_password
from app.model import (
    User,
    UserPermission,
    ActivityLog,
    ActiveUser,
    UserSlot,
    VpsAccount,
    NotificationSettings,
    UserNotificationPrefs,
    NotificationLog,
    DEFAULT_NOTIFICATION_TEMPLATE,
)
from app.schemas.account_schema import AllocateSlotRequest

router = APIRouter(prefix="/admin", tags=["Admin"])


# ========================
# ADMIN - PROFILE MANAGEMENT
# ========================

@router.get("/profiles")
async def get_profiles(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db)
):
    try:
        payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        current_user_id = payload.get("user_id")
        current_user = db.query(User).filter_by(id=current_user_id).first()
        if current_user.role != "admin":
            raise HTTPException(status_code=403, detail="Admin access only")
        users = db.query(User).all()

        all_users = []
        pending_users = []
        def user_to_dict(user):
            slots_db = db.query(UserSlot).filter_by(user_id=user.id).all()
            slots_out = []
            for s in slots_db:
                vps = db.query(VpsAccount).get(s.vps_id) if s.vps_id else None
                slots_out.append({
                    "slot_number": s.slot_number,
                    "status":      s.status,
                    "vps_host":    vps.host if vps else None,
                    "vps_username": vps.username if vps else None,
                })
            perm = db.query(UserPermission).filter_by(user_id=user.id).first()
            can_trade        = True  if not perm else perm.can_trade
            profit_share_pct = 50.0 if not perm else (perm.profit_share_pct or 50.0)
            return {
                "id":               user.id,
                "username":         user.username,
                "email":            user.email,
                "full_name":        user.full_name,
                "role":             user.role,
                "approval_status":  user.approval_status,
                "can_trade":        can_trade,
                "profit_share_pct": profit_share_pct,
                "slots_requested":  user.slots_requested,
                "slots_allocated":  len([s for s in slots_db if s.status != 'pending']),
                "slots":            slots_out,
            }
        for user in users:
            # ❌ Skip current admin
            if user.id == current_user_id:
                continue
            user_data = user_to_dict(user)

            all_users.append(user_data)

            if user.approval_status == "pending":
                pending_users.append(user_data)

        return {
            "all_users": all_users,
            "pending_users": pending_users,
            "pending_count": len(pending_users)
        }

    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")



@router.post("/approve")
async def approve_user(
    data: dict,
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db)
):
    try:
        payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        current_user_id = payload.get("user_id")
        current_user = db.query(User).filter_by(id=current_user_id).first()
        if current_user.role != "admin":
            raise HTTPException(status_code=403, detail="Admin access only")

        user_id = data.get("user_id")
        decision = data.get("decision")   # "approve" or "decline"
        note = data.get("approval_note")

        if not user_id or decision not in ["approve", "decline"]:
            raise HTTPException(status_code=400, detail="Invalid request")

        status = "approved" if decision == "approve" else "declined"

        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        user.approval_status = status
        user.approval_note = note
        user.approved_by = str(payload.get("user_id"))   # store as string for safety
        user.approved_at = datetime.utcnow()

        db.commit()

        return {"message": f"User {user.username} has been {status}"}

    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")
    

@router.get("/activity")
def get_activity(hours: int = 24, db: Session = Depends(get_db), credentials: HTTPAuthorizationCredentials = Depends(security)):

    # =========================
    # 🔒 ADMIN ONLY
    # =========================
    payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
    current_user_id = payload.get("user_id")
    current_user = db.query(User).filter_by(id=current_user_id).first()
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access only")

    now_ts = int(time.time())
    cutoff_ts = now_ts - (hours * 3600)

    # =========================
    # 🟢 ACTIVE USERS
    # =========================
    ACTIVE_WINDOW = 60  # seconds (last seen within 60s = active)

    active_users = db.query(ActiveUser).filter(
        ActiveUser.last_seen >= (now_ts - ACTIVE_WINDOW),
        ActiveUser.online == True
    ).all()

    active_now = []
    for u in active_users:
        active_now.append({
            "username": u.username,
            "role": u.role,
            "page": u.page,
            "action": u.action
        })

    # =========================
    # 🔴 RECENTLY OFFLINE
    # =========================
    offline_users = db.query(ActiveUser).filter(
        ActiveUser.last_seen < (now_ts - ACTIVE_WINDOW)
    ).order_by(ActiveUser.last_seen.desc()).limit(10).all()

    def time_ago(ts):
        diff = now_ts - ts
        if diff < 60:
            return f"{diff}s ago"
        elif diff < 3600:
            return f"{diff // 60}m ago"
        elif diff < 86400:
            return f"{diff // 3600}h ago"
        else:
            return f"{diff // 86400}d ago"

    offline = []
    for u in offline_users:
        offline.append({
            "username": u.username,
            "role": u.role,
            "page": u.page,
            "last_seen_ago": time_ago(u.last_seen)
        })

    # =========================
    # 📜 HISTORY LOGS
    # =========================
    logs = db.query(ActivityLog).filter(
        ActivityLog.ts >= cutoff_ts
    ).order_by(ActivityLog.ts.desc()).limit(100).all()

    history = []
    for log in logs:
        history.append({
            "username": log.username,
            "action": log.action,
            "page": log.page,
            "time_ago": time_ago(log.ts)
        })

    return {
        "active_now": active_now,
        "offline": offline,
        "history": history,
        "active_count": len(active_now)
    }


@router.post("/activity/heartbeat")
async def heartbeat(
    request: Request,
    db: Session = Depends(get_db),
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    try:
        payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")

    user_id = payload.get("user_id")

    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token payload")

    # ✅ Fetch real user
    user = db.query(User).filter(User.id == user_id).first()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    now_ts = int(time.time())
    ip = request.client.host if request.client else None

    body = await request.json()
    page = body.get("page", "dashboard")
    action = body.get("action", "heartbeat")

    # =========================
    # UPSERT ACTIVE USER
    # =========================
    existing = db.query(ActiveUser).filter_by(username=user.username).first()

    if existing:
        existing.page = page
        existing.action = action
        existing.last_seen = now_ts
        existing.online = True
        existing.ip = ip
    else:
        db.add(ActiveUser(
            username=user.username,
            role=user.role,
            page=page,
            action=action,
            ip=ip,
            last_seen=now_ts,
            online=True
        ))

    # =========================
    # LOG HISTORY (OPTIONAL THROTTLE)
    # =========================
    db.add(ActivityLog(
        ts=now_ts,
        username=user.username,
        role=user.role,
        page=page,
        action=action,
        ip=ip
    ))

    db.commit()

    return {"status": "ok"}

@router.post("/allocate-slot")
def admin_allocate_slot(
    body: AllocateSlotRequest,
    db: Session = Depends(get_db),
    credentials: HTTPAuthorizationCredentials = Depends(security),         # your admin auth dep
):
    """
    Admin calls this after confirming payment.
    Creates a VpsAccount row and a UserSlot row (or updates existing).
    """
    from app.model import VpsAccount, UserSlot
    payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
    current_user_id = payload.get("user_id")
    current_user = db.query(User).filter_by(id=current_user_id).first()
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access only")
 
    # 1. Create the VPS record
    vps = VpsAccount(
        owner_user_id        = body.user_id,
        host                 = body.vps_host,
        username             = body.vps_username,
        password             = body.vps_password,  # encrypt in production!
        port                 = body.vps_port,
        subscription_status  = "active",
        subscription_since   = datetime.utcnow(),
    )
    db.add(vps)
    db.flush()  # get vps.id without committing
 
    # 2. Upsert the UserSlot row
    slot = db.query(UserSlot).filter_by(
        user_id     = body.user_id,
        slot_number = body.slot_number,
    ).first()
 
    if slot:
        slot.vps_id    = vps.id
        slot.status    = "provisioned"
        slot.updated_at = datetime.utcnow()
    else:
        slot = UserSlot(
            user_id     = body.user_id,
            slot_number = body.slot_number,
            vps_id      = vps.id,
            status      = "provisioned",
        )
        db.add(slot)
 
    db.commit()
    return {"message": f"Slot {body.slot_number} provisioned for user {body.user_id}."}
 
 
# ── Admin: get slots for a specific user (for modal) ────────────
 
@router.get("/user-slots/{user_id}")
def admin_get_user_slots(
    user_id: int,
    db: Session = Depends(get_db),
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
    current_user_id = payload.get("user_id")

    current_user = db.query(User).filter_by(id=current_user_id).first()
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access only")
 
    user = db.query(User).get(user_id)
    if not user:
        raise HTTPException(404, "User not found")
 
    slots = db.query(UserSlot).filter_by(user_id=user_id).all()
 
    slots_out = []
    for s in slots:
        vps = db.query(VpsAccount).get(s.vps_id) if s.vps_id else None
        slots_out.append({
            "slot_number": s.slot_number,
            "status":      s.status,
            "vps_host":    vps.host     if vps else None,
            "vps_username": vps.username if vps else None,
        })
 
    return {
        "user": {
            "id":              user.id,
            "username":        user.username,
            "slots_requested": user.slots_requested,
        },
        "slots": slots_out,
    }


@router.post("/update-user")
async def update_user(
    data: dict,
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
):
    payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
    current_user_id = payload.get("user_id")
    current_user = db.query(User).filter_by(id=current_user_id).first()
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access only")

    user_id = data.get("user_id")
    user = db.query(User).filter_by(id=user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if "role" in data:
        user.role = data["role"]

    if "password" in data and data["password"]:
        user.password_hash = hash_password(data["password"])

    # Upsert UserPermission
    perm = db.query(UserPermission).filter_by(user_id=user_id).first()
    if not perm:
        perm = UserPermission(user_id=user_id)
        db.add(perm)

    if "can_trade" in data:
        perm.can_trade = bool(data["can_trade"])

    if "profit_share_pct" in data and data["profit_share_pct"] is not None:
        pct = float(data["profit_share_pct"])
        if not (0 <= pct <= 100):
            raise HTTPException(400, "profit_share_pct must be 0–100")
        perm.profit_share_pct = pct

    db.commit()
    return {"message": f"User {user.username} updated successfully."}


@router.post("/reset-password")
async def reset_password(
    data: dict,
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db)
):
    try:
        payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        current_user_id = payload.get("user_id")
        current_user = db.query(User).filter_by(id=current_user_id).first()
        if current_user.role != "admin":
            raise HTTPException(status_code=403, detail="Admin access only")

        user_id = data.get("user_id")
        new_password = data.get("password")

        if not user_id or not new_password:
            raise HTTPException(status_code=400, detail="Missing fields")

        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        user.password_hash = hash_password(new_password)

        db.commit()

        return {"message": f"Password updated for {user.username}"}

    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")
    

@router.delete("/delete-user/{user_id}")
async def delete_user(
    user_id: int,
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db)
):
    try:
        # =========================
        # AUTH CHECK
        # =========================
        payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        current_user_id = payload.get("user_id")
        current_user = db.query(User).filter_by(id=current_user_id).first()
        if current_user.role != "admin":
            raise HTTPException(status_code=403, detail="Admin access only")

        current_admin_id = payload.get("user_id")

        # Prevent self-delete
        if user_id == current_admin_id:
            raise HTTPException(status_code=400, detail="You cannot delete yourself")

        # =========================
        # GET USER
        # =========================
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        # =========================
        # DELETE USER
        # =========================
        db.delete(user)

        db.commit()

        return {
            "success": True,
            "message": f"User '{user.username}' and all related data deleted successfully"
        }

    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")

    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    
    


# ─────────────────────────────────────────────────────────────────────────────
# NOTIFICATION SETTINGS
# ─────────────────────────────────────────────────────────────────────────────

def _require_admin(credentials, db):
    payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
    user_id = payload.get("user_id")
    user = db.query(User).filter_by(id=user_id).first()
    if not user or user.role != "admin":
        raise HTTPException(403, "Admin access only")
    return user_id


@router.get("/notification-settings")
def get_notification_settings(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
):
    _require_admin(credentials, db)
    row = db.query(NotificationSettings).first()
    if not row:
        return {
            "margin_threshold_pct":   50.0,
            "check_interval_minutes": 15,
            "message_template":       DEFAULT_NOTIFICATION_TEMPLATE,
        }
    return {
        "margin_threshold_pct":   row.margin_threshold_pct,
        "check_interval_minutes": row.check_interval_minutes,
        "message_template":       row.message_template or DEFAULT_NOTIFICATION_TEMPLATE,
    }


@router.post("/notification-settings")
def update_notification_settings(
    data: dict,
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
):
    admin_id = _require_admin(credentials, db)
    row = db.query(NotificationSettings).first()
    if not row:
        row = NotificationSettings()
        db.add(row)

    if "margin_threshold_pct" in data:
        val = float(data["margin_threshold_pct"])
        if not (0 < val < 500):
            raise HTTPException(400, "margin_threshold_pct must be between 0 and 500")
        row.margin_threshold_pct = val

    if "check_interval_minutes" in data:
        val = int(data["check_interval_minutes"])
        if not (1 <= val <= 1440):
            raise HTTPException(400, "check_interval_minutes must be 1–1440")
        row.check_interval_minutes = val

    if "message_template" in data:
        tpl = (data["message_template"] or "").strip()
        if not tpl:
            raise HTTPException(400, "message_template cannot be empty")
        row.message_template = tpl

    row.updated_by = admin_id
    db.commit()
    return {"message": "Notification settings saved."}


@router.get("/notification-settings/preview-placeholders")
def get_template_placeholders(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
):
    _require_admin(credentials, db)
    return {
        "placeholders": [
            "{account_number}  — MT5 login number",
            "{broker}          — Server / broker name",
            "{margin_level}    — Margin level percentage (e.g. 45.2)",
            "{balance}         — Account balance in USD",
            "{equity}          — Account equity in USD",
            "{margin}          — Margin currently in use",
            "{free_margin}     — Available free margin",
        ],
        "default_template": DEFAULT_NOTIFICATION_TEMPLATE,
    }


# ─────────────────────────────────────────────────────────────────────────────
# USER NOTIFICATION PREFS (admin view / set)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/users/{user_id}/notification-prefs")
def get_user_notification_prefs(
    user_id: int,
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
):
    _require_admin(credentials, db)
    prefs = db.query(UserNotificationPrefs).filter_by(user_id=user_id).first()
    if not prefs:
        # auto-create so we can return the link token
        import secrets as _sec
        prefs = UserNotificationPrefs(user_id=user_id)
        db.add(prefs)
        db.commit()
        db.refresh(prefs)
    return {
        "telegram_chat_id":    prefs.telegram_chat_id,
        "telegram_link_token": prefs.telegram_link_token,
        "telegram_linked":     bool(prefs.telegram_chat_id),
        "notify_telegram":     prefs.notify_telegram,
        "notify_email":        prefs.notify_email,
    }


@router.post("/users/{user_id}/notification-prefs")
def update_user_notification_prefs(
    user_id: int,
    data: dict,
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
):
    _require_admin(credentials, db)
    prefs = db.query(UserNotificationPrefs).filter_by(user_id=user_id).first()
    if not prefs:
        prefs = UserNotificationPrefs(user_id=user_id)
        db.add(prefs)

    if "notify_telegram" in data:
        prefs.notify_telegram = bool(data["notify_telegram"])
    if "notify_email" in data:
        prefs.notify_email = bool(data["notify_email"])
    if "telegram_chat_id" in data:
        prefs.telegram_chat_id = data["telegram_chat_id"] or None

    db.commit()
    return {"message": "Notification preferences updated."}


@router.post("/users/{user_id}/notification-prefs/reset-token")
def reset_telegram_link_token(
    user_id: int,
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
):
    import secrets as _sec
    _require_admin(credentials, db)
    prefs = db.query(UserNotificationPrefs).filter_by(user_id=user_id).first()
    if not prefs:
        prefs = UserNotificationPrefs(user_id=user_id)
        db.add(prefs)
    prefs.telegram_link_token = _sec.token_urlsafe(8)[:8].upper()
    prefs.telegram_chat_id    = None   # unlink when token resets
    db.commit()
    return {"telegram_link_token": prefs.telegram_link_token}


@router.get("/notification-logs")
def get_notification_logs(
    limit: int = 50,
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
):
    _require_admin(credentials, db)
    logs = (
        db.query(NotificationLog)
        .order_by(NotificationLog.sent_at.desc())
        .limit(limit)
        .all()
    )
    return {"logs": [
        {
            "id":           l.id,
            "user_id":      l.user_id,
            "account_id":   l.account_id,
            "channel":      l.channel,
            "margin_level": l.margin_level,
            "sent_at":      l.sent_at.isoformat() if l.sent_at else None,
        }
        for l in logs
    ]}


        # # =========================
        # # GET TRADING ACCOUNTS
        # # =========================
        # accounts = db.query(TradingAccount).filter(
        #     TradingAccount.owner_user_id == user_id
        # ).all()

        # account_ids = [a.id for a in accounts]

        # # =========================
        # # METAAPI CLEANUP
        # # =========================
        # for acc in accounts:
        #     if acc.metaapi_account_id:
        #         try:
        #             await account_manager.undeploy(acc.metaapi_account_id)
        #             await account_manager.remove_account(acc.metaapi_account_id)
        #         except Exception as e:
        #             print(f"[MetaAPI Delete Error] {acc.metaapi_account_id}: {e}")

        # # =========================
        # # COPY SYSTEM CLEANUP
        # # =========================
        # if account_ids:
        #     db.query(CopyTradeLink).filter(
        #         (CopyTradeLink.master_account_id.in_(account_ids)) |
        #         (CopyTradeLink.slave_account_id.in_(account_ids))
        #     ).delete(synchronize_session=False)

        #     db.query(CopyRelationship).filter(
        #         (CopyRelationship.master_account_id.in_(account_ids)) |
        #         (CopyRelationship.slave_account_id.in_(account_ids))
        #     ).delete(synchronize_session=False)

        #     db.query(BotLog).filter(
        #         BotLog.account_id.in_(account_ids)
        #     ).delete(synchronize_session=False)

        # # =========================
        # # DELETE USER-RELATED TABLES
        # # =========================
        # db.query(TradingAccount).filter(
        #     TradingAccount.owner_user_id == user_id
        # ).delete(synchronize_session=False)

        # db.query(UserPermission).filter(
        #     UserPermission.user_id == user_id
        # ).delete(synchronize_session=False)

        # db.query(ActivityLog).filter(
        #     ActivityLog.username == user.username
        # ).delete(synchronize_session=False)

        # db.query(ActiveUser).filter(
        #     ActiveUser.username == user.username
        # ).delete(synchronize_session=False)
