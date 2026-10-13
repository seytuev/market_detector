import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import uvicorn
from app.config import Settings
from app.db import Database
from app.models import Instrument
from app.web.api import create_app
db = Database()
db.upsert_instrument(Instrument(id=None,asset="BTC",venue="binance",market_type="spot",symbol="BTCUSDT",quote_asset="USDT"))
app = create_app(db, Settings(db_path="tmp/ui/fixture.db",auth_token="browser-fixture-token"))
uvicorn.run(app, host="127.0.0.1",port=8765)
