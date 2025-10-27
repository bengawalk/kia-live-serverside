"""
Trip scheduler service that creates and manages trip schedules from static data.
This component was missing from the new architecture and is critical for GTFS-RT generation.
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from pathlib import Path
import pytz

from src.core.config import ApplicationConfig
from src.data.models.trip import Trip, TripStop, TripSchedule
from src.data.models.vehicle import StationInfo
from src.data.repositories.live_data_repo import LiveDataRepository

logger = logging.getLogger(__name__)
local_tz = pytz.timezone("Asia/Kolkata")


class TripSchedulerService:
    """
    Service that loads static schedule data and creates trip schedules for live data matching.
    This replaces the missing functionality from old_src/live_data_service/live_data_scheduler.py
    """
    
    def __init__(self, config: ApplicationConfig, live_repo: LiveDataRepository):
        self.config = config
        self.live_repo = live_repo
        self.trip_schedules: Dict[str, TripSchedule] = {}
        self.route_mappings: Dict[str, str] = {}  # route_key -> route_id
        self.is_running = False
        self.scheduler_task: Optional[asyncio.Task] = None
        
    async def start(self):
        """Start the trip scheduler service"""
        logger.info("Starting Trip Scheduler Service...")
        
        # Load static data and create trip schedules
        await self._load_static_schedules()
        
        # Set trip schedules in the live data repository
        if hasattr(self.live_repo, 'set_trip_schedules'):
            self.live_repo.set_trip_schedules(self.trip_schedules)
        
        # Start scheduling task
        self.is_running = True
        self.scheduler_task = asyncio.create_task(self._scheduling_loop())
        
        logger.info(f"Trip Scheduler started with {len(self.trip_schedules)} route schedules")
        
    async def stop(self):
        """Stop the trip scheduler service"""
        logger.info("Stopping Trip Scheduler Service...")
        
        self.is_running = False
        
        if self.scheduler_task and not self.scheduler_task.done():
            self.scheduler_task.cancel()
            try:
                await self.scheduler_task
            except asyncio.CancelledError:
                pass
                
        logger.info("Trip Scheduler stopped")
    
    async def _load_static_schedules(self):
        """Load and process static schedule data from input files"""
        try:
            # Load times.json (main schedule data)
            times_file = self.config.in_dir / "times.json"
            if not times_file.exists():
                logger.error("times.json not found - cannot create trip schedules")
                return
                
            with open(times_file, 'r', encoding='utf-8') as f:
                times_data = json.load(f)
            
            # Load route mappings
            children_file = self.config.in_dir / "routes_children_ids.json"
            if children_file.exists():
                with open(children_file, 'r', encoding='utf-8') as f:
                    self.route_mappings = {k: str(v) for k, v in json.load(f).items()}
            
            # Load station data for stop info
            stations_data = await self._load_station_data()
            
            # Create trip schedules for each route
            for route_key, route_schedule_data in times_data.items():
                if route_key not in self.route_mappings:
                    logger.warning(f"No route mapping found for {route_key}")
                    continue
                    
                route_id = self.route_mappings[route_key]
                
                # Create trip schedule
                trip_schedule = await self._create_trip_schedule(
                    route_key, route_id, route_schedule_data, stations_data
                )
                
                if trip_schedule:
                    self.trip_schedules[route_key] = trip_schedule
                    logger.debug(f"Created schedule for {route_key} with {len(trip_schedule.trips)} trips")
            
            logger.info(f"Loaded {len(self.trip_schedules)} trip schedules")
            
        except Exception as e:
            logger.error(f"Failed to load static schedules: {e}")
            import traceback
            traceback.print_exc()
    
    async def _load_station_data(self) -> Dict[str, StationInfo]:
        """Load station/stop information"""
        stations = {}
        
        try:
            stops_file = self.config.in_dir / "client_stops.json"
            if stops_file.exists():
                with open(stops_file, 'r', encoding='utf-8') as f:
                    stops_data = json.load(f)
                
                # Extract station info from client_stops.json
                for route_key, route_data in stops_data.items():
                    for stop in route_data.get("stops", []):
                        stop_id = str(stop.get("stop_id")) if stop.get("stop_id") else f"gen_{stop.get('name', 'unknown')}"
                        
                        stations[stop_id] = StationInfo(
                            station_id=stop_id,
                            name=stop.get("name", ""),
                            name_kn=stop.get("name_kn", ""),
                            latitude=float(stop.get("loc", [0, 0])[0]),
                            longitude=float(stop.get("loc", [0, 0])[1]),
                            distance_on_route=float(stop.get("distance", 0))
                        )
        except Exception as e:
            logger.warning(f"Could not load station data: {e}")
        
        return stations
    
    async def _create_trip_schedule(self, route_key: str, route_id: str, 
                                  schedule_data: List[dict], stations_data: Dict[str, StationInfo]) -> Optional[TripSchedule]:
        """Create a TripSchedule from static schedule data"""
        try:
            trips = []
            # print("SCH DATA ROUTE", route_key)
            # print("LENGTH SCH DATA ", len(schedule_data))
            seq = 1
            for trip_data in schedule_data:
                # Create trip from static data
                trip = await self._create_trip_from_static(route_key, route_id, trip_data, stations_data, seq)
                seq += 1
                if trip:
                    trips.append(trip)
            
            if trips:
                return TripSchedule(
                    route_key=route_key,
                    route_id=route_id,
                    trips=trips
                )
            
        except Exception as e:
            logger.error(f"Failed to create trip schedule for {route_key}: {e}")
            
        return None
    
    async def _create_trip_from_static(self, route_key: str, route_id: str, 
                                     trip_data: dict, stations_data: Dict[str, StationInfo], seq: int = 0) -> Optional[Trip]:
        """Create a Trip instance from static trip data"""
        try:
            start_time_minutes = trip_data.get("start", 0)
            duration = trip_data.get("duration", 60.0)
            
            # Convert start time to datetime
            if start_time_minutes < 100:
                # Assume it's just minutes
                hours = 0
                minutes = start_time_minutes
            else:
                # HHMM format
                hours = start_time_minutes // 100
                minutes = start_time_minutes % 100
            
            now = datetime.now(local_tz)
            start_time = now.replace(hour=hours, minute=minutes, second=0, microsecond=0)
            
            # Handle midnight crossings
            if start_time < now - timedelta(hours=6):
                start_time += timedelta(days=1)
            elif start_time > now + timedelta(hours=18):
                start_time -= timedelta(days=1)
            
            # Create trip stops with context-aware parsing for midnight crossings
            stops_data = trip_data.get("stops", [])
            trip_stops = []
            prev_scheduled_time = start_time

            for i, stop_data in enumerate(stops_data):
                stop_id = str(stop_data.get("stop_id", ""))
                station_info = stations_data.get(stop_id)

                # Use the from_static_data method from TripStop with prev_scheduled_time for context
                trip_stop = TripStop.from_static_data(stop_data, i + 1, station_info, prev_scheduled_time)

                # Update prev_scheduled_time for next iteration
                if trip_stop.scheduled_arrival:
                    prev_scheduled_time = trip_stop.scheduled_arrival

                trip_stops.append(trip_stop)
            
            # Generate sequential trip ID (matching old system format)
            trip_id = f"{route_id}_{seq}"
            
            # Create trip
            trip = Trip(
                id=trip_id,
                route_id=route_id,
                route_key=route_key,
                start_time=start_time,
                duration_minutes=duration,
                stops=trip_stops,
                is_active=True,  # Mark as active so it appears in GTFS-RT
                shape_id=f"sh_{route_id}"
            )
            
            return trip
            
        except Exception as e:
            logger.error(f"Failed to create trip from static data: {e}")
            return None
    
    async def _scheduling_loop(self):
        """Main scheduling loop that manages active trips"""
        logger.info("Starting trip scheduling loop")
        date = datetime.now()
        while self.is_running:
            try:
                if date < (datetime.now() - timedelta(days=1)):
                    await self._load_static_schedules()
                    date = datetime.now()
                await self._update_active_trips()
                await asyncio.sleep(60)  # Run every minute
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in scheduling loop: {e}")
                await asyncio.sleep(60)
        
        logger.info("Trip scheduling loop stopped")
    
    async def _update_active_trips(self):
        """Update active trips based on current time and schedule"""
        current_time = datetime.now(local_tz)
        
        for route_key, schedule in self.trip_schedules.items():
            for trip in schedule.trips:
                # Check if trip should be active (started in last 2 hours, not completed)
                trip_start = trip.start_time
                time_since_start = current_time - trip_start
                
                # Consider trip active if:
                # 1. Started within last 2 hours
                # 2. Not yet completed
                # 3. Start time is not too far in future (within 30 minutes)
                is_active = (
                    timedelta(hours=-2) <= time_since_start <= timedelta(hours=4)
                )
                
                if is_active:
                    # Store active trip in repository
                    await self.live_repo.store_trip(trip)
    
    def get_scheduler_stats(self) -> Dict[str, any]:
        """Get scheduler statistics"""
        total_trips = sum(len(schedule.trips) for schedule in self.trip_schedules.values())
        
        return {
            "is_running": self.is_running,
            "route_schedules_loaded": len(self.trip_schedules),
            "total_trips_defined": total_trips,
            "route_mappings": len(self.route_mappings)
        }
