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
            if cached and now - cached["ts"] < 5:
                return cached["data"]

            try:
                account = await rpc_pool.get_account(account_id)

                dedicated_ip = None
                try:
                    dedicated_ip = getattr(account, "allocate_dedicated_ip", None)
                except Exception:
                    pass

                if account.state != "DEPLOYED":
                    return {}

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