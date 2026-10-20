"""Fixed-template map tile proxy.

Only two upstream URL templates exist (map and traffic flow), both on
api.tomtom.com and set by the operator. Clients can only choose z/x/y
integers, so this is not an open proxy. Tiles are not stored on disk; the
provider's cache headers are passed through so the browser caches them.
"""

from __future__ import annotations

import httpx
from fastapi import HTTPException, Request, Response

from .budget import Budget
from .config import Config

PASS_HEADERS = ("cache-control", "etag", "last-modified", "expires", "content-type")


class TileProxy:
    def __init__(self, cfg: Config, budget_factory, transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = cfg
        self.budget_factory = budget_factory
        self.client = httpx.AsyncClient(timeout=15, transport=transport, follow_redirects=False,
                                        headers={"User-Agent": "8star-routes/1.0"})

    async def close(self) -> None:
        await self.client.aclose()

    async def fetch(self, kind: str, z: int, x: int, y: int, request: Request) -> Response:
        if self.cfg.map_tile_mode != "proxy":
            raise HTTPException(404, "Tile proxy disabled")
        if kind not in ("map", "traffic"):
            raise HTTPException(404)
        if not (0 <= z <= 22 and 0 <= x < 2 ** z and 0 <= y < 2 ** z):
            raise HTTPException(400, "Tile out of range")
        if not self.cfg.tomtom_api_key:
            raise HTTPException(503, "No TomTom key configured for map tiles")
        budget: Budget = self.budget_factory()
        if not budget.tile_reserve(kind):
            return Response(status_code=503, headers={"X-Budget-Blocked": kind, "Cache-Control": "no-store"},
                            content=f"{kind} tile budget ceiling reached")
        template = self.cfg.map_tile_url if kind == "map" else self.cfg.traffic_tile_url
        url = template.format(z=z, x=x, y=y)
        fwd = {h: request.headers[h] for h in ("if-none-match", "if-modified-since") if h in request.headers}
        try:
            up = await self.client.get(url, params={"key": self.cfg.tomtom_api_key}, headers=fwd)
        except httpx.HTTPError:
            budget.tile_error(kind)
            raise HTTPException(502, "Tile provider unreachable") from None
        if up.status_code not in (200, 304):
            budget.tile_error(kind)
            return Response(status_code=502, content=f"Tile provider answered {up.status_code}", headers={"Cache-Control": "no-store"})
        headers = {h: up.headers[h] for h in PASS_HEADERS if h in up.headers}
        return Response(status_code=up.status_code, content=up.content if up.status_code == 200 else b"", headers=headers)
