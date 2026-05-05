# app/model.py

import os
from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Float, DateTime, Text,
    Boolean, ForeignKey, UniqueConstraint
)
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy import create_engine
from sqlalchemy import JSON

Base = declarative_base()

# =========================
# USERS
# =========================
class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    username = Column(String(64), unique=True, nullable=False)

    # --- NEW: real name fields ---
    first_name = Column(String(64), nullable=False, server_default="")
    last_name  = Column(String(64), nullable=False, server_default="")

    email = Column(String(190), unique=True)
    password_hash = Column(String(255), nullable=False)

    # --- NEW: referral ---
    referral_code = Column(String(64), nullable=True)   # code this user was invited with

    role = Column(String(16), default="user")  # admin/user
    approval_status = Column(String(16), default="pending")  # pending/approved/rejected

    approval_note = Column(String(255))
    approved_by = Column(String(64))
    approved_at = Column(DateTime)

    # --- NEW: onboarding progress ---
    # Tracks which step of signup the user has completed so the frontend
    # can resume and the admin can see where each user stands.
    #
    # Values (in order):
    #   "registered"          – completed step 1 (account created)
    #   "brokers_registered"  – completed step 2 (opened broker accounts)
    #   "vps_activated"       – completed step 3 (paid VPS subscription)
    #   "accounts_submitted"  – completed step 4 (sent MT5 details to admin)
    #   "approved"            – admin approved, full dashboard access
    onboarding_step = Column(String(32), default="registered")

    # --- NEW: notification preference (Telegram handle or email) ---
    notification_contact = Column(String(190), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)

    accounts    = relationship("TradingAccount", back_populates="owner")
    #cycle_slots = relationship("CycleSlot",      back_populates="owner")

    # Convenience property
    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip()


# =========================
# USER PERMISSIONS
# =========================
class UserPermission(Base):
    __tablename__ = "user_permissions"

    user_id          = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    can_trade        = Column(Boolean, default=True)
    updated_at       = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# =========================
# REFERRAL CODES
# =========================
class ReferralCode(Base):
    """
    Tracks invite codes so admins can create and manage them,
    and so signups can be attributed to a referrer.
    """
    __tablename__ = "referral_codes"

    id          = Column(Integer, primary_key=True)
    code        = Column(String(64), unique=True, nullable=False)
    created_by  = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    max_uses    = Column(Integer, nullable=True)   # None = unlimited
    use_count   = Column(Integer, default=0)
    is_active   = Column(Boolean, default=True)
    created_at  = Column(DateTime, default=datetime.utcnow)

    creator = relationship("User", foreign_keys=[created_by])


class TradingAccount(Base):
    __tablename__ = "trading_accounts"

    id = Column(Integer, primary_key=True)

    owner_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))

    # Basic Info
    name     = Column(String(255), nullable=False)           # Friendly name
    login    = Column(Integer, nullable=False, unique=True)  # MT5 Login
    password = Column(Text, nullable=False)
    server   = Column(String(255), nullable=False)

    # MetaAPI Integration
    metaapi_account_id = Column(String(255), unique=True, nullable=True)
    region = Column(String(50))
    state  = Column(String(50), default="created")  # created/deployed/undeployed/error

    # Trading Settings
    manual_trades    = Column(Boolean, default=True)
    use_dedicated_ip = Column(Boolean, default=True)

    # Magic number (copy trading)
    magic = Column(Integer, default=0)

    # Connection state
    connection_status  = Column(String(20), default="disconnected")
    last_connected_at  = Column(DateTime, nullable=True)
    last_error         = Column(Text, nullable=True)
    listener_active    = Column(Boolean, default=False)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    owner = relationship("User", back_populates="accounts")

    copy_relationships_as_master = relationship(
        "CopyRelationship",
        foreign_keys="[CopyRelationship.master_account_id]",
        backref="master_account",
    )
    copy_relationships_as_slave = relationship(
        "CopyRelationship",
        foreign_keys="[CopyRelationship.slave_account_id]",
        backref="slave_account",
    )


# =========================
# COPY RELATIONSHIPS (MASTER/SLAVE)
# =========================
class CopyRelationship(Base):
    __tablename__ = "copy_relationships"

    id = Column(Integer, primary_key=True)

    master_account_id = Column(Integer, ForeignKey("trading_accounts.id", ondelete="CASCADE"))
    slave_account_id  = Column(Integer, ForeignKey("trading_accounts.id", ondelete="CASCADE"))

    copy_direction = Column(String(16), default="same")  # same/opposite
    strict_mode    = Column(Boolean, default=False)
    is_active      = Column(Boolean, default=True)

    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("master_account_id", "slave_account_id", name="uniq_copy_pair"),
    )


# =========================
# COPY TRADE LINKS (LIVE TRADE MAPPING)
# =========================
class CopyTradeLink(Base):
    __tablename__ = "copy_trade_links"

    id = Column(Integer, primary_key=True)

    master_account_id = Column(Integer, ForeignKey("trading_accounts.id", ondelete="CASCADE"))
    slave_account_id  = Column(Integer, ForeignKey("trading_accounts.id", ondelete="CASCADE"))

    master_ticket = Column(String(64), nullable=False)
    slave_ticket  = Column(String(64))

    symbol     = Column(String(32))
    trade_type = Column(String(64))  # buy/sell

    volume = Column(Float, default=0)

    status     = Column(String(16), default="open")  # open/closed/error
    last_error = Column(String(255))

    closed_at  = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# =========================
# ACTIVITY LOG
# =========================
class ActivityLog(Base):
    __tablename__ = "activity_log"

    id = Column(Integer, primary_key=True)
    ts = Column(Integer, nullable=False)

    username = Column(String(64), nullable=False)
    role     = Column(String(16), nullable=False)

    page   = Column(String(64), nullable=False)
    action = Column(String(64), nullable=False)

    meta = Column(Text)
    ip   = Column(String(64))

    created_at = Column(DateTime, default=datetime.utcnow)


# =========================
# VPS ACCOUNTS
# =========================
class VpsAccount(Base):
    __tablename__ = "vps_accounts"

    id = Column(Integer, primary_key=True)

    owner_user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    host     = Column(String(255), nullable=False)
    username = Column(String(128), nullable=False)
    password = Column(Text, nullable=False)

    protocol = Column(String(8), default="ssh")
    port     = Column(Integer, nullable=True)

    associated_mt5_id = Column(
        Integer,
        ForeignKey("trading_accounts.id", ondelete="SET NULL"),
        nullable=True,
    )

    auto_connect    = Column(Boolean, default=True)
    is_online       = Column(Boolean, default=False)
    last_checked_at = Column(DateTime, nullable=True)

    # --- NEW: subscription / payment state ---
    # "unpaid" → "active" → "cancelled" / "expired"
    subscription_status = Column(String(16), default="unpaid")
    subscription_since  = Column(DateTime, nullable=True)
    subscription_until  = Column(DateTime, nullable=True)   # next renewal date

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    owner          = relationship("User")
    associated_mt5 = relationship("TradingAccount", foreign_keys=[associated_mt5_id])


# =========================
# ACTIVE USERS (REAL-TIME TRACKING)
# =========================
class ActiveUser(Base):
    __tablename__ = "active_users"

    username = Column(String(64), primary_key=True)

    role = Column(String(16), nullable=False)
    page = Column(String(64), nullable=False)
    action = Column(String(64), nullable=False)

    meta = Column(Text)

    ip = Column(String(64))
    ua = Column(String(255))

    online = Column(Boolean, default=True)

    last_seen = Column(Integer, nullable=False)

    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


 