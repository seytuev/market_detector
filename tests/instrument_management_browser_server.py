"""Isolated browser fixture. Run with:
python -m uvicorn tests.instrument_management_browser_server:app --port 8897
"""
from app.adapters.binance import BinanceSpotAdapter
from app.config import Settings
from app.db import Database
from app.models import Instrument
from app.web.api import create_app


async def catalog(self):
    return [Instrument(None, "SOL", "binance", "spot", "SOLUSDT", "USDT", precision=3)]


BinanceSpotAdapter.catalog = catalog
settings = Settings()
settings.auth_token = "asset-ui-test"
settings.db_path = "tmp/asset-ui-settings.db"
app = create_app(Database(":memory:"), settings)
