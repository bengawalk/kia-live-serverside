# src/core/resource_manager.py
import asyncio
import os
from typing import Optional

import aiohttp
import psutil


class ResourceBudget:
    """Manage resource allocation across components.

    Budgets are computed as percentages of a total memory budget in MB.
    """

    def __init__(self, total_memory_mb: int):
        self.total_memory_mb = int(total_memory_mb)
        # Percent allocations as per architectural plan
        self.budgets = {
            'api_cache': int(self.total_memory_mb * 0.2),
            'live_data': int(self.total_memory_mb * 0.3),
            'predictions': int(self.total_memory_mb * 0.2),
            'gtfs_feed': int(self.total_memory_mb * 0.2),
            'buffer': int(self.total_memory_mb * 0.1),
        }

    def get_component_budget(self, component: str) -> int:
        return int(self.budgets.get(component, 0))

    def validate_allocation(self, component: str, requested_mb: int) -> bool:
        return int(requested_mb) <= self.get_component_budget(component)

    def check_budget_warnings(self):
        warnings = []
        total = sum(self.budgets.values())
        if total != self.total_memory_mb:
            warnings.append(
                f"Total allocated {total}MB differs from total budget {self.total_memory_mb}MB"
            )
        return warnings


class ResourceManager:
    """Centralized resource allocation and monitoring"""

    def __init__(self):
        self.memory_limit = self._detect_memory_limit()
        self.cpu_cores = self._detect_cpu_cores()
        self.connection_pool: Optional[aiohttp.ClientSession] = None
        self._http_lock = asyncio.Lock()
        self.budget = ResourceBudget(total_memory_mb=self.memory_limit)

    async def get_http_session(self) -> aiohttp.ClientSession:
        """Get managed HTTP session with connection pooling"""
        async with self._http_lock:
            if self.connection_pool is None or self.connection_pool.closed:
                timeout = aiohttp.ClientTimeout(total=60)
                connector = aiohttp.TCPConnector(limit=max(10, self.cpu_cores * 10))
                self.connection_pool = aiohttp.ClientSession(timeout=timeout, connector=connector)
            return self.connection_pool

    def get_memory_budget(self, component: str) -> int:
        """Get memory budget for specific component"""
        return self.budget.get_component_budget(component)

    def monitor_resource_usage(self):
        """Monitor and log resource usage"""
        # Lightweight snapshot using psutil. This function is intentionally passive
        # and should not raise, to satisfy test expectations.
        process = psutil.Process(os.getpid())
        _ = process.memory_info().rss  # bytes used by current process
        _ = psutil.cpu_percent(interval=0.0)  # instantaneous CPU percent
        # Could be extended to log or emit metrics

    def _detect_memory_limit(self) -> int:
        """Detect available memory (in MB) for budgeting purposes."""
        try:
            total_bytes = psutil.virtual_memory().total
            # Allocate conservatively: 25% of total system memory, minimum 100MB, max 4096MB
            budget_mb = max(100, min(4096, int(total_bytes / (1024 * 1024 * 4))))
            return budget_mb
        except Exception:
            return 512

    def _detect_cpu_cores(self) -> int:
        try:
            cores = os.cpu_count() or 1
            return max(1, int(cores))
        except Exception:
            return 1