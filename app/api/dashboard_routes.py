# app/api/dashboard_routes.py
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from jose import jwt, JWTError
from app.schemas.account_schema import AccountLotSchema, AddAccountRequest
from app.database import get_db
from app.model import User, UserPermission, UserNotificationPrefs
from app.auth import SECRET_KEY, ALGORITHM, get_current_user, security
from app.services.account_management import account_manager

router = APIRouter(prefix="/dashboard", tags=["Dashboard"])


# =========================
# DASHBOARD ROUTE (Protected)
# =========================
@router.get("/")
async def dashboard(
    credentials: HTTPAuthorizationCredentials = Depends(security), 
    db: Session = Depends(get_db)
):
    try:
        payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("user_id")

        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token payload")

        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        permission = db.query(UserPermission).filter(UserPermission.user_id == user_id).first()
        can_trade        = permission.can_trade        if permission else False
        profit_share_pct = permission.profit_share_pct if permission else 50.0

        return {
            "message": "Welcome to Hedge Bridge Dashboard",
            "username": user.username,
            "role": user.role,
            "approval_status": user.approval_status,
            "can_trade": can_trade,
            "profit_share_pct": profit_share_pct,
        }

    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")
    except Exception as e:
        raise HTTPException(status_code=500, detail="Internal server error")
    
    
@router.get("/my-slots")
def get_my_slots(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
):
    payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
    user_id = payload.get("user_id")
    from app.model import UserSlot, VpsAccount, TradingAccount, SlotSymbolMap

    slots = (
        db.query(UserSlot)
        .filter_by(user_id=user_id)
        .order_by(UserSlot.slot_number)
        .all()
    )

    out = []
    for s in slots:
        vps    = db.query(VpsAccount).get(s.vps_id)             if s.vps_id            else None
        master = db.query(TradingAccount).get(s.master_account_id) if s.master_account_id else None
        slave  = db.query(TradingAccount).get(s.slave_account_id)  if s.slave_account_id  else None
        maps   = db.query(SlotSymbolMap).filter_by(slot_id=s.id).order_by(SlotSymbolMap.id).all()

        out.append({
            "id":             s.id,
            "slot_number":    s.slot_number,
            "status":         s.status,
            "vps_host":       vps.host if vps else None,
            "master_account": _account_summary(master) if master else None,
            "slave_account":  _account_summary(slave)  if slave  else None,
            "symbol_maps":    [{"id": m.id, "master_symbol": m.master_symbol, "slave_symbol": m.slave_symbol} for m in maps],
        })

    return {"slots": out}


# ── Symbol map CRUD ──────────────────────────────────────────────

@router.post("/my-slots/{slot_id}/symbol-maps")
def add_symbol_map(
    slot_id: int,
    body: dict,
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
):
    from app.model import UserSlot, SlotSymbolMap

    payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
    user_id = payload.get("user_id")

    slot = db.query(UserSlot).filter_by(id=slot_id, user_id=user_id).first()
    if not slot:
        raise HTTPException(404, "Slot not found")

    master_symbol = (body.get("master_symbol") or "").strip().upper()
    slave_symbol  = (body.get("slave_symbol")  or "").strip().upper()
    if not master_symbol or not slave_symbol:
        raise HTTPException(400, "master_symbol and slave_symbol are required")

    entry = SlotSymbolMap(slot_id=slot_id, master_symbol=master_symbol, slave_symbol=slave_symbol)
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return {"id": entry.id, "master_symbol": entry.master_symbol, "slave_symbol": entry.slave_symbol}


@router.get("/my-slots/{slot_id}/metrics")
async def get_slot_metrics(
    slot_id: int,
    role: str = "master",
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
):
    from app.model import UserSlot, TradingAccount

    payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
    user_id = payload.get("user_id")

    slot = db.query(UserSlot).filter_by(id=slot_id, user_id=user_id).first()
    if not slot:
        raise HTTPException(404, "Slot not found")

    account_col_id = slot.master_account_id if role == "master" else slot.slave_account_id
    if not account_col_id:
        raise HTTPException(404, f"No {role} account linked to this slot")

    account = db.query(TradingAccount).get(account_col_id)
    if not account or not account.metaapi_account_id:
        raise HTTPException(400, "Account not registered with MetaAPI — deploy it first")

    metrics = await account_manager.get_account_metrics(account.metaapi_account_id)
    return {"metrics": metrics, "account_id": account.id, "login": account.login}


@router.post("/my-slots/{slot_id}/close-position")
async def close_slot_position(
    slot_id: int,
    body: dict,
    role: str = "master",
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
):
    from app.model import UserSlot, TradingAccount

    payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
    user_id = payload.get("user_id")

    slot = db.query(UserSlot).filter_by(id=slot_id, user_id=user_id).first()
    if not slot:
        raise HTTPException(404, "Slot not found")

    account_col_id = slot.master_account_id if role == "master" else slot.slave_account_id
    if not account_col_id:
        raise HTTPException(404, f"No {role} account linked to this slot")

    account = db.query(TradingAccount).get(account_col_id)
    if not account or not account.metaapi_account_id:
        raise HTTPException(400, "Account not registered with MetaAPI")

    position_id = str(body.get("position_id") or "").strip()
    if not position_id:
        raise HTTPException(400, "position_id is required")

    result = await account_manager.close_position(account.metaapi_account_id, position_id)
    if not result.get("success"):
        raise HTTPException(400, result.get("message", "Failed to close position"))
    return {"success": True}


@router.delete("/my-slots/{slot_id}/symbol-maps/{map_id}")
def delete_symbol_map(
    slot_id: int,
    map_id: int,
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
):
    from app.model import UserSlot, SlotSymbolMap

    payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
    user_id = payload.get("user_id")

    slot = db.query(UserSlot).filter_by(id=slot_id, user_id=user_id).first()
    if not slot:
        raise HTTPException(404, "Slot not found")

    entry = db.query(SlotSymbolMap).filter_by(id=map_id, slot_id=slot_id).first()
    if not entry:
        raise HTTPException(404, "Mapping not found")

    db.delete(entry)
    db.commit()
    return {"message": "Mapping deleted"}
 
 
@router.get("/notification-prefs")
def get_my_notification_prefs(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
):
    payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
    user_id = payload.get("user_id")

    prefs = db.query(UserNotificationPrefs).filter_by(user_id=user_id).first()
    if not prefs:
        prefs = UserNotificationPrefs(user_id=user_id)
        db.add(prefs)
        db.commit()
        db.refresh(prefs)

    return {
        "telegram_link_token": prefs.telegram_link_token,
        "telegram_linked":     bool(prefs.telegram_chat_id),
        "notify_telegram":     prefs.notify_telegram,
        "notify_email":        prefs.notify_email,
    }


@router.patch("/notification-prefs")
def update_my_notification_prefs(
    data: dict,
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
):
    payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
    user_id = payload.get("user_id")

    prefs = db.query(UserNotificationPrefs).filter_by(user_id=user_id).first()
    if not prefs:
        prefs = UserNotificationPrefs(user_id=user_id)
        db.add(prefs)

    if "notify_telegram" in data:
        prefs.notify_telegram = bool(data["notify_telegram"])
    if "notify_email" in data:
        prefs.notify_email    = bool(data["notify_email"])

    db.commit()
    return {"message": "Preferences saved."}


def _account_summary(acct):
    return {
        "id":                acct.id,
        "name":              acct.name,
        "login":             acct.login,
        "server":            acct.server,
        "connection_status": acct.connection_status,
        "state":             acct.state,
        "has_metaapi":       bool(acct.metaapi_account_id),
    }
 
 
# ── User: add a trading account to a slot ───────────────────────

@router.post("/my-slots/{slot_id}/add-account")
async def add_account_to_slot(
    slot_id: int,
    body: AddAccountRequest,
    db: Session = Depends(get_db),
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    from app.model import UserSlot, TradingAccount, CopyRelationship

    if body.role not in ("master", "slave"):
        raise HTTPException(400, "role must be 'master' or 'slave'")

    payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
    user_id = payload.get("user_id")

    slot = db.query(UserSlot).filter_by(id=slot_id, user_id=user_id).first()
    if not slot:
        raise HTTPException(404, "Slot not found")
    if slot.status == "pending":
        raise HTTPException(400, "VPS not yet provisioned for this slot")

    if body.role == "master" and slot.master_account_id:
        raise HTTPException(400, "Master account already linked to this slot")
    if body.role == "slave" and slot.slave_account_id:
        raise HTTPException(400, "Slave account already linked to this slot")

    # Register with MetaAPI cloud first
    reg = await account_manager.add_account(
        name=body.name,
        server=body.server,
        login=str(body.login),
        password=body.password,
        manual_trades=body.manual_trades,
        use_dedicated_ip=body.use_dedicated_ip,
        magic=slot_id * 1000 + slot.slot_number,
    )
    if not reg.get("success"):
        raise HTTPException(400, f"MetaAPI registration failed: {reg.get('message')}")

    metaapi_account_id = reg.get("account_id")

    # Reuse existing DB record if same login already belongs to this user
    existing = db.query(TradingAccount).filter_by(login=body.login, owner_user_id=user_id).first()
    if existing:
        existing.name             = body.name
        existing.server           = body.server
        existing.password         = body.password
        existing.manual_trades    = body.manual_trades
        existing.use_dedicated_ip = body.use_dedicated_ip
        existing.metaapi_account_id = metaapi_account_id
        existing.state            = "created"
        acct = existing
    else:
        acct = TradingAccount(
            owner_user_id     = user_id,
            name              = body.name,
            login             = body.login,
            password          = body.password,
            server            = body.server,
            manual_trades     = body.manual_trades,
            use_dedicated_ip  = body.use_dedicated_ip,
            magic             = slot_id * 1000 + slot.slot_number,
            metaapi_account_id= metaapi_account_id,
            state             = "created",
        )
        db.add(acct)

    db.flush()

    if body.role == "master":
        slot.master_account_id = acct.id
    else:
        slot.slave_account_id = acct.id

    if slot.master_account_id and slot.slave_account_id:
        existing_rel = db.query(CopyRelationship).filter_by(
            master_account_id=slot.master_account_id,
            slave_account_id=slot.slave_account_id,
        ).first()
        if not existing_rel:
            db.add(CopyRelationship(
                master_account_id=slot.master_account_id,
                slave_account_id=slot.slave_account_id,
                copy_direction="opposite",
                is_active=True,
            ))
        slot.status = "active"

    slot.updated_at = datetime.utcnow()
    db.commit()

    return {"message": f"{body.role.capitalize()} account connected to slot {slot.slot_number}.", "account_id": acct.id}
    