import os
from dotenv import load_dotenv

load_dotenv()

KALSHI_API_KEY_ID = os.getenv("KALSHI_API_KEY_ID", "")
KALSHI_PRIVATE_KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH", "./kalshi.key")
KALSHI_BASE_URL = os.getenv("KALSHI_BASE_URL", "https://api.elections.kalshi.com")
KALSHI_WS_URL = os.getenv("KALSHI_WS_URL", "wss://api.elections.kalshi.com/trade-api/ws/v2")
API_PATH_PREFIX = "/trade-api/v2"

BANKROLL_CENTS = int(os.getenv("BANKROLL", "3000"))
BET_PERCENT = int(os.getenv("BET_PERCENT", "10"))
MIN_ARB_PERCENT = float(os.getenv("MIN_ARB_PERCENT", "2.0"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

SCAN_INTERVAL_SEC = 30
ORDERBOOK_FETCH_WORKERS = 20

TAKER_FEE_RATE = 0.07
