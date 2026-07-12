"""
Cloudflare R2 uploader for publishing the GTFS-RT feed.

Uploads serialized protobuf bytes to an R2 bucket so downstream applications
can consume the feed directly from object storage instead of an HTTP endpoint.

Credentials are read from the same environment variables used by the rest of
the pipeline:
- R2_ACCOUNT_ID
- R2_ACCESS_KEY_ID
- R2_SECRET_ACCESS_KEY
- R2_BUCKET_NAME
"""

import os
import asyncio
import logging
from typing import Optional

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)


class R2Uploader:
    """Upload bytes to a Cloudflare R2 bucket without blocking the event loop."""

    def __init__(self):
        self.account_id = os.getenv("R2_ACCOUNT_ID")
        self.access_key_id = os.getenv("R2_ACCESS_KEY_ID")
        self.secret_access_key = os.getenv("R2_SECRET_ACCESS_KEY")
        self.bucket_name = os.getenv("R2_BUCKET_NAME")
        self._client = None

    @property
    def enabled(self) -> bool:
        return all([
            self.account_id,
            self.access_key_id,
            self.secret_access_key,
            self.bucket_name,
        ])

    def _get_client(self):
        if self._client is None:
            endpoint_url = f"https://{self.account_id}.r2.cloudflarestorage.com"
            self._client = boto3.client(
                "s3",
                endpoint_url=endpoint_url,
                aws_access_key_id=self.access_key_id,
                aws_secret_access_key=self.secret_access_key,
                region_name="auto",
                config=BotoConfig(retries={"max_attempts": 3, "mode": "standard"}),
            )
        return self._client

    def _put_object(self, data: bytes, key: str, content_type: str) -> None:
        self._get_client().put_object(
            Bucket=self.bucket_name,
            Key=key,
            Body=data,
            ContentType=content_type,
            CacheControl="no-store, no-cache, must-revalidate",
        )

    async def upload_bytes(
        self,
        data: bytes,
        key: str,
        content_type: str = "application/octet-stream",
    ) -> bool:
        """Upload bytes to R2 under ``key``. Returns True on success."""
        if not self.enabled:
            logger.warning(
                "R2 credentials not configured; skipping upload of '%s'", key
            )
            return False

        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None, self._put_object, data, key, content_type
            )
            logger.debug("Uploaded %d bytes to R2 as '%s'", len(data), key)
            return True
        except (BotoCoreError, ClientError) as e:
            logger.error("Failed to upload '%s' to R2: %s", key, e)
            return False
        except Exception as e:
            logger.error("Unexpected error uploading '%s' to R2: %s", key, e)
            return False
