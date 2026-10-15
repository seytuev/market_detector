from io import BytesIO
from urllib.parse import urlparse

from PIL import Image

from tests.test_web import AUTH, client, db, settings  # noqa: F401


def png_bytes():
    output = BytesIO()
    Image.new("RGB", (20, 10), "#62c9b0").save(output, format="PNG")
    return output.getvalue()


def test_only_owner_creates_but_link_opens_without_auth(client, settings):
    data = png_bytes()
    assert client.post("/api/chart-images", content=data).status_code == 401
    response = client.post("/api/chart-images", content=data, headers=AUTH)
    assert response.status_code == 200
    url = response.json()["url"]
    assert settings.auth_token not in url
    path = urlparse(url).path
    image = client.get(path)
    assert image.status_code == 200 and image.content == data
    assert image.headers["content-type"] == "image/png"
    assert "attachment" not in image.headers.get("content-disposition", "")
    assert client.post("/api/chart-images", content=data, headers=AUTH).json()["url"] == url
    assert client.get("/charts/no-such-image.png").status_code == 404


def test_public_base_and_image_validation(client, settings):
    settings.public_base_url = "https://charts.example.com/"
    response = client.post("/api/chart-images", content=png_bytes(), headers=AUTH)
    assert response.json()["url"].startswith("https://charts.example.com/charts/")
    for bad in (b"", b"<html>not an image</html>", b"\x89PNG\r\n\x1a\nfake"):
        assert client.post("/api/chart-images", content=bad, headers=AUTH).status_code == 422
    assert client.post("/api/chart-images", content=b"x" * (10 * 1024 * 1024 + 1), headers=AUTH).status_code == 413


def test_links_survive_new_app_instance(client, settings, db):
    from fastapi.testclient import TestClient
    from app.web.api import create_app
    path = urlparse(client.post("/api/chart-images", content=png_bytes(), headers=AUTH).json()["url"]).path
    restarted = TestClient(create_app(db, settings))
    assert restarted.get(path).content == png_bytes()
