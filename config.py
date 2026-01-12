import os
from dotenv import load_dotenv

load_dotenv()

# Telegram
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

# Monitoring intervals
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 60))
FAST_CHECK_INTERVAL = int(os.getenv("FAST_CHECK_INTERVAL", 15))

# Thresholds
FAIL_THRESHOLD = int(os.getenv("FAIL_THRESHOLD", 3))
RECOVERY_THRESHOLD = int(os.getenv("RECOVERY_THRESHOLD", 2))

# Database
DB_PATH = os.getenv("DB_PATH", "data/servers.db")