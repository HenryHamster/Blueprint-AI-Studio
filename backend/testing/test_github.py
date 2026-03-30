import pytest
from httpx import AsyncClient

@pytest.mark.asyncio
async def test_validate_github_token_invalid_format(client: AsyncClient):
    """
    Tests that a classic token (ghp_) is immediately rejected without hitting the GitHub API.
    """
    payload = {"token": "ghp_classic_token_123"}
    response = await client.post("/api/github/validate-token", json=payload)
    
    assert response.status_code == 400
    assert "Invalid token format" in response.json()["detail"]

@pytest.mark.asyncio
async def test_validate_github_token_success(client: AsyncClient, mocker):
    """
    Tests a successful validation of a Fine-Grained PAT.
    We mock the httpx.AsyncClient.get call so we don't actually hit GitHub.
    """
    # Mock the httpx.AsyncClient.get call inside validate_fine_grained_pat
    mock_get = mocker.patch("httpx.AsyncClient.get")
    
    # Create a mock response object
    class MockResponse:
        def __init__(self, status_code, json_data):
            self.status_code = status_code
            self._json_data = json_data
        def json(self):
            return self._json_data
            
    # Tell the mock to return a 200 OK with a fake username
    mock_get.return_value = MockResponse(200, {"login": "testuser"})
    
    payload = {"token": "github_pat_valid_token_123"}
    response = await client.post("/api/github/validate-token", json=payload)
    
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "valid"
    assert data["user"] == "testuser"

@pytest.mark.asyncio
async def test_validate_github_token_unauthorized(client: AsyncClient, mocker):
    """
    Tests the scenario where GitHub rejects the token (e.g., expired or revoked).
    """
    mock_get = mocker.patch("httpx.AsyncClient.get")
    
    class MockResponse:
        def __init__(self, status_code, json_data):
            self.status_code = status_code
            self._json_data = json_data
        def json(self):
            return self._json_data
            
    # Tell the mock to return a 401 Unauthorized
    mock_get.return_value = MockResponse(401, {"message": "Bad credentials"})
    
    payload = {"token": "github_pat_expired_token_123"}
    response = await client.post("/api/github/validate-token", json=payload)
    
    assert response.status_code == 401
    assert "invalid or expired" in response.json()["detail"]
