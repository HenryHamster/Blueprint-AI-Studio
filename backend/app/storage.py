import os
import datetime
import logging
import asyncio
import json
from google.cloud import storage

logger = logging.getLogger(__name__)

GCP_PROJECT_ID = os.getenv("GCP_PROJECT_ID", "your-gcp-project-id")
ARTIFACT_BUCKET_NAME = os.getenv("ARTIFACT_BUCKET_NAME", "blueprint-agent-artifacts")

async def upload_json_payload(task_id: str, payload_data: list | dict) -> str:
    """
    Uploads a JSON payload to GCS and returns the gs:// URI.
    This is used to pass large payloads (like images or complex instructions) to the VPM.
    """
    object_name = f"tasks/{task_id}/payload.json"
    logger.info(f"Uploading payload to gs://{ARTIFACT_BUCKET_NAME}/{object_name}")

    def _upload():
        storage_client = storage.Client(project=GCP_PROJECT_ID)
        bucket = storage_client.bucket(ARTIFACT_BUCKET_NAME)
        blob = bucket.blob(object_name)
        blob.upload_from_string(
            data=json.dumps(payload_data),
            content_type="application/json"
        )
        return f"gs://{ARTIFACT_BUCKET_NAME}/{object_name}"

    try:
        uri = await asyncio.to_thread(_upload)
        return uri
    except Exception as e:
        logger.error(f"Failed to upload payload to GCS: {e}")
        raise e

async def generate_upload_signed_url(task_id: str, filename: str, expiration_minutes: int = 15) -> str:
    """
    Generates a v4 signed URL for uploading an object to GCS.
    This allows the agent VM to upload files directly to Cloud Storage
    without needing IAM permissions or routing heavy traffic through the API.
    """
    # Sanitize filename to prevent path traversal
    safe_filename = os.path.basename(filename)
    object_name = f"tasks/{task_id}/{safe_filename}"
    logger.info(f"Generating upload signed URL for gs://{ARTIFACT_BUCKET_NAME}/{object_name}")

    def _generate():
        storage_client = storage.Client(project=GCP_PROJECT_ID)
        bucket = storage_client.bucket(ARTIFACT_BUCKET_NAME)
        blob = bucket.blob(object_name)
        return blob.generate_signed_url(
            version="v4",
            expiration=datetime.timedelta(minutes=expiration_minutes),
            method="PUT",
            content_type="application/octet-stream", # Can be overridden by client
        )

    try:
        url = await asyncio.to_thread(_generate)
        return url
    except Exception as e:
        logger.error(f"Failed to generate upload signed URL: {e}")
        raise e
