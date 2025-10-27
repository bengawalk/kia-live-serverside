"""
Static file data source for the new architecture.
Loads and manages static schedule data from ./in/ directory files.
"""

import json
import asyncio
from pathlib import Path
from typing import Dict, List, Optional, AsyncIterator
from datetime import datetime
import gc

from ..models.vehicle import StationInfo
from ..models.trip import Trip, TripSchedule, TripStop
from ...core.resource_manager import ResourceManager


class StaticFileSource:
    """Static file loader with memory optimization and caching"""
    
    def __init__(self, resource_manager: ResourceManager, in_dir: Path = Path("in")):
        self.resource_manager = resource_manager
        self.in_dir = Path(in_dir)
        self._cache: Dict[str, any] = {}
        self._cache_timestamps: Dict[str, datetime] = {}
        self.cache_ttl_seconds = 3600  # 1 hour cache
        
    async def load_client_stops(self) -> Dict[str, Dict]:
        """Load client_stops.json with caching"""
        cache_key = "client_stops"
        
        # Check cache first
        if self._is_cache_valid(cache_key):
            return self._cache[cache_key]
        
        file_path = self.in_dir / "client_stops.json"
        
        try:
            # Use asyncio to avoid blocking on large file reads
            loop = asyncio.get_event_loop()
            
            def _load_json():
                with open(file_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            
            data = await loop.run_in_executor(None, _load_json)
            
            # Cache the data
            self._cache[cache_key] = data
            self._cache_timestamps[cache_key] = datetime.now()
            
            return data
            
        except Exception as e:
            print(f"[StaticFileSource] Error loading client_stops.json: {e}")
            return {}
    
    async def load_times(self) -> Dict[str, List[Dict]]:
        """Load times.json with caching"""
        cache_key = "times"
        
        # Check cache first
        if self._is_cache_valid(cache_key):
            return self._cache[cache_key]
        
        file_path = self.in_dir / "times.json"
        
        try:
            loop = asyncio.get_event_loop()
            
            def _load_json():
                with open(file_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            
            data = await loop.run_in_executor(None, _load_json)
            
            # Cache the data
            self._cache[cache_key] = data
            self._cache_timestamps[cache_key] = datetime.now()
            
            return data
            
        except Exception as e:
            print(f"[StaticFileSource] Error loading times.json: {e}")
            return {}
    
    async def load_routes_children_ids(self) -> Dict[str, int]:
        """Load routes_children_ids.json with caching"""
        cache_key = "routes_children_ids"
        
        if self._is_cache_valid(cache_key):
            return self._cache[cache_key]
        
        file_path = self.in_dir / "routes_children_ids.json"
        
        try:
            loop = asyncio.get_event_loop()
            
            def _load_json():
                with open(file_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            
            data = await loop.run_in_executor(None, _load_json)
            
            self._cache[cache_key] = data
            self._cache_timestamps[cache_key] = datetime.now()
            
            return data
            
        except Exception as e:
            print(f"[StaticFileSource] Error loading routes_children_ids.json: {e}")
            return {}
    
    async def load_routes_parent_ids(self) -> Dict[str, int]:
        """Load routes_parent_ids.json with caching"""
        cache_key = "routes_parent_ids"
        
        if self._is_cache_valid(cache_key):
            return self._cache[cache_key]
        
        file_path = self.in_dir / "routes_parent_ids.json"
        
        try:
            loop = asyncio.get_event_loop()
            
            def _load_json():
                with open(file_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            
            data = await loop.run_in_executor(None, _load_json)
            
            self._cache[cache_key] = data
            self._cache_timestamps[cache_key] = datetime.now()
            
            return data
            
        except Exception as e:
            print(f"[StaticFileSource] Error loading routes_parent_ids.json: {e}")
            return {}
    
    async def load_start_times(self) -> Dict[str, List[Dict]]:
        """Load start_times.json with caching"""
        cache_key = "start_times"
        
        if self._is_cache_valid(cache_key):
            return self._cache[cache_key]
        
        file_path = self.in_dir / "start_times.json"
        
        try:
            loop = asyncio.get_event_loop()
            
            def _load_json():
                with open(file_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            
            data = await loop.run_in_executor(None, _load_json)
            
            self._cache[cache_key] = data
            self._cache_timestamps[cache_key] = datetime.now()
            
            return data
            
        except Exception as e:
            print(f"[StaticFileSource] Error loading start_times.json: {e}")
            return {}
    
    async def get_stations_for_route(self, route_key: str) -> Dict[str, StationInfo]:
        """Get station information for a specific route"""
        client_stops = await self.load_client_stops()
        
        if route_key not in client_stops:
            return {}
        
        route_data = client_stops[route_key]
        stops_data = route_data.get("stops", [])
        
        stations = {}
        for i, stop_data in enumerate(stops_data):
            station_info = StationInfo.from_static_data(
                stop_data, 
                distance=stop_data.get("distance", 0.0)
            )
            stations[station_info.station_id] = station_info
        
        return stations
    
    async def stream_trip_schedules(self) -> AsyncIterator[TripSchedule]:
        """Stream trip schedules for all routes with memory optimization"""
        times_data = await self.load_times()
        client_stops_data = await self.load_client_stops()
        
        # Process routes in batches to control memory usage
        route_keys = list(times_data.keys())
        batch_size = 5
        
        for i in range(0, len(route_keys), batch_size):
            batch_keys = route_keys[i:i + batch_size]
            
            for route_key in batch_keys:
                try:
                    # Get stations for this route
                    stations = await self.get_stations_for_route(route_key)
                    
                    # Create trip schedule
                    route_times = times_data[route_key]
                    trip_schedule = TripSchedule.from_static_data(
                        route_key, 
                        route_times, 
                        stations
                    )
                    
                    yield trip_schedule
                    
                except Exception as e:
                    print(f"[StaticFileSource] Error processing route {route_key}: {e}")
                    continue
            
            # Periodic cleanup between batches
            gc.collect()
    
    async def get_trip_schedule(self, route_key: str) -> Optional[TripSchedule]:
        """Get trip schedule for a specific route"""
        times_data = await self.load_times()
        
        if route_key not in times_data:
            return None
        
        try:
            stations = await self.get_stations_for_route(route_key)
            route_times = times_data[route_key]
            
            return TripSchedule.from_static_data(route_key, route_times, stations)
            
        except Exception as e:
            print(f"[StaticFileSource] Error creating trip schedule for {route_key}: {e}")
            return None
    
    async def get_route_mapping(self) -> Dict[str, str]:
        """Get mapping from route keys to route IDs"""
        children_ids = await self.load_routes_children_ids()
        parent_ids = await self.load_routes_parent_ids()
        
        # Create mapping of route_key -> child_route_id
        route_mapping = {}
        for route_key, child_id in children_ids.items():
            route_mapping[route_key] = str(child_id)
        
        return route_mapping
    
    async def get_parent_route_mapping(self) -> Dict[str, str]:
        """Get mapping from route keys to parent route IDs"""
        parent_ids = await self.load_routes_parent_ids()
        
        route_mapping = {}
        for route_key, parent_id in parent_ids.items():
            route_mapping[route_key] = str(parent_id)
        
        return route_mapping
    
    async def get_all_route_keys(self) -> List[str]:
        """Get all available route keys"""
        times_data = await self.load_times()
        return list(times_data.keys())
    
    async def get_all_parent_route_ids(self) -> List[str]:
        """Get all unique parent route IDs for API polling"""
        parent_mapping = await self.get_parent_route_mapping()
        unique_parent_ids = list(set(parent_mapping.values()))
        return unique_parent_ids
    
    def _is_cache_valid(self, cache_key: str) -> bool:
        """Check if cached data is still valid"""
        if cache_key not in self._cache or cache_key not in self._cache_timestamps:
            return False
        
        age_seconds = (datetime.now() - self._cache_timestamps[cache_key]).total_seconds()
        return age_seconds < self.cache_ttl_seconds
    
    def clear_cache(self):
        """Clear all cached data"""
        self._cache.clear()
        self._cache_timestamps.clear()
        gc.collect()
    
    def get_cache_stats(self) -> Dict[str, any]:
        """Get cache statistics"""
        return {
            "cached_files": list(self._cache.keys()),
            "cache_count": len(self._cache),
            "oldest_cache": min(self._cache_timestamps.values()) if self._cache_timestamps else None,
            "newest_cache": max(self._cache_timestamps.values()) if self._cache_timestamps else None
        }
    
    async def validate_data_integrity(self) -> Dict[str, any]:
        """Validate the integrity of loaded static data"""
        validation_results = {
            "client_stops": {"status": "unknown", "routes": 0, "total_stops": 0},
            "times": {"status": "unknown", "routes": 0, "total_trips": 0},
            "routes_children_ids": {"status": "unknown", "routes": 0},
            "routes_parent_ids": {"status": "unknown", "routes": 0},
            "consistency": {"matched_routes": 0, "unmatched_routes": []}
        }
        
        try:
            # Load all data
            client_stops = await self.load_client_stops()
            times = await self.load_times()
            children_ids = await self.load_routes_children_ids()
            parent_ids = await self.load_routes_parent_ids()
            
            # Validate client_stops
            validation_results["client_stops"]["routes"] = len(client_stops)
            total_stops = sum(len(route_data.get("stops", [])) for route_data in client_stops.values())
            validation_results["client_stops"]["total_stops"] = total_stops
            validation_results["client_stops"]["status"] = "ok"
            
            # Validate times
            validation_results["times"]["routes"] = len(times)
            total_trips = sum(len(trips) for trips in times.values())
            validation_results["times"]["total_trips"] = total_trips
            validation_results["times"]["status"] = "ok"
            
            # Validate route mappings
            validation_results["routes_children_ids"]["routes"] = len(children_ids)
            validation_results["routes_children_ids"]["status"] = "ok"
            
            validation_results["routes_parent_ids"]["routes"] = len(parent_ids)
            validation_results["routes_parent_ids"]["status"] = "ok"
            
            # Check consistency between files
            times_routes = set(times.keys())
            children_routes = set(children_ids.keys())
            parent_routes = set(parent_ids.keys())
            client_routes = set(client_stops.keys())
            
            all_routes = times_routes | children_routes | parent_routes | client_routes
            matched_routes = times_routes & children_routes & parent_routes & client_routes
            unmatched_routes = all_routes - matched_routes
            
            validation_results["consistency"]["matched_routes"] = len(matched_routes)
            validation_results["consistency"]["unmatched_routes"] = list(unmatched_routes)
            
        except Exception as e:
            validation_results["error"] = str(e)
        
        return validation_results


class StaticDataManager:
    """Manager for coordinating static data loading and caching"""
    
    def __init__(self, resource_manager: ResourceManager, in_dir: Path = Path("in")):
        self.static_source = StaticFileSource(resource_manager, in_dir)
        self._loaded = False
        self._trip_schedules: Dict[str, TripSchedule] = {}
        
    async def initialize(self):
        """Initialize and preload static data"""
        if self._loaded:
            return
        
        print("[StaticDataManager] Loading static data...")
        
        # Validate data integrity first
        validation = await self.static_source.validate_data_integrity()
        print(f"[StaticDataManager] Data validation: {validation['consistency']['matched_routes']} matched routes")
        
        # Load trip schedules
        schedule_count = 0
        async for trip_schedule in self.static_source.stream_trip_schedules():
            self._trip_schedules[trip_schedule.route_key] = trip_schedule
            schedule_count += 1
            
            # Progress indicator
            if schedule_count % 10 == 0:
                print(f"[StaticDataManager] Loaded {schedule_count} trip schedules...")
        
        self._loaded = True
        print(f"[StaticDataManager] Completed loading {schedule_count} trip schedules")
    
    def get_trip_schedule(self, route_key: str) -> Optional[TripSchedule]:
        """Get cached trip schedule for route"""
        return self._trip_schedules.get(route_key)
    
    def get_all_route_keys(self) -> List[str]:
        """Get all loaded route keys"""
        return list(self._trip_schedules.keys())
    
    async def get_parent_route_ids(self) -> List[str]:
        """Get all parent route IDs for API polling"""
        return await self.static_source.get_all_parent_route_ids()
    
    async def refresh_data(self):
        """Refresh static data from files"""
        self.static_source.clear_cache()
        self._trip_schedules.clear()
        self._loaded = False
        await self.initialize()
    
    def get_stats(self) -> Dict[str, any]:
        """Get statistics about loaded data"""
        total_trips = sum(len(schedule.trips) for schedule in self._trip_schedules.values())
        total_stops = sum(
            len(trip.stops) 
            for schedule in self._trip_schedules.values() 
            for trip in schedule.trips
        )
        
        return {
            "loaded": self._loaded,
            "route_schedules": len(self._trip_schedules),
            "total_trips": total_trips,
            "total_stops": total_stops,
            "cache_stats": self.static_source.get_cache_stats()
        }
