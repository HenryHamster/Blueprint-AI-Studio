import os
import logging
import asyncio
from google.cloud import secretmanager

logger = logging.getLogger(__name__)

GCP_PROJECT_ID = os.getenv("GCP_PROJECT_ID", "your-gcp-project-id")

async def store_user_secret(user_id: str, secret_name: str, secret_value: str) -> str:
    """
    Stores a sensitive token (like a GitHub PAT) in Google Secret Manager.
    Returns the GCP Secret Resource Name.
    """
    full_secret_id = f"user-{user_id}-{secret_name}"
    logger.info(f"Storing secret {full_secret_id} in Secret Manager...")
    
    def _store():
        client = secretmanager.SecretManagerServiceClient()
        parent = f"projects/{GCP_PROJECT_ID}"
        
        # 1. Create the secret (if it doesn't exist)
        try:
            secret = client.get_secret(request={"name": f"{parent}/secrets/{full_secret_id}"})
        except Exception:
            secret = client.create_secret(
                request={
                    "parent": parent,
                    "secret_id": full_secret_id,
                    "secret": {"replication": {"automatic": {}}},
                }
            )
        
        # 2. Add the secret version (the actual payload)
        payload = secret_value.encode("UTF-8")
        response = client.add_secret_version(
            request={"parent": secret.name, "payload": {"data": payload}}
        )
        return response.name
        
    try:
        version_name = await asyncio.to_thread(_store)
        return version_name
    except Exception as e:
        logger.error(f"Failed to store secret {full_secret_id}: {e}")
        raise e

async def get_user_secret(secret_resource_name: str) -> str:
    """
    Retrieves a sensitive token (like a GitHub PAT) from Google Secret Manager.
    """
    logger.info(f"Retrieving secret {secret_resource_name} from Secret Manager...")
    
    def _get():
        client = secretmanager.SecretManagerServiceClient()
        response = client.access_secret_version(request={"name": secret_resource_name})
        return response.payload.data.decode("UTF-8")
        
    try:
        secret_value = await asyncio.to_thread(_get)
        return secret_value
    except Exception as e:
        logger.error(f"Failed to retrieve secret {secret_resource_name}: {e}")
        raise e
