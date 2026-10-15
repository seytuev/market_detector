"""Owner creates snapshots; anyone holding the image link can view that PNG."""
import asyncio

from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse

from ..services.chart_links import MAX_IMAGE_BYTES, chart_image_path, save_chart_image


def register_chart_link_routes(app, settings, require_auth):
    @app.post("/api/chart-images", dependencies=[Depends(require_auth)])
    async def create_chart_image(request: Request):
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > MAX_IMAGE_BYTES:
                raise HTTPException(413, "Размер изображения должен быть до 10 МБ.")
        try:
            path = await asyncio.to_thread(save_chart_image, settings, bytes(data))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        base = settings.public_base_url.rstrip("/") or str(request.base_url).rstrip("/")
        return {"url": base + path}

    @app.get("/charts/{key}.png")
    def get_chart_image(key: str):
        path = chart_image_path(settings, key)
        if path is None:
            raise HTTPException(404, "Снимок не найден.")
        return FileResponse(path, media_type="image/png", headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "X-Content-Type-Options": "nosniff",
        })
