# app/services/account_management.py
#
# Administrative MetaAPI operations (create, deploy, undeploy, remove, update).
# Uses a shared admin SDK instance for account management — no rpc_pool.
# Data methods (metrics, symbol spec/price, account info) accept a pre-built
# connection from dashboard_session so the web service never touches rpc_pool.

import asyncio
import time
from datetime import datetime, timezone
from typing import Optional, Dict, List

from swaparb.api_client import get_metaapi_client

# Shared SDK instance for admin calls only (no RPC connections)
_api = None

def _get_admin_api():
    global _api
    if _api is None:
        _api = get_metaapi_client()
    return _api

async def _get_account(account_id: str):
    api = _get_admin_api()
    return await api.metatrader_account_api.get_account(account_id)


class MT5AccountManager:
    def __init__(self):
        self._metrics_cache: Dict[str, Dict] = {}
        self._semaphore = asyncio.Semaphore(5)

    # =========================
    # ADD ACCOUNT
    # =========================
    async def add_account(
        self,
        name: str,
        server: str,
        login: str,
        password: str,
        manual_trades: bool = True,
        use_dedicated_ip: bool = True,
        magic: Optional[int] = None
    ) -> Dict:
        try:
            api = _get_admin_api()
            accounts = await api.metatrader_account_api.get_accounts_with_infinite_scroll_pagination()

            for acc in accounts:
                if str(acc.login) == str(login) and acc.type.startswith('cloud'):
                    return {"success": True, "account_id": acc.id}

            account_data = {
                'name': name,
                'type': 'cloud',
                'login': login,
                'password': password,
                'server': server,
                'platform': 'mt5',
                'manualTrades': manual_trades,
                'allocateDedicatedIp': 'ipv4' if use_dedicated_ip else None,
                'magic': 0 if manual_trades else (magic or 0)
            }

            new_account = await api.metatrader_account_api.create_account(account_data)

            # Force undeploy — MetaAPI auto-deploys on creation.
            try:
                if new_account.state != "UNDEPLOYED":
                    await new_account.undeploy()
            except Exception as ue:
                print(f"⚠️  Could not undeploy new account {new_account.id} on creation: {ue}")

            return {"success": True, "account_id": new_account.id}

        except Exception as e:
            return {"success": False, "message": str(e)}

    # =========================
    # REMOVE ACCOUNT
    # =========================
    async def remove_account(self, account_id: str) -> Dict:
        try:
            account = await _get_account(account_id)
            await account.remove()
            return {"success": True}
        except Exception as e:
            if "not found" in str(e).lower():
                return {"success": True}
            return {"success": False, "message": str(e)}

    # =========================
    # UPDATE ACCOUNT
    # =========================
    async def update_account(self, account_id: str, update_data: Dict) -> Dict:
        try:
            account = await _get_account(account_id)
            await account.update(update_data)
            return {"success": True}
        except Exception as e:
            return {"success": False, "message": str(e)}

    # =========================
    # DEPLOY
    # =========================
    async def deploy(self, account_id: str) -> Dict:
        try:
            account = await _get_account(account_id)
            if account.state != "DEPLOYED":
                await account.deploy()
            return {"success": True}
        except Exception as e:
            return {"success": False, "message": str(e)}

    # =========================
    # UNDEPLOY
    # =========================
    async def undeploy(self, account_id: str) -> Dict:
        try:
            account = await _get_account(account_id)
            if account.state != "UNDEPLOYED":
                await account.undeploy()
            return {"success": True}
        except Exception as e:
            return {"success": False, "message": str(e)}

    # =========================
    # METRICS
    # =========================
    async def get_account_metrics(self, account_id: str, connection=None) -> Dict:
        """
        Fetch balance/equity/positions for dashboard display.
        Pass the user's already-open dashboard_session connection.
        Returns {} if no connection is provided or on error.
        """
        async with self._semaphore:
            now = time.time()
            cached = self._metrics_cache.get(account_id)
            if cached and now - cached["ts"] < 5 and "_account_state" not in cached["data"]:
                return cached["data"]

            if not connection:
                print(f"[Metrics] No connection provided → {account_id}, returning empty")
                return {}

            try:
                start = time.perf_counter()

                info, positions = await asyncio.gather(
                    asyncio.wait_for(connection.get_account_information(), timeout=5),
                    asyncio.wait_for(connection.get_positions(), timeout=5)
                )

                latency_ms = (time.perf_counter() - start) * 1000

                now_dt = datetime.now(timezone.utc)
                is_wednesday = now_dt.weekday() == 2  # 0=Mon … 6=Sun

                dedicated_ip = None

                positions_out: List[Dict] = []
                total_cycle_swap = 0.0

                for pos in positions:
                    sym        = pos.get("symbol", "?")
                    cycle_swap = float(pos.get("swap") or 0)
                    total_cycle_swap += cycle_swap

                    open_raw  = pos.get("time") or pos.get("openTime")
                    days_open = 1.0
                    if open_raw:
                        try:
                            if isinstance(open_raw, str):
                                open_dt = datetime.fromisoformat(open_raw.replace("Z", "+00:00"))
                            else:
                                open_dt = open_raw if open_raw.tzinfo else open_raw.replace(tzinfo=timezone.utc)
                            elapsed = (now_dt - open_dt).total_seconds()
                            days_open = max(1.0, elapsed / 86400)
                        except Exception:
                            pass

                    daily_avg   = cycle_swap / days_open
                    today_swap  = daily_avg * 3 if is_wednesday else daily_avg
                    weekly_swap = daily_avg * 7

                    pnl = pos.get("profit")
                    positions_out.append({
                        "id":          str(pos.get("id") or pos.get("ticket") or ""),
                        "symbol":      sym,
                        "type":        pos.get("type", ""),
                        "lots":        pos.get("volume"),
                        "open_price":  pos.get("openPrice"),
                        "pnl":         round(float(pnl), 2) if pnl is not None else None,
                        "days_open":   round(days_open, 1),
                        "cycle_swap":  round(cycle_swap,  2),
                        "today_swap":  round(today_swap,  2),
                        "weekly_swap": round(weekly_swap, 2),
                    })

                result = {
                    "balance":          info.get("balance"),
                    "equity":           info.get("equity"),
                    "margin":           info.get("margin"),
                    "free_margin":      info.get("freeMargin"),
                    "leverage":         info.get("leverage"),
                    "latency_ms":       round(latency_ms, 2),
                    "positions":        positions_out,
                    "positions_count":  len(positions),
                    "total_cycle_swap": round(total_cycle_swap, 2),
                    "is_wednesday":     is_wednesday,
                    "dedicated_ip":     dedicated_ip,
                }

                self._metrics_cache[account_id] = {"ts": now, "data": result}
                return result

            except asyncio.TimeoutError:
                print(f"[Metrics] Timeout → {account_id}")
                return {}
            except Exception as e:
                print(f"[Metrics] Error → {account_id}: {e}")
                return {}

    # =========================
    # SYMBOL SPECIFICATION
    # =========================
    async def get_symbol_spec(self, account_id: str, symbol: str, connection=None) -> Dict:
        """Fetch swap rates, contract details and commission for a symbol."""
        try:
            if not connection:
                return {"error": "No connection available"}
            spec = await asyncio.wait_for(
                connection.get_symbol_specification(symbol),
                timeout=8,
            )
            if not spec:
                return {"error": "Symbol not found"}

            commission_per_lot = None
            comm_raw = spec.get("commissions") or spec.get("dealCommissions")
            if isinstance(comm_raw, list) and comm_raw:
                for c in comm_raw:
                    if isinstance(c, dict):
                        v = c.get("commission")
                        if v is not None:
                            try:
                                commission_per_lot = float(v)
                                break
                            except (TypeError, ValueError):
                                pass
            if commission_per_lot is None:
                for field in ("commissionLot", "commissionRate", "commission"):
                    v = spec.get(field)
                    if v is not None:
                        try:
                            commission_per_lot = float(v)
                            break
                        except (TypeError, ValueError):
                            pass

            return {
                "swap_long":           spec.get("swapLong"),
                "swap_short":          spec.get("swapShort"),
                "swap_rollover3_days": spec.get("swapRollover3Days"),
                "swap_mode":           spec.get("swapMode"),
                "contract_size":       spec.get("contractSize"),
                "digits":              spec.get("digits"),
                "commission_per_lot":  commission_per_lot,
            }
        except Exception as e:
            print(f"[SymbolSpec] {account_id}/{symbol}: {e}")
            return {"error": str(e)}

    # =========================
    # ACCOUNT INFO
    # =========================
    async def get_account_info(self, account_id: str, connection=None) -> Dict:
        """Fetch basic account info (balance, leverage)."""
        try:
            if not connection:
                return {}
            info = await asyncio.wait_for(
                connection.get_account_information(),
                timeout=5,
            )
            if not info:
                return {}
            return {
                "balance":  info.get("balance"),
                "leverage": info.get("leverage"),
            }
        except Exception as e:
            print(f"[AccountInfo] {account_id}: {e}")
            return {}

    # =========================
    # SYMBOL PRICE (live bid/ask)
    # =========================
    async def get_symbol_price(self, account_id: str, symbol: str, connection=None) -> Dict:
        """Fetch live bid/ask price for a symbol."""
        try:
            if not connection:
                return {"error": "No connection available"}
            price = await asyncio.wait_for(
                connection.get_symbol_price(symbol),
                timeout=8,
            )
            if not price:
                return {"error": "Symbol not found"}
            return {
                "bid": price.get("bid"),
                "ask": price.get("ask"),
            }
        except Exception as e:
            print(f"[SymbolPrice] {account_id}/{symbol}: {e}")
            return {"error": str(e)}

    # =========================
    # CLOSE POSITION
    # =========================
    async def close_position(self, account_id: str, position_id: str, connection=None) -> Dict:
        try:
            if not connection:
                return {"success": False, "message": "No connection available"}
            result = await asyncio.wait_for(
                connection.close_position(position_id, {}),
                timeout=15
            )
            return {"success": True, "result": result}
        except asyncio.TimeoutError:
            return {"success": False, "message": "Request timed out"}
        except Exception as e:
            return {"success": False, "message": str(e)}


account_manager = MT5AccountManager()
