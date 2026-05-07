import uuid
import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_list_users_empty(client: AsyncClient):
    response = await client.get("/api/admin/users")
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.asyncio
async def test_add_user(client: AsyncClient):
    response = await client.post(
        "/api/admin/users", json={"email": "alice@example.com", "role": "user"}
    )
    assert response.status_code == 201
    data = response.json()
    assert data["email"] == "alice@example.com"
    assert data["role"] == "user"
    assert data["id"]
    assert data["created_at"]
    assert data["last_login_at"] is None


@pytest.mark.asyncio
async def test_add_user_admin_role(client: AsyncClient):
    response = await client.post(
        "/api/admin/users", json={"email": "bob@example.com", "role": "admin"}
    )
    assert response.status_code == 201
    assert response.json()["role"] == "admin"


@pytest.mark.asyncio
async def test_add_user_default_role(client: AsyncClient):
    response = await client.post(
        "/api/admin/users", json={"email": "carol@example.com"}
    )
    assert response.status_code == 201
    assert response.json()["role"] == "user"


@pytest.mark.asyncio
async def test_add_user_duplicate(client: AsyncClient):
    await client.post("/api/admin/users", json={"email": "dup@example.com"})
    response = await client.post("/api/admin/users", json={"email": "dup@example.com"})
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_add_user_invalid_email(client: AsyncClient):
    response = await client.post(
        "/api/admin/users", json={"email": "not-an-email", "role": "user"}
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_add_user_invalid_role(client: AsyncClient):
    response = await client.post(
        "/api/admin/users", json={"email": "x@example.com", "role": "superadmin"}
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_update_user_role(client: AsyncClient):
    create = await client.post(
        "/api/admin/users", json={"email": "upd@example.com", "role": "user"}
    )
    user_id = create.json()["id"]

    response = await client.patch(f"/api/admin/users/{user_id}", json={"role": "admin"})
    assert response.status_code == 200
    assert response.json()["role"] == "admin"


@pytest.mark.asyncio
async def test_update_user_not_found(client: AsyncClient):
    fake_id = str(uuid.uuid4())
    response = await client.patch(f"/api/admin/users/{fake_id}", json={"role": "admin"})
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_delete_user(client: AsyncClient):
    create = await client.post("/api/admin/users", json={"email": "del@example.com"})
    user_id = create.json()["id"]

    response = await client.delete(f"/api/admin/users/{user_id}")
    assert response.status_code == 200

    # Verify removed from list
    listing = await client.get("/api/admin/users")
    emails = [u["email"] for u in listing.json()]
    assert "del@example.com" not in emails


@pytest.mark.asyncio
async def test_delete_user_not_found(client: AsyncClient):
    fake_id = str(uuid.uuid4())
    response = await client.delete(f"/api/admin/users/{fake_id}")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_delete_self_blocked(client: AsyncClient):
    # The auth override returns email "test@example.com" — create a user with that email
    create = await client.post(
        "/api/admin/users", json={"email": "test@example.com", "role": "admin"}
    )
    user_id = create.json()["id"]

    response = await client.delete(f"/api/admin/users/{user_id}")
    assert response.status_code == 400
    assert "Cannot remove your own" in response.json()["error"]["message"]


@pytest.mark.asyncio
async def test_list_users_populated(client: AsyncClient):
    await client.post(
        "/api/admin/users", json={"email": "a@example.com", "role": "admin"}
    )
    await client.post(
        "/api/admin/users", json={"email": "b@example.com", "role": "user"}
    )

    response = await client.get("/api/admin/users")
    assert response.status_code == 200
    users = response.json()
    assert len(users) == 2
    emails = {u["email"] for u in users}
    assert emails == {"a@example.com", "b@example.com"}


@pytest.mark.asyncio
async def test_get_me(client: AsyncClient):
    response = await client.get("/api/me")
    assert response.status_code == 200
    data = response.json()
    assert data["email"] == "test@example.com"
    assert data["role"] == "admin"
    assert data["user_id"] == "test-user-123"
