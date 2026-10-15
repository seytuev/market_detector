"""Import the original terminal archive through the authenticated app API.

Auth stays in the existing server env; it is never printed or put in a URL.
"""
import argparse
import hashlib
import json
from zipfile import ZipFile
import httpx
from app.main import load_dotenv
from app.config import Settings
from app.research.contracts import VERSION
from tools.import_terminal_snapshot import build_items


def main():
    p=argparse.ArgumentParser();p.add_argument("archive");p.add_argument("--url",required=True);p.add_argument("--verify-core-assets",action="store_true");args=p.parse_args()
    if not (args.url.startswith("https://") or args.url.startswith(("http://127.0.0.1:","http://localhost:"))):p.error("Use HTTPS or loopback")
    load_dotenv();cfg=Settings();checksum=hashlib.sha256(open(args.archive,"rb").read()).hexdigest();items,captured=build_items(args.archive)
    totals={"items":len(items),"imported":0,"quarantined":0,"ideas":0,"baskets":0,"channels":0}
    with httpx.Client(base_url=args.url.rstrip("/"),headers={"Authorization":"Bearer "+cfg.auth_token},timeout=90,follow_redirects=False) as client:
        def post(path,body,key):
            response=client.post("/api/research/v1"+path,json=body,headers={"Idempotency-Key":key});response.raise_for_status();return response.json()["data"]
        for start in range(0,len(items),50):
            value=post("/integration/import-batch",{"schema_version":VERSION,"dataset_id":"archive:"+checksum,"items":items[start:start+50],
                "archive_captured_at":captured,"next_cursor":"archive:"+str(start+50),"has_more":start+50<len(items),"history_complete":False},"archive:"+checksum+":"+str(start))
            totals["imported"]+=value["imported"];totals["quarantined"]+=value["quarantined"]
        if args.verify_core_assets:
            instruments=client.get("/api/instruments");instruments.raise_for_status();instruments=instruments.json()
            for symbol,coin in (("BTC","bitcoin"),("ETH","ethereum"),("SOL","solana"),("ZEC","zcash")):
                verified=httpx.get("https://api.coingecko.com/api/v3/coins/"+coin,params={"localization":"false","tickers":"false","market_data":"false","community_data":"false","developer_data":"false"},timeout=20)
                if verified.status_code!=200:continue
                identity=verified.json()
                if identity.get("id")!=coin or identity.get("symbol","").upper()!=symbol:continue
                assets=client.get("/api/research/v1/assets",params={"q":symbol,"limit":100});assets.raise_for_status()
                asset=next((a for a in assets.json()["data"]["items"] if any(ref["namespace"]=="terminal" and ref["external_id"]=="ticker:"+symbol for ref in a["external_ids"])),None)
                if not asset:continue
                post("/assets/"+asset["asset_uid"]+"/external-identifiers",{"external_ids":[{"namespace":"coingecko","external_id":coin}],
                    "reason":"Проверены CoinGecko ID и symbol по официальному API каталога"},"verify-core:"+coin)
                links=client.get("/api/research/v1/identity/instruments");links.raise_for_status();links=links.json()["data"]["items"]
                for instrument in instruments:
                    if instrument.get("asset")!=symbol or instrument.get("market_type")!="spot" or instrument.get("quote_asset")!="USDT":continue
                    current=next((r["link"] for r in links if r["instrument_id"]==instrument["id"]),None)
                    if current and current.get("asset_uid")!=asset["asset_uid"]:continue
                    if not current:
                        post("/instruments/"+str(instrument["id"])+"/asset-link",{"asset_uid":asset["asset_uid"],"expected_revision":None,
                            "reason":"Проверены CoinGecko ID, символ, spot-рынок и расчётная валюта USDT"},"link-core:"+str(instrument["id"])+":"+coin)
        with ZipFile(args.archive) as z:
            existing_channels=client.get("/api/research/v1/channels");existing_channels.raise_for_status()
            configured={row["handle"] for row in existing_channels.json()["data"]["items"]}
            narratives={}
            name=next((n for n in z.namelist() if n.endswith("/narrative-baskets.json")),None)
            if name:narratives={v["id"]:v for v in json.loads(z.read(name)).get("baskets",[])}
            for filename in z.namelist():
                if "/basket-index-" in filename and filename.endswith(".json"):
                    external=filename.rsplit("basket-index-",1)[1][:-5];state=json.loads(z.read(filename)).get("state",{})
                    details=narratives.get(external,{})
                    post("/baskets/import",{"external_id":external,"state":state,"title":details.get("name"),"thesis":details.get("thesis","")},"basket-import:"+checksum+":"+external);totals["baskets"]+=1
                if filename.endswith("/investment-ideas.json"):
                    for idea in json.loads(z.read(filename)).get("ideas",[]):
                        post("/ideas/import",{"external_id":idea["id"],"idea":idea},"idea-import:"+checksum+":"+idea["id"]);totals["ideas"]+=1
                if filename.endswith("/telegram-sources.json"):
                    for channel in json.loads(z.read(filename)).get("sources",[]):
                        if channel["handle"].removeprefix("@").lower() in configured:continue
                        post("/channels",{"handle":channel["handle"],"title":channel.get("title") or channel["handle"],"enabled":bool(channel.get("enabled"))},
                            "channel-import:"+checksum+":"+channel["handle"]);totals["channels"]+=1
    print(json.dumps(totals))


if __name__=="__main__":main()
