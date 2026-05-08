print("🔔 NOTIFICATION WORKER STARTING")

import asyncio
from dotenv import load_dotenv
load_dotenv()

from swaparb.notification_worker import notification_worker

async def main():
    await notification_worker.start()

if __name__ == "__main__":
    asyncio.run(main())
