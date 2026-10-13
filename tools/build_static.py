"""Vendor the fixed chart release after verifying the npm tarball integrity."""
import base64
import hashlib
import io
import json
import tarfile
from pathlib import Path
from urllib.request import urlopen

VERSION = "4.2.0"
ROOT = Path(__file__).resolve().parents[1]


def main():
    with urlopen(f"https://registry.npmjs.org/lightweight-charts/{VERSION}", timeout=30) as response:
        metadata = json.load(response)
    dist = metadata["dist"]
    with urlopen(dist["tarball"], timeout=60) as response:
        archive = response.read()
    algorithm, encoded = dist["integrity"].split("-", 1)
    if algorithm != "sha512" or hashlib.sha512(archive).digest() != base64.b64decode(encoded):
        raise RuntimeError("Chart package integrity mismatch")
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        member = tar.extractfile("package/dist/lightweight-charts.standalone.production.js")
        if member is None:
            raise RuntimeError("Standalone chart asset missing")
        content = member.read()
    target = ROOT / "app/web/static/vendor"
    target.mkdir(parents=True, exist_ok=True)
    (target / "lightweight-charts.js").write_bytes(content)
    (target / "chart-package.json").write_text(json.dumps({"version": VERSION, "integrity": dist["integrity"], "sha256": hashlib.sha256(content).hexdigest()}, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
