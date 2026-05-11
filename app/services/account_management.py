# app/services/account_management.py

import asyncio
import time
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from app.services.rpc_pool import rpc_pool


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
            api = await rpc_pool._get_api()
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

            api = await rpc_pool._get_api()
            new_account = await api.metatrader_account_api.create_account(account_data)

            # Pre-cache the account object in the pool
            rpc_pool._accounts[new_account.id] = new_account

            # Force undeploy — MetaAPI auto-deploys on creation.
            # Users must explicitly click Deploy before the account goes live.
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
            account = await rpc_pool.get_account(account_id)
            await account.remove()
            await rpc_pool.invalidate(account_id)
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
            account = await rpc_pool.get_account(account_id)
            await account.update(update_data)
            return {"success": True}
        except Exception as e:
            return {"success": False, "message": str(e)}

    # =========================
    # DEPLOY
    # =========================
    async def deploy(self, account_id: str) -> Dict:
        try:
            account = await rpc_pool.get_account(account_id)
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
            account = await rpc_pool.get_account(account_id)
            if account.state != "UNDEPLOYED":
                await account.undeploy()
            return {"success": True}
        except Exception as e:
            return {"success": False, "message": str(e)}

    # =========================
    # METRICS
    # =========================
    async def get_account_metrics(self, account_id: str):
        async with self._semaphore:
            now = time.time()

            cached = self._metrics_cache.get(account_id)
            # Only serve cache for real metrics, never for state-only stubs
            if cached and now - cached["ts"] < 5 and "_account_state" not in cached["data"]:
                return cached["data"]

            try:
                # Use cached account object if available — avoids a remote
                # get_account() call when the object is already in the pool.
                # If not cached, fetch it (one API call), then reload for state.
                account = await rpc_pool.get_account(account_id)

                # Always reload — ensures state is fresh after deploy/undeploy
                try:
                    await asyncio.wait_for(account.reload(), timeout=8)
                except Exception as re:
                    print(f"[Metrics] reload warning {account_id}: {re}")

                dedicated_ip = None
                try:
                    dedicated_ip = getattr(account, "allocate_dedicated_ip", None)
                except Exception:
                    pass

                current_state = (account.state or "").upper()
                if current_state != "DEPLOYED":
                    # Return structured state so dashboard can show the right message
                    return {"_account_state": current_state.lower()}

                connection = await rpc_pool.get_connection(account_id)

                start = time.perf_counter()

                info, positions = await asyncio.gather(
                    asyncio.wait_for(connection.get_account_information(), timeout=5),
                    asyncio.wait_for(connection.get_positions(), timeout=5)
                )

                latency_ms = (time.perf_counter() - start) * 1000

                now_dt       = datetime.now(timezone.utc)
                is_wednesday = now_dt.weekday() == 2  # 0=Mon … 6=Sun

                positions_out: List[Dict] = []
                total_cycle_swap = 0.0

                for pos in positions:
                    sym        = pos.get("symbol", "?")
                    cycle_swap = float(pos.get("swap") or 0)
                    total_cycle_swap += cycle_swap

                    # Estimate daily avg from open time
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
                    weekly_swap = daily_avg * 7   # 4 normal + 1 Wednesday(×3)

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
                print(f"[Timeout] {account_id}")
                return {}
            except Exception as e:
                print(f"[Error] {account_id}: {e}")
                return {}


    # =========================
    # SYMBOL SPECIFICATION
    # =========================
    async def get_symbol_spec(self, account_id: str, symbol: str) -> Dict:
        """Fetch swap rates and contract details for a symbol from a deployed account."""
        try:
            connection = await rpc_pool.get_connection(account_id)
            spec = await asyncio.wait_for(
                connection.get_symbol_specification(symbol),
                timeout=8,
            )
            if not spec:
                return {"error": "Symbol not found"}
            return {
                "swap_long":           spec.get("swapLong"),
                "swap_short":          spec.get("swapShort"),
                "swap_rollover3_days": spec.get("swapRollover3Days"),
                "swap_mode":           spec.get("swapMode"),
                "contract_size":       spec.get("contractSize"),
                "digits":              spec.get("digits"),
            }
        except Exception as e:
            print(f"[SymbolSpec] {account_id}/{symbol}: {e}")
            return {"error": str(e)}

    # =========================
    # SYMBOL PRICE (live bid/ask)
    # =========================
    async def get_symbol_price(self, account_id: str, symbol: str) -> Dict:
        """Fetch live bid/ask price for a symbol from a deployed account."""
        try:
            connection = await rpc_pool.get_connection(account_id)
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
    async def close_position(self, account_id: str, position_id: str) -> Dict:
        try:
            connection = await rpc_pool.get_connection(account_id)
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