import pytest
from httpx import ASGITransport, AsyncClient

from meldx.app.main import create_app


@pytest.mark.asyncio
async def test_health() -> None:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True}
