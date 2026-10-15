"""Copy the existing STVTerminal algorithms, retaining reproducible provenance."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

FILES = [
    "src/events/types.ts", "src/events/time.ts", "src/events/merge.ts", "src/events/importance.ts",
    "services/project-signals/model.ts", "services/ticker_recap/mention-identity.ts",
    "services/market/assetResolution.ts", "src/telegram/stories.ts", "src/telegram/noise.ts",
    "src/live/tmeReader.ts",
    "services/investment-ideas/analytics.ts", "services/investment-ideas/validation.ts", "services/baskets/index-model.ts",
    "src/playbook/types.ts", "src/playbook/snapshot.ts", "src/playbook/similar.ts",
    "src/playbook/conditions.ts", "src/playbook/weights.ts",
    "src/playbook/distill.ts", "src/playbook/match.ts",
    "services/backtest/types.ts", "services/backtest/validation.ts",
    *["services/backtest/rules/"+name+".ts" for name in
      ("registry","breakout","dip-buy","relative-strength","event-window","narrative-momentum",
       "stop-loss","take-profit","time-stop","trailing","rs-fade")],
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("checkout", type=Path)
    args = parser.parse_args()
    destination = Path(__file__).resolve().parents[1] / "app/research/terminal"
    revision = subprocess.check_output(["git", "-C", str(args.checkout), "rev-parse", "HEAD"], text=True).strip()
    entries = []
    for relative in FILES:
        source = args.checkout / "site" / relative
        body = source.read_bytes()
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        entries.append({"source": relative, "sha256": hashlib.sha256(body).hexdigest(), "mode": "unchanged"})
    source = args.checkout / "site/services/ticker_recap/hub.ts"
    body = source.read_text(encoding="utf-8")
    start = body.index("export type HubPost")
    end = body.index("export async function additionalHubPosts")
    schema = next(line for line in body.splitlines() if line.startswith("export const digestSchema="))
    target = destination / "services/ticker_recap/hub-core.ts"
    target.write_text('import { z } from "zod";\n' + body[start:end] + schema + "\n", encoding="utf-8")
    entries.append({"source": "services/ticker_recap/hub.ts", "target": "services/ticker_recap/hub-core.ts",
                    "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                    "mode": "verbatim extraction: HubPost, mergeHubPosts, hubConsensus, digestSchema"})
    (destination / "provenance.json").write_text(json.dumps({
        "repository": "https://github.com/seytuev/stvterminal", "commit": revision,
        "purpose": "Original algorithms integrated into Market Researcher; no running terminal required.",
        "files": entries,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
