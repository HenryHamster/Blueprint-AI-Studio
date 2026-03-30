import pytest
from app.storage import generate_upload_signed_url

@pytest.mark.asyncio
async def test_generate_upload_signed_url(mocker):
    """
    Tests that the storage module generates a signed URL correctly using the GCP SDK.
    We mock the google.cloud.storage.Client so no real network requests are made.
    """
    # 1. Mock the GCS Client and its chained methods
    mock_storage = mocker.patch("app.storage.storage")
    mock_client = mock_storage.Client.return_value
    mock_bucket = mock_client.bucket.return_value
    mock_blob = mock_bucket.blob.return_value
    
    # 2. Tell the mock to return a fake URL
    mock_blob.generate_signed_url.return_value = "https://fake-signed-url.com/upload"

    # 3. Call our function
    url = await generate_upload_signed_url("task-123", "logs.txt")
    
    # 4. Verify the result and that the mock was called
    assert url == "https://fake-signed-url.com/upload"
    mock_blob.generate_signed_url.assert_called_once()
