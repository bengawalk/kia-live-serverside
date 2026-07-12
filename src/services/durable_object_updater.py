"""
Publishes GTFS-RT feed updates to the Cloudflare Durable Object broadcaster.

The Durable Object (see worker/) holds the most recent rt.pb bytes and
fans them out over WebSocket to connected clients, so consumers get live
updates without polling R2 directly. This module pushes each new rt.pb
payload to the Worker's internal publish endpoint.

Configuration is read from environment variables:
- DO_WORKER_URL: base URL of the deployed Worker (e.g. https://kia-live-rt-broadcaster.<account>.workers.dev)
- DO_UPDATE_SECRET: shared secret sent as a Bearer token; must match the
  Worker's DO_UPDATE_SECRET
"""

import os
import logging

import aiohttp

logger = logging.getLogger(__name__)


class DurableObjectUpdater:
    """Push new rt.pb bytes to the Durable Object broadcaster over HTTP."""

    def __init__(self):
        worker_url = os.getenv("DO_WORKER_URL", "").rstrip("/")
        self.worker_url = worker_url
        self.update_secret = os.getenv("DO_UPDATE_SECRET")

    @property
    def enabled(self) -> bool:
        return bool(self.worker_url and self.update_secret)

    async def publish(self, data: bytes) -> bool:
        """Push ``data`` to the Worker's /rt/publish endpoint. Returns True on success."""
        if not self.enabled:
            logger.warning(
                "Durable Object worker not configured; skipping rt.pb broadcast"
            )
            return False

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.worker_url}/rt/publish",
                    data=data,
                    headers={
                        "Authorization": f"Bearer {self.update_secret}",
                        "Content-Type": "application/x-protobuf",
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as response:
                    if response.status != 200:
                        body = await response.text()
                        logger.error(
                            "Durable Object publish failed (%d): %s",
                            response.status,
                            body,
                        )
                        return False
                    logger.debug("Broadcast %d bytes via Durable Object", len(data))
                    return True
        except Exception as e:
            logger.error("Failed to publish rt.pb to Durable Object: %s", e)
            return False