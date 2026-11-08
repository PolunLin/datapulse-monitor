from datetime import UTC, datetime
from decimal import Decimal

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_crud_round_trip_preserves_decimal_and_normalizes_timestamp(
    client, make_account, record_payload
):
    owner = await make_account("owner@example.com")
    created = await client.post("/api/v1/records", json=record_payload, headers=owner.headers)
    assert created.status_code == 201, created.text
    record = created.json()
    assert record["created_by"] == owner.id
    assert Decimal(record["value"]) == Decimal("23.125")
    assert datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00")) == datetime(
        2026, 11, 8, 1, tzinfo=UTC
    )

    url = f"/api/v1/records/{record['id']}"
    fetched = await client.get(url, headers=owner.headers)
    assert fetched.status_code == 200
    assert fetched.json()["id"] == record["id"]

    updated = await client.patch(
        url, json={"title": "updated sample", "value": "0.000001"}, headers=owner.headers
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["title"] == "updated sample"
    assert Decimal(updated.json()["value"]) == Decimal("0.000001")
    assert updated.json()["category"] == record["category"]
    assert updated.json()["created_by"] == owner.id

    deleted = await client.delete(url, headers=owner.headers)
    assert deleted.status_code == 204
    assert deleted.content == b""
    assert (await client.get(url, headers=owner.headers)).status_code == 404


async def test_another_user_cannot_modify_but_admin_can(client, make_account, record_payload):
    owner = await make_account("creator@example.com")
    stranger = await make_account("stranger@example.com")
    admin = await make_account("admin@example.com", role="admin")
    created = await client.post("/api/v1/records", json=record_payload, headers=owner.headers)
    assert created.status_code == 201
    url = f"/api/v1/records/{created.json()['id']}"

    assert (await client.get(url, headers=stranger.headers)).status_code == 200
    assert (
        await client.patch(url, json={"title": "stolen"}, headers=stranger.headers)
    ).status_code == 403
    assert (await client.delete(url, headers=stranger.headers)).status_code == 403

    updated = await client.patch(url, json={"title": "admin edit"}, headers=admin.headers)
    assert updated.status_code == 200
    assert updated.json()["created_by"] == owner.id
    assert (await client.delete(url, headers=admin.headers)).status_code == 204


async def test_viewer_can_read_but_cannot_create_update_or_delete(
    client, make_account, record_payload
):
    owner = await make_account("producer@example.com")
    viewer = await make_account("viewer@example.com", role="viewer")
    created = await client.post("/api/v1/records", json=record_payload, headers=owner.headers)
    assert created.status_code == 201
    url = f"/api/v1/records/{created.json()['id']}"

    assert (await client.get("/api/v1/records", headers=viewer.headers)).status_code == 200
    assert (await client.get(url, headers=viewer.headers)).status_code == 200
    assert (
        await client.post("/api/v1/records", json=record_payload, headers=viewer.headers)
    ).status_code == 403
    assert (
        await client.patch(url, json={"title": "viewer edit"}, headers=viewer.headers)
    ).status_code == 403
    assert (await client.delete(url, headers=viewer.headers)).status_code == 403


async def test_role_downgrade_applies_to_already_issued_tokens(
    client, make_account, record_payload, app
):
    from app.models.user import User

    account = await make_account("downgrade@example.com")
    created = await client.post("/api/v1/records", json=record_payload, headers=account.headers)
    assert created.status_code == 201
    async with app.state.session_factory() as session:
        async with session.begin():
            user = await session.get(User, account.id)
            user.role = "viewer"

    response = await client.patch(
        f"/api/v1/records/{created.json()['id']}",
        json={"value": "99"},
        headers=account.headers,
    )
    assert response.status_code == 403


async def test_record_owner_cannot_be_injected_or_changed(client, make_account, record_payload):
    account = await make_account("ownership@example.com")
    forbidden = await client.post(
        "/api/v1/records",
        json={**record_payload, "created_by": account.id + 1},
        headers=account.headers,
    )
    assert forbidden.status_code == 422
    created = await client.post("/api/v1/records", json=record_payload, headers=account.headers)
    assert created.status_code == 201
    response = await client.patch(
        f"/api/v1/records/{created.json()['id']}",
        json={"created_by": account.id + 1},
        headers=account.headers,
    )
    assert response.status_code == 422


async def test_record_validation_rejects_unusable_data(client, make_account, record_payload):
    account = await make_account("validation@example.com")
    for changed in (
        {"title": "   "},
        {"title": "a" * 201},
        {"category": "   "},
        {"value": "NaN"},
        {"value": "Infinity"},
        {"value": "1000000000000"},
        {"value": "0.0000001"},
        {"timestamp": "2026-11-08T09:00:00"},
    ):
        response = await client.post(
            "/api/v1/records", json={**record_payload, **changed}, headers=account.headers
        )
        assert response.status_code == 422, (changed, response.text)


async def test_patch_rejects_empty_body_and_null_required_fields(
    client, make_account, record_payload
):
    account = await make_account("patch@example.com")
    created = await client.post("/api/v1/records", json=record_payload, headers=account.headers)
    assert created.status_code == 201
    url = f"/api/v1/records/{created.json()['id']}"
    for patch in ({}, {"title": None}, {"value": None}, {"category": None}, {"timestamp": None}):
        response = await client.patch(url, json=patch, headers=account.headers)
        assert response.status_code == 422, (patch, response.text)


async def test_list_filters_sort_and_pagination(client, make_account, record_payload):
    owner = await make_account("filter-owner@example.com")
    other = await make_account("filter-other@example.com")
    for title, value, category, timestamp, actor in (
        ("cpu first", "5", "device-001", "2026-11-08T00:00:00Z", owner),
        ("cpu middle", "20", "device-001", "2026-11-08T01:00:00Z", owner),
        ("cpu last", "10", "device-001", "2026-11-08T02:00:00Z", other),
        ("memory first", "30", "device-002", "2026-11-08T01:00:00Z", owner),
    ):
        response = await client.post(
            "/api/v1/records",
            json={
                **record_payload,
                "title": title,
                "value": value,
                "category": category,
                "timestamp": timestamp,
            },
            headers=actor.headers,
        )
        assert response.status_code == 201, response.text

    response = await client.get(
        "/api/v1/records",
        params={"sort_by": "value", "sort_order": "desc", "page": 2, "page_size": 2},
        headers=owner.headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert (data["total"], data["page"], data["page_size"]) == (4, 2, 2)
    assert [Decimal(item["value"]) for item in data["items"]] == [Decimal(10), Decimal(5)]

    response = await client.get(
        "/api/v1/records",
        params={
            "category": "device-001",
            "created_by": owner.id,
            "q": "middle",
            "start_time": "2026-11-08T01:00:00Z",
            "end_time": "2026-11-08T02:00:00Z",
        },
        headers=owner.headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["title"] == "cpu middle"

    # End of a time interval is exclusive; offset timestamps compare as UTC.
    response = await client.get(
        "/api/v1/records",
        params={
            "category": "device-001",
            "start_time": "2026-11-08T09:00:00+08:00",
            "end_time": "2026-11-08T10:00:00+08:00",
        },
        headers=owner.headers,
    )
    assert response.status_code == 200
    assert [item["title"] for item in response.json()["items"]] == ["cpu middle"]


async def test_equal_sort_values_have_stable_nonoverlapping_pages(
    client, make_account, record_payload
):
    account = await make_account("ties@example.com")
    ids = set()
    for number in range(4):
        response = await client.post(
            "/api/v1/records",
            json={**record_payload, "title": f"same timestamp {number}"},
            headers=account.headers,
        )
        assert response.status_code == 201
        ids.add(response.json()["id"])

    pages = []
    for page in (1, 2):
        params = {"sort_by": "timestamp", "sort_order": "desc", "page_size": 2, "page": page}
        first = await client.get("/api/v1/records", params=params, headers=account.headers)
        repeated = await client.get("/api/v1/records", params=params, headers=account.headers)
        assert first.status_code == repeated.status_code == 200
        page_ids = [item["id"] for item in first.json()["items"]]
        assert page_ids == [item["id"] for item in repeated.json()["items"]]
        pages.extend(page_ids)
    assert len(pages) == len(set(pages)) == 4
    assert set(pages) == ids


async def test_invalid_query_options_are_rejected(client, make_account):
    account = await make_account("query@example.com")
    for params in (
        {"page": 0},
        {"page_size": 101},
        {"sort_by": "password_hash"},
        {"sort_order": "sideways"},
        {"start_time": "2026-11-08T09:00:00"},
        {"start_time": "2026-11-09T00:00:00Z", "end_time": "2026-11-08T00:00:00Z"},
    ):
        response = await client.get("/api/v1/records", params=params, headers=account.headers)
        assert response.status_code == 422, (params, response.text)


async def test_missing_records_return_404(client, make_account):
    account = await make_account("missing-record@example.com")
    url = "/api/v1/records/2147483647"
    assert (await client.get(url, headers=account.headers)).status_code == 404
    assert (
        await client.patch(url, json={"title": "missing"}, headers=account.headers)
    ).status_code == 404
    assert (await client.delete(url, headers=account.headers)).status_code == 404


async def test_records_require_authentication(client, record_payload):
    assert (await client.get("/api/v1/records")).status_code == 401
    assert (await client.get("/api/v1/records/1")).status_code == 401
    assert (await client.post("/api/v1/records", json=record_payload)).status_code == 401
    assert (
        await client.patch("/api/v1/records/1", json={"title": "unauthorized"})
    ).status_code == 401
    assert (await client.delete("/api/v1/records/1")).status_code == 401


async def test_device_category_validation_applies_to_create_and_edit(
    client, make_account, record_payload
):
    owner = await make_account("device-categories@example.com")
    for category in ("device-001", "device-002", "device-003"):
        created = await client.post(
            "/api/v1/records", json={**record_payload, "category": category}, headers=owner.headers
        )
        assert created.status_code == 201, created.text
        assert created.json()["category"] == category
    url = f"/api/v1/records/{created.json()['id']}"
    for category in ("temperature", "device-004", "legacy", "設備 A"):
        assert (
            await client.post(
                "/api/v1/records",
                json={**record_payload, "category": category},
                headers=owner.headers,
            )
        ).status_code == 422
        assert (
            await client.patch(url, json={"category": category}, headers=owner.headers)
        ).status_code == 422
    updated = await client.patch(url, json={"category": "device-001"}, headers=owner.headers)
    assert updated.status_code == 200
    assert updated.json()["category"] == "device-001"


async def test_legacy_category_remains_readable_searchable_and_can_be_migrated(
    client, app, make_account
):
    from app.models.record import Record

    owner = await make_account("legacy-category@example.com")
    async with app.state.session_factory.begin() as session:
        record = Record(
            title="old measurement", value=Decimal("12"), category="cpu", created_by=owner.id
        )
        session.add(record)
        await session.flush()
        identifier = record.id
    found = await client.get("/api/v1/records", params={"category": "cpu"}, headers=owner.headers)
    assert found.status_code == 200
    assert [row["id"] for row in found.json()["items"]] == [identifier]
    url = f"/api/v1/records/{identifier}"
    updated = await client.patch(url, json={"value": "13"}, headers=owner.headers)
    assert updated.status_code == 200
    assert updated.json()["category"] == "cpu"
    migrated = await client.patch(url, json={"category": "device-002"}, headers=owner.headers)
    assert migrated.status_code == 200
    assert migrated.json()["category"] == "device-002"
