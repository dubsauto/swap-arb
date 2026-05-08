# run_worker.py  (replaces run_listener.py + run_notification.py)
import asyncio
from dotenv import load_dotenv
load_dotenv()

from swaparb.listener_manager import listener_manager
from swaparb.notification_worker import notification_worker

async def main():
    await asyncio.gather(
        listener_manager.start(),
        notification_worker.start(),
    )

if __name__ == "__main__":
    asyncio.run(main())