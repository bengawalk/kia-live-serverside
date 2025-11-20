"""
BMTC API data source for the new architecture.
Streaming API client with memory optimization and bounded resource usage.
"""

import asyncio
import json
import time
import random
import gc
from typing import AsyncIterator, Optional, Dict, List
from datetime import datetime, timedelta
import aiohttp
import pytz

from ..models.vehicle import Vehicle, StationInfo, RouteInfo, VehiclePosition
from ...core.resource_manager import ResourceManager
from ...utils.memory_utils import BoundedCache

local_tz = pytz.timezone("Asia/Kolkata")


class BMTCAPISource:
    """Streaming BMTC API client with bounded memory usage"""
    
    def __init__(self, resource_manager: ResourceManager):
        self.resource_manager = resource_manager
        self.base_url = "https://bmtcmobileapi.karnataka.gov.in/WebAPI"
        self.headers = {
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'en-US,en;q=0.5',
            'Content-Type': 'application/json',
            'User-Agent': 'BrowsePy/0.1',
            'lan': 'en',
            'deviceType': 'WEB',
            'Connection': 'close',
            'Accept-Encoding': 'identity'
        }
        self.min_gap_seconds = 10.0  # 10 seconds minimum between same payload requests
        self.last_request_time = 0.5 # 500 ms between requests to prevent throttle cases
        self._session: Optional[aiohttp.ClientSession] = None
        # Track last request time per unique payload to prevent duplicate API calls
        self._payload_timestamps: Dict[str, float] = {}
        # Cache API responses per payload for 20 seconds
        self._payload_cache: Dict[str, tuple[float, List[Vehicle]]] = {}
        
    async def __aenter__(self):
        """Async context manager entry"""
        self._session = await self.resource_manager.get_http_session()
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit"""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
    
    async def fetch_route_vehicles(self, route_id: str) -> AsyncIterator[Vehicle]:
        """Stream vehicles for a specific route with caching and smart rate limiting"""
        if not self._session:
            raise RuntimeError("BMTCAPISource must be used as async context manager")
        
        # Create unique payload key for caching and rate limiting
        payload_key = f"route_{route_id}"
        now = time.monotonic()
        
        # Check if we have cached data from the last 20 seconds
        if payload_key in self._payload_cache:
            cache_timestamp, cached_vehicles = self._payload_cache[payload_key]
            if now - cache_timestamp < self.min_gap_seconds:
                # Return cached data without making API call
                # print(f"[BMTCAPISource] Returning cached data for route {route_id}")
                for vehicle in cached_vehicles:
                    yield vehicle
                return
        
        # No cached data or cache expired, make API call
        # But first check if we need to rate limit (for new payloads, no rate limiting)
        last_payload_time = self._payload_timestamps.get(payload_key, 0.0)
        if last_payload_time > 0:  # We've made this request before
            wait_time = max(0.0, last_payload_time + self.min_gap_seconds - now)
            if wait_time > 0:
                # Return cached data if available while we wait
                if payload_key in self._payload_cache:
                    cache_timestamp, cached_vehicles = self._payload_cache[payload_key]
                    # print(f"[BMTCAPISource] Rate limited, returning cached data for route {route_id}")
                    for vehicle in cached_vehicles:
                        yield vehicle
                    return
                # If no cached data, we have to wait
                jitter = random.uniform(0, 0.2)
                await asyncio.sleep(wait_time + jitter)
        
        # Make the API call and cache the results
        vehicles_from_api = []
        
        url = f"{self.base_url}/SearchByRouteDetails_v4"
        payload = {
            "routeid": int(route_id),
            "servicetypeid": 0
        }

        # print(f"[BMTCAPISource] Making API call for route {route_id}")
        max_attempts = 3
        base_backoff = 0.5
        
        for attempt in range(1, max_attempts + 1):
            try:
                timeout = aiohttp.ClientTimeout(total=30)
                print('sending request ', route_id)
                async with self._session.post(url, json=payload, headers=self.headers, timeout=timeout) as response:
                    if response.status != 200:
                        print('RESPONSE IS NOT 200', route_id)
                        if 500 <= response.status < 600 and attempt < max_attempts:
                            # Retry on server errors
                            wait_time = base_backoff * (2 ** (attempt - 1)) + random.uniform(0, 0.2)
                            await asyncio.sleep(wait_time)
                            continue
                        else:
                            # Non-retryable error or final attempt
                            return
                    
                    # Stream JSON processing to minimize memory usage
                    response_text = await response.text()
                    
                    try:
                        # Direct JSON parsing (memory optimized)
                        api_data = json.loads(response_text)
                        response_text = None  # Release immediately
                        
                        if not api_data.get("issuccess", False):
                            return
                        
                        # Process both "up" and "down" directions
                        for direction in ["up", "down"]:
                            direction_data = api_data.get(direction, {}).get("data", [])
                            
                            # Stream stations one by one
                            for station_data in direction_data:
                                station_info = StationInfo.from_api_data(station_data)
                                vehicle_details = station_data.get("vehicleDetails", [])
                                
                                # Stream vehicles one by one
                                for vehicle_data in vehicle_details:
                                    try:
                                        vehicle = Vehicle.from_api_data(
                                            vehicle_data,
                                            station_data.get('routeid'),
                                            station_info.station_id
                                        )
                                        vehicles_from_api.append(vehicle)
                                        yield vehicle
                                    except Exception as e:
                                        # Log but continue processing other vehicles
                                        print(f"[BMTCAPISource] Error processing vehicle: {e}")
                                        continue
                                
                                # Periodic cleanup to prevent memory accumulation
                                if len(vehicle_details) > 10:
                                    gc.collect()
                        
                        # Cache the results for future requests
                        self._payload_cache[payload_key] = (now, vehicles_from_api)
                        self._payload_timestamps[payload_key] = now
                        
                        # Clean up old cache entries to prevent memory leaks
                        cutoff_time = now - (self.min_gap_seconds * 2)
                        keys_to_remove = [k for k, (timestamp, _) in self._payload_cache.items() if timestamp < cutoff_time]
                        for key in keys_to_remove:
                            self._payload_cache.pop(key, None)
                        
                        # Clean up old timestamps
                        self._payload_timestamps = {k: v for k, v in self._payload_timestamps.items() if v > cutoff_time}
                        
                        # Final cleanup
                        api_data = None
                        gc.collect()
                        return
                        
                    except json.JSONDecodeError as e:
                        print(f"[BMTCAPISource] JSON decode error: {e}")
                        return
                        
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                if attempt == max_attempts:
                    print(f"[BMTCAPISource] Final attempt failed for route {route_id}: {e}")
                    return
                else:
                    # Exponential backoff for retries
                    wait_time = base_backoff * (2 ** (attempt - 1)) + random.uniform(0, 0.2)
                    await asyncio.sleep(wait_time)
    
    async def fetch_all_routes(self, route_ids: List[str]) -> AsyncIterator[tuple[str, List[Vehicle]]]:
        """Stream all route data with batching for memory efficiency"""
        batch_size = 5  # Process routes in small batches
        
        for i in range(0, len(route_ids), batch_size):
            batch_route_ids = route_ids[i:i + batch_size]
            
            # Process batch concurrently but with limited concurrency
            semaphore = asyncio.Semaphore(2)  # Max 2 concurrent requests per batch
            
            async def fetch_route_batch(route_id: str) -> tuple[str, List[Vehicle]]:
                async with semaphore:
                    vehicles = []
                    async for vehicle in self.fetch_route_vehicles(route_id):
                        vehicles.append(vehicle)
                    return route_id, vehicles
            
            # Create tasks for batch
            tasks = [fetch_route_batch(route_id) for route_id in batch_route_ids]
            
            # Wait for batch completion
            try:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                
                for result in results:
                    if isinstance(result, Exception):
                        print(f"[BMTCAPISource] Batch error: {result}")
                        continue
                    
                    route_id, vehicles = result
                    if vehicles:  # Only yield if we have vehicles
                        yield route_id, vehicles
                
            except Exception as e:
                print(f"[BMTCAPISource] Batch processing error: {e}")
                continue
            
            # Cleanup between batches
            gc.collect()
            
            # Small delay between batches to prevent overwhelming the API
            await asyncio.sleep(0.5)
    
    async def get_route_info(self, route_id: str) -> Optional[RouteInfo]:
        """Get route information (cached or from first API call)"""
        # This would typically be cached, but for now we'll extract from first API call
        async for vehicle in self.fetch_route_vehicles(route_id):
            # Extract route info from first vehicle's station data
            # This is a simplified implementation
            return RouteInfo(
                route_id=route_id,
                route_number=f"Route-{route_id}",  # Would need actual mapping
                direction="UP",  # Would need to determine from data
                from_station="",  # Would extract from API
                to_station=""   # Would extract from API
            )
        return None
    
    def is_healthy(self) -> bool:
        """Check if the API source is healthy"""
        return self._session is not None and not self._session.closed
    
    async def test_connectivity(self) -> bool:
        """Test basic API connectivity"""
        if not self._session:
            return False
            
        try:
            test_url = f"{self.base_url}/SearchByRouteDetails_v4"
            test_payload = {"routeid": 1101, "servicetypeid": 0}
            
            timeout = aiohttp.ClientTimeout(total=10)
            async with self._session.post(test_url, json=test_payload, headers=self.headers, timeout=timeout) as response:
                return response.status == 200
                
        except Exception:
            return False


class BMTCAPICache:
    """Simple in-memory cache for API responses with TTL and memory limits"""
    
    def __init__(self, max_size_mb: int = 20, default_ttl_seconds: int = 30):
        self.max_size_mb = max_size_mb
        self.default_ttl_seconds = default_ttl_seconds
        self._cache: Dict[str, tuple[datetime, List[Vehicle]]] = {}
        self._access_times: Dict[str, datetime] = {}
    
    def _cleanup_if_needed(self):
        """Remove expired entries and enforce memory limits"""
        now = datetime.now()
        
        # Remove expired entries
        expired_keys = []
        for key, (timestamp, data) in self._cache.items():
            if (now - timestamp).total_seconds() > self.default_ttl_seconds:
                expired_keys.append(key)
        
        for key in expired_keys:
            self._cache.pop(key, None)
            self._access_times.pop(key, None)
        
        # Simple memory pressure check (approximate)
        if len(self._cache) > 100:  # Arbitrary limit
            # Remove least recently accessed items
            sorted_keys = sorted(self._access_times.items(), key=lambda x: x[1])
            for key, _ in sorted_keys[:20]:  # Remove oldest 20 entries
                self._cache.pop(key, None)
                self._access_times.pop(key, None)
    
    def get(self, route_id: str) -> Optional[List[Vehicle]]:
        """Get cached vehicles for route"""
        self._cleanup_if_needed()
        
        if route_id in self._cache:
            timestamp, vehicles = self._cache[route_id]
            now = datetime.now()
            
            if (now - timestamp).total_seconds() <= self.default_ttl_seconds:
                self._access_times[route_id] = now
                return vehicles
            else:
                # Expired
                self._cache.pop(route_id, None)
                self._access_times.pop(route_id, None)
        
        return None
    
    def set(self, route_id: str, vehicles: List[Vehicle]):
        """Cache vehicles for route"""
        self._cleanup_if_needed()
        
        now = datetime.now()
        self._cache[route_id] = (now, vehicles)
        self._access_times[route_id] = now
    
    def clear(self):
        """Clear all cached data"""
        self._cache.clear()
        self._access_times.clear()
    
    def get_stats(self) -> Dict[str, int]:
        """Get cache statistics"""
        return {
            "entries": len(self._cache),
            "total_vehicles": sum(len(vehicles) for _, vehicles in self._cache.values())
        }
