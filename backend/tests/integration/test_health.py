import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_health_and_openapi_are_available_without_authentication(client):
    live = await client.get("/health/live")
    assert live.status_code == 200
    assert live.json()["status"] == "ok"
    assert (await client.get("/health/ready")).status_code == 200
    assert (await client.get("/docs")).status_code == 200
    response = await client.get("/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    assert "/api/v1/auth/login" in schema["paths"]
    assert "post" in schema["paths"]["/api/v1/records"]
    assert schema["components"]["securitySchemes"]
