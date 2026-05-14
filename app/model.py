# app/model.py

import os
import secrets
from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Float, DateTime, Text,
    Boolean, ForeignKey, UniqueConstraint
)
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy import create_engine
from sqlalchemy import JSON

DEFAULT_NOTIFICATION_TEMPLATE = (
    "⚠️ <b>Margin Alert</b> — Account {account_number}\n\n"
    "Your account at <b>{broker}</b> has reached a critical margin level and needs attention.\n\n"
    "📊 Margin Level: <b>{margin_level}%</b>\n"
    "💰 Balance: <b>${balance}</b>\n"
    "📈 Equity: <b>${equity}</b>\n"
    "🔒 Margin Used: <b>${margin}</b>\n"
    "💵 Free Margin: <b>${free_margin}</b>\n\n"
    "<b>Action Required:</b>\n"
    "• Withdraw profits from your <b>MASTER account</b> (Broker A — the profitable side)\n"
    "• Deposit those funds into your <b>SLAVE account</b> (Broker B — the losing side)\n\n"
    "This rebalance will keep your swap-arbitrage strategy alive and continue generating profits. 🚀"
)

Base = declarative_base()

# =========================
# USERS
# =========================
class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    username = Column(String(64), unique=True, nullable=False)
    first_name = Column(String(64), nullable=False, server_default="")
    last_name  = Column(String(64), nullable=False, server_default="")
    email = Column(String(190), unique=True)
    password_hash = Column(String(255), nullable=False)
    referral_code = Column(String(64), nullable=True)   # code this user was invited with
    role = Column(String(16), default="user")  # admin/user
    approval_status = Column(String(16), default="pending")  # pending/approved/rejected
    approval_note = Column(String(255))
    approved_by = Column(String(64))
    approved_at = Column(DateTime)
    onboarding_step = Column(String(32), default="registered")
    slots_requested = Column(Integer, nullable=True)
    notification_contact = Column(String(190), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    accounts    = relationship("TradingAccount", back_populates="owner")
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
    profit_share_pct = Column(Float, default=50.0)   # platform's share of cycle swap profit
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

    copy_direction = Column(String(16), default="opposite")  # same/opposite
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
# USER SLOTS
# =========================
class UserSlot(Base):
    """
    One row per slot purchased by a user.
    Admin creates these rows after confirming payment.
    Each slot maps to one VPS and expects exactly two TradingAccounts
    (one master, one slave) to be linked to it.
    """
    __tablename__ = "user_slots"
 
    id            = Column(Integer, primary_key=True)
    user_id       = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
 
    # Which slot number for this user (1, 2, 3…)
    slot_number   = Column(Integer, nullable=False)
 
    # Admin links a VPS record here once provisioned
    vps_id        = Column(Integer, ForeignKey("vps_accounts.id", ondelete="SET NULL"), nullable=True)
 
    # Admin links master/slave trading accounts here
    master_account_id = Column(Integer, ForeignKey("trading_accounts.id", ondelete="SET NULL"), nullable=True)
    slave_account_id  = Column(Integer, ForeignKey("trading_accounts.id", ondelete="SET NULL"), nullable=True)
 
    # Slot lifecycle:
    #   "pending"     – payment confirmed, VPS not yet provisioned
    #   "provisioned" – VPS assigned, waiting for user to add MT5 accounts
    #   "active"      – both MT5 accounts linked, copy-trading running
    #   "paused"      – copy-trading paused by admin or user
    status        = Column(String(16), default="pending")
 
    created_at    = Column(DateTime, default=datetime.utcnow)
    updated_at    = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
 
    # Relationships
    user          = relationship("User",           foreign_keys=[user_id])
    vps           = relationship("VpsAccount",     foreign_keys=[vps_id])
    master_account = relationship("TradingAccount", foreign_keys=[master_account_id])
    slave_account  = relationship("TradingAccount", foreign_keys=[slave_account_id])
 
    __table_args__ = (
        UniqueConstraint("user_id", "slot_number", name="uniq_user_slot"),
    )

class BotLog(Base):
    __tablename__ = "bot_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)

    account_id = Column(Integer, ForeignKey("trading_accounts.id", ondelete="CASCADE"), nullable=False)

    timestamp = Column(DateTime, default=datetime.utcnow)

    level = Column(String(20))        # INFO, TRADE, ERROR
    category = Column(String(32))     # SYSTEM, COPY, EXECUTION

    message = Column(Text)
    raw_json = Column(JSON)

class AccountLot(Base):
    __tablename__ = "account_lots"

    account_id = Column(
        Integer,
        ForeignKey("trading_accounts.id", ondelete="CASCADE"),
        primary_key=True
    )

    lot_size = Column(Float, default=0.10)

    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CopyTradeSettings(Base):
    __tablename__ = "copy_trade_settings"

    user_id             = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    fixed_lot_enabled   = Column(Boolean, default=False)
    pips_offset_enabled = Column(Boolean, default=False)
    pips_offset         = Column(Integer, default=0)
    updated_at          = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class SlotSymbolMap(Base):
    """Per-slot symbol translation: master_symbol ↔ slave_symbol."""
    __tablename__ = "slot_symbol_maps"

    id            = Column(Integer, primary_key=True)
    slot_id       = Column(Integer, ForeignKey("user_slots.id", ondelete="CASCADE"), nullable=False, index=True)
    master_symbol = Column(String(32), nullable=False)
    slave_symbol  = Column(String(32), nullable=False)
    created_at    = Column(DateTime, default=datetime.utcnow)


# =========================
# NOTIFICATION SETTINGS (global, one row)
# =========================
class NotificationSettings(Base):
    __tablename__ = "notification_settings"

    id                     = Column(Integer, primary_key=True)
    margin_threshold_pct   = Column(Float,   default=50.0)
    check_interval_minutes = Column(Integer, default=15)
    message_template       = Column(Text,    default=DEFAULT_NOTIFICATION_TEMPLATE)
    updated_at             = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by             = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)


# =========================
# USER NOTIFICATION PREFS
# =========================
class UserNotificationPrefs(Base):
    __tablename__ = "user_notification_prefs"

    user_id             = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    telegram_chat_id    = Column(String(64),  nullable=True)
    telegram_link_token = Column(String(16),  nullable=False,
                                 default=lambda: secrets.token_urlsafe(8)[:8].upper())
    notify_telegram     = Column(Boolean, default=True)
    notify_email        = Column(Boolean, default=True)
    updated_at          = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# =========================
# NOTIFICATION LOG
# =========================
class NotificationLog(Base):
    __tablename__ = "notification_logs"

    id           = Column(Integer,  primary_key=True)
    user_id      = Column(Integer,  ForeignKey("users.id",            ondelete="CASCADE"))
    account_id   = Column(Integer,  ForeignKey("trading_accounts.id", ondelete="CASCADE"))
    channel      = Column(String(32))   # telegram / email / telegram+email
    message      = Column(Text)
    margin_level = Column(Float)
    sent_at      = Column(DateTime, default=datetime.utcnow)


class SymbolMappingGroup(Base):
    __tablename__ = "symbol_mapping_groups"

    id = Column(Integer, primary_key=True)

    owner_user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"))

    name = Column(String(100))

    created_at = Column(DateTime, default=datetime.utcnow)

class SymbolMappingEntry(Base):
    __tablename__ = "symbol_mapping_entries"

    id = Column(Integer, primary_key=True)

    group_id = Column(Integer, ForeignKey("symbol_mapping_groups.id", ondelete="CASCADE"))
    account_id = Column(Integer, ForeignKey("trading_accounts.id", ondelete="CASCADE"))

    symbol = Column(String(32), nullable=False)

    __table_args__ = (
        UniqueConstraint("group_id", "account_id", name="uniq_group_account"),
    )

# =========================
# TRACKED POSITIONS (safety-net replication monitor)
# =========================
class TrackedPosition(Base):
    __tablename__ = "tracked_positions"

    id                = Column(Integer, primary_key=True)
    master_account_id = Column(Integer, ForeignKey("trading_accounts.id", ondelete="CASCADE"), nullable=False)
    master_ticket     = Column(String(64), nullable=False)
    first_seen_at     = Column(DateTime, nullable=False)
    closed_by_tracker = Column(Boolean, default=False)
    intervention_at   = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("master_account_id", "master_ticket", name="uniq_tracked_pos"),
    )


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


 