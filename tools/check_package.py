import zipfile
from pathlib import Path
wheel = next(Path("dist").glob("*.whl"))
with zipfile.ZipFile(wheel) as archive:
    names = set(archive.namelist())
    for name in ("app/schema.sql", "app/web/static/index.html", "app/web/static/vendor/lightweight-charts.js",
                 "app/notify/h1_image.py", "app/web/static/h1_snapshot.html", "app/web/static/h1_snapshot.js"):
        assert name in names, f"Missing package resource: {name}"
print("Package resources verified")
