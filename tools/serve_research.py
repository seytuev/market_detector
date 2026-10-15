"""Run the integrated application locally without starting a second market worker."""
from __future__ import annotations
import argparse
import asyncio
from contextlib import asynccontextmanager
import uvicorn

from app.main import load_dotenv
from app.config import Settings
from app.db import Database
from app.web.api import create_app


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--port",type=int,default=8086)
    args=parser.parse_args()
    load_dotenv()
    settings=Settings()
    db=Database(settings.database_target)
    app=create_app(db,settings)
    @asynccontextmanager
    async def lifespan(app):
        stop=asyncio.Event()
        task=asyncio.create_task(app.state.research.run(stop),name="research-local")
        try:
            yield
        finally:
            stop.set()
            await task
            await app.state.settings_jobs.shutdown()
            db.close()
    app.router.lifespan_context=lifespan
    uvicorn.run(app,host="127.0.0.1",port=args.port)


if __name__=="__main__":
    main()
