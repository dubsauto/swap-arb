# swaparb/listener_manager.py

import asyncio
from sqlalchemy.orm import Session
from app.database import SessionLocal
from app.model import TradingAccount, CopyRelationship, UserSlot
from swaparb.listener import MetaApiTradeListener
from swaparb.api_client import get_metaapi_client, reset_metaapi_client
from swaparb.connection_store import set_connection, get_connection, remove_connection, get_all_connections
from app.services.rpc_pool import rpc_pool
import time


GRACE_PERIOD = 60
KEEPALIVE_INTERVAL = 45
SYNC_TIMEOUT = 30      # per attempt; 2 attempts = 60s max before reconnect
                       # (was 60 × 3 = 180s — caused 15-min outage)
MAX_SYNC_ATTEMPTS = 2  # 2 × 30s = 60s before "dead" (was 3 × 60s = 180s)
DEPLOY_WAIT = 8
CONNECT_TIMEOUT = 15   # connection.connect() hard deadline
CLOSE_TIMEOUT = 10     # connection.close() hard deadline
RELOAD_TIMEOUT = 5     # account.reload() hard deadline
GLOBAL_OUTAGE_THRESHOLD = 0.6
GLOBAL_OUTAGE_WINDOW = 10.0
GLOBAL_OUTAGE_COOLDOWN = 45


class ListenerManager:
    _sync_count: int = 0  # class-level counter for periodic diagnostic logs

    def __init__(self):
        self._lock = asyncio.Lock()
        self._running = False
        self._listeners = {}          # metaapi_account_id → listener
        self._sync_tasks = {}         # metaapi_account_id → Task
        self._connected_at = {}       # metaapi_account_id → monotonic timestamp
        self._attaching = set()       # metaapi_account_ids currently being attached
        self._api = None
        self._reconnect_queue = asyncio.Queue()
        self._reconnect_attempts = {} # metaapi_account_id → int
        self._reconnect_limit = 3     # nuclear reset after 3 cycles (was 5)
                                      # 3 × ~60s = ~3 min vs. 5 × ~180s = ~15 min

        # Serialises SDK creation/close so concurrent failures don't each
        # create a new MetaApi instance and leak the previous ones.
        self._sdk_reset_lock = asyncio.Lock()

        # Guard against cascade SDK resets
        self._last_sdk_reset = 0.0

        # Global outage detection
        self._disconnect_times = {}
        self._global_outage = False
        self._outage_recovery_task = None

    # =====================================
    # PER-ACCOUNT STATE CLEANUP
    # =====================================
    def _purge_account_state(self, account_id: str):
        """Remove all per-account metadata so memory is reclaimed."""
        self._reconnect_attempts.pop(account_id, None)
        self._disconnect_times.pop(account_id, None)
        self._connected_at.pop(account_id, None)
        self._attaching.discard(account_id)

    # =====================================
    # SAFE STREAM CLOSE
    # =====================================
    async def _close_stream_safely(self, connection, account_id: str):
        """Close a streaming connection with a hard timeout so callers never hang."""
        try:
            await asyncio.wait_for(connection.close(), timeout=CLOSE_TIMEOUT)
            print(f"[LM] Closed stream → {account_id}")
        except asyncio.TimeoutError:
            print(f"[LM] Stream close timed out → {account_id}")
        except Exception as e:
            print(f"[LM] Stream close error → {account_id}: {e}")

    # =====================================
    # SDK MANAGEMENT
    # =====================================
    async def _get_api(self):
        if self._api is None:
            self._api = get_metaapi_client()
        return self._api

    async def _reset_sdk_safely(self, stale_instance=None):
        """
        Replace the MetaApi SDK with a fresh instance and properly close the
        old one so its WebSocket threads and memory are freed.

        Serialised by _sdk_reset_lock: many coroutines that all notice the
        same stale SDK produce only ONE new instance, not N instances.

        stale_instance — the api object the caller saw fail.  If self._api
                         has already been replaced by another coroutine by
                         the time we acquire the lock, we skip the reset.
        """
        async with self._sdk_reset_lock:
            if stale_instance is not None and self._api is not stale_instance:
                print("[LM] SDK already reset by another coroutine — skipping")
                return

            old = self._api
            now = time.monotonic()
            self._last_sdk_reset = now

            print("[LM] Resetting MetaApi SDK...")
            try:
                self._api = reset_metaapi_client()
                print("[LM] MetaApi SDK reset complete")
            except Exception as e:
                print(f"[LM] SDK reset failed: {e}")
                self._api = None
                return

            # Close the old instance AFTER the new one is live.
            # Guard against SDK versions where close() returns None instead of
            # a coroutine — 'NoneType' object can't be awaited.
            if old is not None:
                try:
                    if hasattr(old, "close"):
                        result = old.close()
                        if asyncio.iscoroutine(result):
                            await asyncio.wait_for(result, timeout=10)
                        print("[LM] Old SDK instance closed cleanly")
                except Exception as e:
                    print(f"[LM] Old SDK close error (non-critical): {e}")

    # =====================================
    # SET LISTENER ACTIVE FLAG IN DB
    # =====================================
    def _set_listener_active(self, account_id_or_metaapi_id, active: bool):
        db: Session = SessionLocal()
        try:
            if isinstance(account_id_or_metaapi_id, int):
                db.query(TradingAccount).filter(
                    TradingAccount.id == account_id_or_metaapi_id
                ).update({"listener_active": active})
            else:
                db.query(TradingAccount).filter(
                    TradingAccount.metaapi_account_id == account_id_or_metaapi_id
                ).update({"listener_active": active})
            db.commit()
        except Exception as e:
            print(f"⚠️ Failed to set listener_active={active} for {account_id_or_metaapi_id}: {e}")
            db.rollback()
        finally:
            db.close()

    # =====================================
    # GLOBAL OUTAGE DETECTION
    # =====================================
    def _record_disconnect(self, account_id: str):
        now = time.monotonic()
        self._disconnect_times[account_id] = now

        recent = [
            t for t in self._disconnect_times.values()
            if now - t <= GLOBAL_OUTAGE_WINDOW
        ]

        total_known = max(len(get_all_connections()) + len(recent), 1)
        ratio = len(recent) / total_known

        if ratio >= GLOBAL_OUTAGE_THRESHOLD and not self._global_outage:
            print(f"🌐 Global outage detected — {len(recent)}/{total_known} accounts dropped simultaneously")
            self._global_outage = True

            if self._outage_recovery_task and not self._outage_recovery_task.done():
                self._outage_recovery_task.cancel()

            self._outage_recovery_task = asyncio.create_task(
                self._recover_from_global_outage()
            )

    async def _recover_from_global_outage(self):
        print(f"⏸️ Pausing reconnects for {GLOBAL_OUTAGE_COOLDOWN}s while MetaApi socket recovers...")
        await asyncio.sleep(GLOBAL_OUTAGE_COOLDOWN)

        # Drain stale queue entries
        while not self._reconnect_queue.empty():
            try:
                self._reconnect_queue.get_nowait()
                self._reconnect_queue.task_done()
            except Exception:
                break

        # Reset all counters — outage was not per-account
        self._reconnect_attempts.clear()
        self._disconnect_times.clear()

        # Reset MetaApi client — zombie state likely after long outage
        print("[LM] Resetting MetaApi client after global outage...")
        await self._reset_sdk_safely()

        # Let rpc_pool rebuild connections immediately — its build-fail cooldowns
        # were set during the outage and would otherwise block trade copying for
        # up to _build_fail_cooldown seconds after the network returns.
        rpc_pool.clear_cooldowns()

        self._global_outage = False

        print("🌐 Global outage cooldown complete — queuing fresh reconnects for all accounts")

        db: Session = SessionLocal()
        try:
            # Reuse the same slot-aware logic: only re-queue accounts that are
            # part of an active slot (or legacy CopyRelationship) and deployed.
            active_slots = db.query(UserSlot).filter(UserSlot.status == "active").all()
            slot_account_ids: set[int] = {
                aid for slot in active_slots
                for aid in (slot.master_account_id, slot.slave_account_id)
                if aid
            }

            legacy_rels = db.query(CopyRelationship).filter(
                CopyRelationship.slave_account_id.isnot(None)
            ).all()
            for rel in legacy_rels:
                for aid in (rel.master_account_id, rel.slave_account_id):
                    if aid:
                        slot_account_ids.add(aid)

            accounts = db.query(TradingAccount).filter(
                TradingAccount.id.in_(slot_account_ids)
            ).all()

            for acc in accounts:
                if not acc.state or acc.state.upper() != "DEPLOYED":
                    continue
                if not acc.metaapi_account_id:
                    continue
                if get_connection(acc.metaapi_account_id) is None:
                    try:
                        self._reconnect_queue.put_nowait(acc.metaapi_account_id)
                        print(f"📋 Queued for reconnect → {acc.metaapi_account_id}")
                    except Exception:
                        pass
        finally:
            db.close()

    # =====================================
    # KEEPALIVE
    # =====================================
    async def _keep_connections_alive(self):
        while True:
            try:
                await asyncio.sleep(KEEPALIVE_INTERVAL)
                now = time.monotonic()

                for account_id, connection in list(get_all_connections().items()):
                    try:
                        connected_at = self._connected_at.get(account_id, 0)
                        elapsed = now - connected_at

                        if elapsed < GRACE_PERIOD:
                            print(f"🕐 Grace period active → {account_id}, skipping health check")
                            continue

                        sync_task = self._sync_tasks.get(account_id)
                        if sync_task and not sync_task.done():
                            print(f"⏳ Sync in progress → {account_id}, skipping keepalive kill")
                            continue

                        health = getattr(connection, 'health_monitor', None)
                        status = getattr(health, 'health_status', None) if health else None

                        if status is not None and not status.get("connected", False):
                            print(f"💀 Keepalive detected dead connection → {account_id}")
                            await self.mark_disconnected(account_id)

                    except Exception as e:
                        print(f"⚠️ Keepalive check error for {account_id}: {e}")

            except Exception as e:
                print(f"❌ Keepalive loop error: {e}")
                await asyncio.sleep(10)

    # =====================================
    # STARTUP DIAGNOSTIC
    # =====================================
    def _log_startup_state(self):
        db: Session = SessionLocal()
        try:
            all_slots   = db.query(UserSlot).all()
            all_accts   = db.query(TradingAccount).all()
            active_rels = db.query(CopyRelationship).filter(CopyRelationship.slave_account_id.isnot(None)).all()

            print(f"[Startup] Slots ({len(all_slots)}):")
            for s in all_slots:
                print(f"  slot#{s.slot_number} user={s.user_id} status={s.status} master={s.master_account_id} slave={s.slave_account_id}")

            print(f"[Startup] TradingAccounts ({len(all_accts)}):")
            for a in all_accts:
                print(f"  id={a.id} login={a.login} state={a.state!r} metaapi_id={a.metaapi_account_id!r}")

            print(f"[Startup] CopyRelationships with slave ({len(active_rels)}):")
            for r in active_rels:
                print(f"  master={r.master_account_id} slave={r.slave_account_id}")

        except Exception as e:
            print(f"[Startup] Diagnostic failed: {e}")
        finally:
            db.close()

    # =====================================
    # START MANAGER
    # =====================================
    async def start(self):
        if self._running:
            return

        self._api = get_metaapi_client()
        self._running = True
        print("🚀 Listener Manager started")
        self._log_startup_state()

        asyncio.create_task(self._keep_connections_alive())
        asyncio.create_task(self._reconnect_worker())

        while True:
            try:
                await self._sync()
                await asyncio.sleep(5)
            except Exception as e:
                print(f"❌ Manager error: {e}")
                await asyncio.sleep(3)

    # =====================================
    # RECONNECT WORKER
    # =====================================
    async def _reconnect_worker(self):
        while True:
            try:
                account_id = await self._reconnect_queue.get()

                if self._global_outage:
                    print(f"⏸️ Global outage active — requeueing {account_id}")
                    await asyncio.sleep(5)
                    try:
                        self._reconnect_queue.put_nowait(account_id)
                    except Exception:
                        pass
                    self._reconnect_queue.task_done()
                    continue

                # ── DB CHECK: don't reconnect accounts the user has undeployed ──
                db: Session = SessionLocal()
                try:
                    acc = db.query(TradingAccount).filter(
                        TradingAccount.metaapi_account_id == account_id
                    ).first()
                finally:
                    db.close()

                if not acc:
                    print(f"⚠️ Account not found in DB → {account_id}, skipping reconnect")
                    self._reconnect_attempts.pop(account_id, None)
                    self._reconnect_queue.task_done()
                    continue

                # DB is source of truth — if user undeployed, stop reconnecting
                if not acc.state or acc.state.upper() != "DEPLOYED":
                    print(f"🛑 DB state={acc.state!r} for {account_id} — not reconnecting")
                    self._purge_account_state(account_id)
                    self._reconnect_queue.task_done()
                    continue

                attempts = self._reconnect_attempts.get(account_id, 0) + 1
                self._reconnect_attempts[account_id] = attempts

                print(f"🔁 Reconnect attempt {attempts}/{self._reconnect_limit} → {account_id}")

                if attempts >= self._reconnect_limit:
                    print(f"💣 Reconnect limit hit → {account_id}, triggering nuclear reset")
                    self._reconnect_attempts.pop(account_id, None)
                    await self._nuclear_reset(account_id, db_acc=acc)
                    self._reconnect_queue.task_done()
                    continue

                backoff = min(5 * attempts, 60)
                print(f"⏳ Backoff {backoff}s before reconnect → {account_id}")
                await asyncio.sleep(backoff)

                if self._global_outage:
                    print(f"⏸️ Global outage detected during backoff — requeueing {account_id}")
                    self._reconnect_attempts.pop(account_id, None)
                    self._reconnect_queue.task_done()
                    continue

                await self._ensure_listener(acc)
                self._reconnect_queue.task_done()

            except Exception as e:
                print(f"❌ Reconnect worker error: {e}")
                await asyncio.sleep(5)

    # =====================================
    # NUCLEAR RESET
    # =====================================
    async def _nuclear_reset(self, account_id: str, db_acc: TradingAccount = None):
        """
        Last-resort recovery: tear down everything and rebuild.

        Design notes:
        - We hold account_id in _attaching for the ENTIRE nuclear sequence so
          the _sync() loop (runs every 5s) cannot race in with a concurrent
          _ensure_listener() while the SDK is being reset or the account is
          being redeployed.  The race was the root cause of listeners attaching
          with a stale SDK, then the SDK getting replaced underneath them.
        - We skip undeploy/redeploy when the broker reports CONNECTED — the
          issue is MetaApi server-side sync overload, not the broker link.
          Undeploy/redeploy wastes 20-30s and doesn't help in that case.
        """
        print(f"☢️ Nuclear reset starting → {account_id}")

        async with self._lock:
            connection = get_connection(account_id)
            listener = self._listeners.pop(account_id, None)
            # Mark as attaching immediately — holds the slot throughout nuclear
            # so _sync()/_ensure_listener() cannot race in during SDK reset.
            self._attaching.add(account_id)
            self._connected_at.pop(account_id, None)

        self._cancel_sync_task(account_id)

        if listener:
            try:
                listener._known_positions.clear()
                listener._position_cache.clear()
                listener._disconnected = False
            except Exception:
                pass

        if connection:
            try:
                if listener:
                    connection.remove_synchronization_listener(listener)
            except Exception:
                pass
            await self._close_stream_safely(connection, account_id)
            try:
                remove_connection(account_id)
            except Exception:
                pass

        self._set_listener_active(account_id, False)
        print(f"🧹 Nuclear teardown complete → {account_id}")

        # Brief pause — let any in-flight MetaApi callbacks drain before we
        # reset the SDK.  2s is enough; the old 15s was too long and let
        # _sync() fire 3× and try to attach a new listener mid-reset.
        await asyncio.sleep(2)

        # ── 1. Reset SDK FIRST so the fresh listener uses the new instance ──
        stale = self._api
        await self._reset_sdk_safely(stale_instance=stale)

        if self._api is None:
            print(f"⚠️ SDK unavailable after reset → {account_id}, aborting nuclear")
            async with self._lock:
                self._attaching.discard(account_id)
            return

        # ── 2. DB is source of truth: only act if still deployed ──
        if db_acc is None:
            db: Session = SessionLocal()
            try:
                db_acc = db.query(TradingAccount).filter(
                    TradingAccount.metaapi_account_id == account_id
                ).first()
            finally:
                db.close()

        if not db_acc or not db_acc.state or db_acc.state.upper() != "DEPLOYED":
            print(f"🛑 DB state={db_acc.state if db_acc else 'MISSING'!r} → skipping nuclear redeploy for {account_id}")
            async with self._lock:
                self._attaching.discard(account_id)
            return

        # ── 3. Only undeploy/redeploy if broker is genuinely unreachable ──
        # When broker is CONNECTED the issue is MetaApi sync server overload.
        # Undeploy/redeploy won't help and wastes 20-30s.
        try:
            account = await self._api.metatrader_account_api.get_account(account_id)
            broker_connected = getattr(account, "connection_status", None) == "CONNECTED"

            if account.state.upper() != "DEPLOYED":
                print(f"🚀 Nuclear deploy → {account_id}")
                await account.deploy()
                await asyncio.sleep(DEPLOY_WAIT)
            elif not broker_connected:
                print(f"🔄 Nuclear undeploy → redeploy → {account_id} (broker not connected)")
                await account.undeploy()
                await asyncio.sleep(10)
                await account.deploy()
                await asyncio.sleep(DEPLOY_WAIT)
            else:
                print(f"🔄 Nuclear reattach only → {account_id} (broker already CONNECTED, skipping redeploy)")

        except Exception as e:
            print(f"⚠️ Nuclear redeploy failed → {account_id}: {e}")

        # ── 4. Release the slot and queue a fresh reconnect ──
        async with self._lock:
            self._attaching.discard(account_id)

        print(f"🔁 Queuing fresh reconnect after nuclear reset → {account_id}")
        try:
            self._reconnect_queue.put_nowait(account_id)
        except Exception:
            pass

    # =====================================
    # DB SYNC + HEALTH CHECK
    # =====================================
    async def _sync(self):
        if self._global_outage:
            return

        ListenerManager._sync_count += 1
        periodic = (ListenerManager._sync_count % 12 == 1)  # log every ~60s

        db: Session = SessionLocal()

        try:
            # ── Build the set of account IDs that should have active listeners ──
            # An account needs a listener when it is part of an *active* slot
            # AND is deployed.  Paused / pending slots should not have listeners.
            # We also support the legacy path (manual CopyRelationship without a
            # UserSlot row) so existing setups continue to work.

            should_listen: set[int] = set()   # DB TradingAccount.id values

            # 1. Slot-based (new system): active slots only.
            # Also pick up provisioned slots that already have both accounts linked —
            # this handles the case where account IDs were set but status was never
            # transitioned to "active" (e.g. direct DB edits or a past add-account bug).
            # Auto-repair those slots so future syncs are clean.
            active_slots = db.query(UserSlot).filter(
                (UserSlot.status == "active") |
                (
                    UserSlot.master_account_id.isnot(None) &
                    UserSlot.slave_account_id.isnot(None)
                )
            ).all()
            for slot in active_slots:
                if slot.status != "active" and slot.master_account_id and slot.slave_account_id:
                    print(f"[Sync] Auto-repairing slot#{slot.slot_number} user={slot.user_id}: both accounts set but status={slot.status!r} → setting 'active'")
                    slot.status = "active"
                    db.commit()
                for acct_id in (slot.master_account_id, slot.slave_account_id):
                    if acct_id:
                        should_listen.add(acct_id)

            # 2. Legacy path: manual CopyRelationships not attached to any slot
            slot_account_ids = {aid for slot in active_slots
                                for aid in (slot.master_account_id, slot.slave_account_id)
                                if aid}
            legacy_rels = db.query(CopyRelationship).filter(
                CopyRelationship.slave_account_id.isnot(None)
            ).all()
            for rel in legacy_rels:
                for acct_id in (rel.master_account_id, rel.slave_account_id):
                    if acct_id and acct_id not in slot_account_ids:
                        should_listen.add(acct_id)

            if periodic:
                all_slots = db.query(UserSlot).all()
                slot_summary = ", ".join(
                    f"slot#{s.slot_number}(user={s.user_id},status={s.status},m={s.master_account_id},s={s.slave_account_id})"
                    for s in all_slots
                ) or "none"
                print(f"[Sync] Slots in DB: {slot_summary}")
                print(f"[Sync] should_listen={should_listen} | active_slots={len(active_slots)} | legacy_rels={len([r for r in legacy_rels if r.slave_account_id])}")

            if not should_listen:
                if periodic:
                    print("[Sync] ⚠️  should_listen is EMPTY — no active slots and no legacy CopyRelationships with slave. Listeners will not start.")
                return

            # ── Now iterate every account and start / stop listeners ──
            accounts = db.query(TradingAccount).all()

            for acc in accounts:
                if acc.id not in should_listen:
                    # Not in any active slot and no legacy relationship — remove
                    await self._remove_listener(acc)
                    continue

                if not acc.state or acc.state.upper() != "DEPLOYED":
                    # In a qualifying slot but not deployed yet — remove listener
                    # (will be re-attached automatically once the user deploys)
                    await self._remove_listener(acc)
                    print(f"[Sync] Skipping {acc.id} — in active slot but state={acc.state!r}")
                    continue

                await self._ensure_listener(acc)

        finally:
            db.close()

        now = time.monotonic()
        for account_id, connection in list(get_all_connections().items()):
            try:
                connected_at = self._connected_at.get(account_id, 0)
                if now - connected_at < GRACE_PERIOD:
                    continue

                sync_task = self._sync_tasks.get(account_id)
                if sync_task and not sync_task.done():
                    continue

                health = getattr(connection, 'health_monitor', None)
                status = getattr(health, 'health_status', None) if health else None

                if status is not None and not status.get("connected", False):
                    print(f"💀 Dead connection detected → {account_id}")
                    await self.mark_disconnected(account_id)

            except Exception:
                pass

    # =====================================
    # ENSURE LISTENER EXISTS
    # =====================================
    async def _ensure_listener(self, acc: TradingAccount):
        account_id = acc.metaapi_account_id
        if not account_id:
            print(f"[Sync] ⚠️  Account {acc.id} (login={acc.login}) has no metaapi_account_id — skipping listener attach")
            return

        async with self._lock:
            if get_connection(account_id) is not None:
                return
            if account_id in self._attaching:
                print(f"⏸️ Already attaching → {account_id}, skipping")
                return
            self._attaching.add(account_id)

        connection = None

        try:
            print(f"🔌 Attaching listener → {account_id}")

            api = await self._get_api()
            account = await api.metatrader_account_api.get_account(account_id)

            if account.state.upper() != "DEPLOYED":
                print(f"🚀 Deploying → {account_id}")
                await account.deploy()
                await asyncio.sleep(DEPLOY_WAIT)

            print(f"⏳ Waiting for broker connection → {account_id}")
            timeout = 15
            connected = False

            for i in range(timeout):
                try:
                    await asyncio.wait_for(account.reload(), timeout=RELOAD_TIMEOUT)
                except Exception:
                    pass

                status = account.connection_status
                print(f"   [{i+1}/{timeout}] connection_status={status}")

                if status == "CONNECTED":
                    connected = True
                    break

                await asyncio.sleep(1)

            if not connected:
                print(f"⚠️ Broker not CONNECTED after {timeout}s → {account_id}, attempting stream anyway")

            await asyncio.sleep(2)

            connection = account.get_streaming_connection()
            print(f"🔗 Connecting stream → {account_id}")
            try:
                await asyncio.wait_for(connection.connect(), timeout=CONNECT_TIMEOUT)
            except asyncio.TimeoutError:
                await self._close_stream_safely(connection, account_id)
                raise Exception(
                    f"[LM] connection.connect() timed out → {account_id}"
                )

            async with self._lock:
                if get_connection(account_id) is not None:
                    print(f"⚠️ Concurrent attach beat us → {account_id}, closing duplicate")
                    await self._close_stream_safely(connection, account_id)
                    return

                listener = MetaApiTradeListener(acc.id, metaapi_account_id=account_id, manager=self)
                connection.add_synchronization_listener(listener)
                set_connection(account_id, connection)
                self._listeners[account_id] = listener
                self._connected_at[account_id] = time.monotonic()

            print(f"👂 Listener attached → {account_id}")
            self._set_listener_active(account_id, False)

            task = asyncio.create_task(
                self._background_sync_wait(account_id, connection)
            )
            async with self._lock:
                self._sync_tasks[account_id] = task

        except Exception as e:
            print(f"❌ Attach failed {acc.id}: {e}")
            if connection:
                await self._close_stream_safely(connection, account_id)

        finally:
            async with self._lock:
                self._attaching.discard(account_id)

    # =====================================
    # BACKGROUND SYNC WAIT
    # =====================================
    async def _background_sync_wait(self, account_id: str, connection):
        try:
            for attempt in range(1, MAX_SYNC_ATTEMPTS + 1):
                try:
                    await asyncio.wait_for(
                        connection.wait_synchronized(),
                        timeout=SYNC_TIMEOUT
                    )
                    print(f"✅ Background sync complete → {account_id}")
                    self._set_listener_active(account_id, True)
                    self._reconnect_attempts.pop(account_id, None)
                    self._disconnect_times.pop(account_id, None)
                    return

                except asyncio.CancelledError:
                    print(f"🛑 Background sync cancelled → {account_id}")
                    return

                except asyncio.TimeoutError:
                    print(f"⏳ Background sync timeout (attempt {attempt}/{MAX_SYNC_ATTEMPTS}) → {account_id}")

                except Exception as e:
                    print(f"⚠️ Background sync error (attempt {attempt}/{MAX_SYNC_ATTEMPTS}) → {account_id}: {e}")
                    if "connection has been closed" in str(e).lower():
                        print(f"🛑 Connection closed, stopping background sync → {account_id}")
                        await self.mark_disconnected(account_id)
                        return

                await asyncio.sleep(5)

            # All sync attempts exhausted — treat as dead and trigger reconnect
            print(
                f"⚠️ Sync never completed after {MAX_SYNC_ATTEMPTS} attempts → "
                f"{account_id}, triggering reconnect"
            )
            await self.mark_disconnected(account_id)

        finally:
            # Always remove the done task from the dict so it doesn't
            # accumulate as a ghost entry consuming memory indefinitely.
            self._sync_tasks.pop(account_id, None)

    # =====================================
    # CANCEL SYNC TASK
    # =====================================
    def _cancel_sync_task(self, account_id: str):
        task = self._sync_tasks.pop(account_id, None)
        if task and not task.done():
            task.cancel()

    # =====================================
    # REMOVE LISTENER
    # =====================================
    async def _remove_listener(self, acc: TradingAccount):
        account_id = acc.metaapi_account_id
        if not account_id:
            return

        # Fast path: if nothing is attached there is nothing to clean up.
        async with self._lock:
            has_anything = (
                account_id in self._listeners
                or get_connection(account_id) is not None
                or account_id in self._attaching
            )
        if not has_anything:
            return

        async with self._lock:
            connection = get_connection(account_id)
            listener = self._listeners.pop(account_id, None)
            self._attaching.discard(account_id)
            self._connected_at.pop(account_id, None)

        self._cancel_sync_task(account_id)
        # Full per-account state cleanup
        self._purge_account_state(account_id)

        if not connection:
            return

        try:
            print(f"🛑 Removing listener → {account_id}")

            if listener:
                try:
                    connection.remove_synchronization_listener(listener)
                except Exception:
                    pass

            await self._close_stream_safely(connection, account_id)
            remove_connection(account_id)

            if listener:
                try:
                    listener._known_positions.clear()
                    listener._position_cache.clear()
                except Exception:
                    pass

            self._set_listener_active(account_id, False)
            print(f"🗑️ Listener removed → {account_id}")

        except Exception as e:
            print(f"❌ Remove failed {account_id}: {e}")

    # =====================================
    # MARK DISCONNECTED
    # =====================================
    async def mark_disconnected(self, account_id: str):
        self._record_disconnect(account_id)

        async with self._lock:
            if account_id not in self._listeners and get_connection(account_id) is None:
                return

            connection = get_connection(account_id)
            listener = self._listeners.pop(account_id, None)
            self._attaching.discard(account_id)
            self._connected_at.pop(account_id, None)

        self._cancel_sync_task(account_id)

        if connection:
            if listener:
                try:
                    connection.remove_synchronization_listener(listener)
                except Exception:
                    pass
            await self._close_stream_safely(connection, account_id)
            remove_connection(account_id)

        if listener:
            try:
                listener._known_positions.clear()
                listener._position_cache.clear()
            except Exception:
                pass

        self._set_listener_active(account_id, False)
        print(f"♻️ Marked for reconnection → {account_id}")

        if not self._global_outage:
            self._reconnect_queue.put_nowait(account_id)


# =====================================
# SINGLETON
# =====================================
listener_manager = ListenerManager()
