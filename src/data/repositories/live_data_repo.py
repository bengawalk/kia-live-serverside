"""
Live data repository for the new architecture.
Manages live vehicle and trip data with bounded memory usage and TTL-based cleanup.
"""

import asyncio
import gc
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set, Any, Tuple
from collections import defaultdict
import weakref
import time

from ..models.vehicle import Vehicle
from ..models.trip import Trip, TripSchedule
from ...utils.memory_utils import BoundedDict, BoundedCache
from ...core.resource_manager import ResourceManager
from ...services.stop_time_detector import StopTimeDetector, VehiclePositionRecord, StopLocation
from ...services.shape_validator import ShapeValidator

logger = logging.getLogger(__name__)


class LiveDataRepository:
    """Repository for live data with bounded memory usage and automatic cleanup"""
    
    def __init__(self, resource_manager: ResourceManager, database_service, max_memory_mb: int = 75):
        self.resource_manager = resource_manager
        self.database_service = database_service
        self.max_memory_mb = max_memory_mb
        
        # Bounded storage with TTL and memory limits
        vehicle_memory_mb = int(max_memory_mb * 0.45)  # 45% for vehicles
        trip_memory_mb = int(max_memory_mb * 0.45)     # 45% for trips
        cache_memory_mb = int(max_memory_mb * 0.1)    # 10% for general cache
        
        self._vehicles = BoundedDict(
            max_memory_mb=vehicle_memory_mb,
            ttl_seconds=10800,  # 3 hours (matches trip TTL to prevent mid-trip eviction)
            name="vehicles"
        )
        
        self._trips = BoundedDict(
            max_memory_mb=trip_memory_mb,
            ttl_seconds=3600*3,  # 3 hours
            name="trips"
        )
        
        self._vehicle_trip_mapping = BoundedDict(
            max_memory_mb=cache_memory_mb,
            ttl_seconds=1800,  # 30 minutes
            name="vehicle_trip_mapping"
        )
        
        # Trip schedules (static data, longer TTL)
        self._trip_schedules: Dict[str, TripSchedule] = {}
        
        # Historical data storage
        self._historical_vehicle_locations = BoundedDict(
            max_memory_mb=int(max_memory_mb * 0.3),  # 30% for historical vehicle data
            ttl_seconds=86400,  # 24 hours
            name="historical_vehicle_locations"
        )
        
        self._historical_stop_times = BoundedDict(
            max_memory_mb=int(max_memory_mb * 0.2),  # 20% for historical stop times
            ttl_seconds=86400,  # 24 hours
            name="historical_stop_times"
        )
        
        # Indexes for efficient querying
        self._vehicles_by_route: Dict[str, Set[str]] = defaultdict(set)
        self._trips_by_route: Dict[str, Set[str]] = defaultdict(set)
        self._active_trips: Set[str] = set()
        
        # Cleanup task
        self._cleanup_task: Optional[asyncio.Task] = None
        self._cleanup_interval = 300  # 5 minutes
        
        # Batch processing for database operations
        self._pending_vehicle_positions: List[Dict[str, Any]] = []
        self._batch_size = 100
        self._batch_timeout = 30  # seconds
        self._last_batch_time = 0

        # Shape validation for erratic trip detection
        try:
            self._shape_validator = ShapeValidator(routelines_path="in/routelines.json")
            logger.info("ShapeValidator initialized successfully")
        except Exception as e:
            logger.warning(f"Failed to initialize ShapeValidator: {e}. Trip validation will be disabled.")
            self._shape_validator = None

        # Stop time detection with shape validation
        self._stop_time_detector = StopTimeDetector(shape_validator=self._shape_validator)

        # In-memory historical vehicle positions storage
        # Key: f"{vehicle_id}:{route_id}" -> List[VehiclePositionRecord]
        self._historical_positions: Dict[str, List[VehiclePositionRecord]] = defaultdict(list)
        self._max_historical_positions_per_vehicle = 500  # Limit per vehicle
        self._historical_positions_ttl = 21600  # 6 hours in seconds
        
    async def start(self):
        """Start the repository and background cleanup tasks"""
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
    
    async def stop(self):
        """Stop background tasks and cleanup"""
        # Flush any remaining vehicle positions before stopping
        try:
            await self._flush_vehicle_positions_batch()
        except Exception as e:
            logger.error(f"Error flushing final vehicle positions: {e}")
            
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
    
    async def store_vehicle(self, vehicle: Vehicle):
        """Store vehicle data with automatic indexing"""
        vehicle_key = f"{vehicle.route_id}:{vehicle.id}"

        # Store vehicle
        self._vehicles[vehicle_key] = vehicle

        # Update indexes
        self._vehicles_by_route[vehicle.route_id].add(vehicle.id)

        # Determine trip_id for this vehicle position
        trip_id = None
        if vehicle.is_trip_active() and vehicle.scheduled_trip_start_time:
            # Look for an existing trip that matches this vehicle
            trip = await self.get_trip_by_route_and_start_time(
                vehicle.route_id,
                vehicle.scheduled_trip_start_time
            )
            if trip:
                trip_id = trip.id
            else:
                # Fallback to a generated trip ID format
                trip_id = f"{vehicle.route_id}_{vehicle.scheduled_trip_start_time.replace(':', '')}"

        # Store vehicle-trip mapping if trip is active
        if vehicle.is_trip_active():
            trip_key = self._get_trip_key(vehicle.route_id, vehicle.scheduled_trip_start_time)
            self._vehicle_trip_mapping[vehicle.id] = trip_key

        # Store historical position for stop time detection with trip_id
        await self.store_vehicle_position_history(vehicle, trip_id)

        # Save unique vehicle position to database (batched for efficiency)
        if self.database_service:
            try:
                position_data = {
                    "vehicle_id": vehicle.id,
                    "trip_id": trip_id,  # Use actual GTFS trip ID or None
                    "route_id": vehicle.route_id,
                    "lat": vehicle.position.latitude,
                    "lon": vehicle.position.longitude,
                    "bearing": vehicle.position.bearing,
                    "timestamp": int(vehicle.position.timestamp.timestamp()),
                    "speed": None,  # Not available in current data model
                    "status": "ACTIVE" if vehicle.is_trip_active() else "INACTIVE"
                }

                # Add to batch for efficient processing
                self._pending_vehicle_positions.append(position_data)

                # Process batch if it's full or enough time has passed
                current_time = time.time()
                if (len(self._pending_vehicle_positions) >= self._batch_size or
                    current_time - self._last_batch_time >= self._batch_timeout):
                    await self._flush_vehicle_positions_batch()

            except Exception as e:
                logger.error(f"Failed to batch vehicle position for database: {e}")
    
    async def get_vehicle(self, vehicle_id: str, route_id: str) -> Optional[Vehicle]:
        """Get vehicle by ID and route with SQLite fallback"""
        vehicle_key = f"{route_id}:{vehicle_id}"

        # Try in-memory first (fast path)
        vehicle = self._vehicles.get(vehicle_key)
        if vehicle:
            return vehicle

        # Vehicle not in memory - try to recover from SQLite
        if not self.database_service:
            return None

        try:
            logger.info(f"Vehicle {vehicle_id} not in memory, attempting SQLite recovery...")

            # Get recent positions from SQLite
            positions = await self.database_service.get_recent_vehicle_positions(
                vehicle_id=vehicle_id,
                route_id=route_id,
                limit=500,
                max_age_hours=6
            )

            if not positions:
                logger.warning(f"No positions found in SQLite for vehicle {vehicle_id} on route {route_id}")
                return None

            # Get the most recent position to reconstruct the vehicle
            most_recent = positions[0]  # Already sorted by timestamp descending

            import pytz
            from ..models.vehicle import Vehicle, VehiclePosition
            local_tz = pytz.timezone("Asia/Kolkata")

            # Reconstruct VehiclePosition
            position_timestamp = datetime.fromtimestamp(most_recent["timestamp"], local_tz)
            vehicle_position = VehiclePosition(
                latitude=most_recent["lat"],
                longitude=most_recent["lon"],
                bearing=most_recent["bearing"],
                timestamp=position_timestamp
            )

            # Reconstruct Vehicle (with minimal data from database)
            recovered_vehicle = Vehicle(
                id=vehicle_id,
                number="",  # Not stored in DB
                route_id=route_id,
                service_type="",  # Not stored in DB
                position=vehicle_position,
                scheduled_arrival_time=None,
                scheduled_departure_time=None,
                actual_arrival_time=None,
                actual_departure_time=None,
                scheduled_trip_start_time=None,
                current_station_id=None,
                stop_covered_status=0,
                trip_position=1,
                eta=None
            )

            # Re-store in memory with fresh TTL
            self._vehicles[vehicle_key] = recovered_vehicle
            self._vehicles_by_route[route_id].add(vehicle_id)

            # Restore historical positions from SQLite
            from ...services.stop_time_detector import VehiclePositionRecord
            recovered_positions = []
            for pos_data in positions:
                pos_timestamp = datetime.fromtimestamp(pos_data["timestamp"], local_tz)
                position_record = VehiclePositionRecord(
                    latitude=pos_data["lat"],
                    longitude=pos_data["lon"],
                    bearing=pos_data["bearing"],
                    timestamp=pos_timestamp,
                    vehicle_id=vehicle_id,
                    route_id=route_id,
                    trip_id=pos_data["trip_id"]
                )
                recovered_positions.append(position_record)

            # Store historical positions (sorted by timestamp ascending for proper order)
            self._historical_positions[vehicle_key] = sorted(recovered_positions, key=lambda p: p.timestamp)

            logger.info(f"Successfully recovered vehicle {vehicle_id} with {len(positions)} positions from SQLite")
            return recovered_vehicle

        except Exception as e:
            logger.error(f"Error recovering vehicle {vehicle_id} from SQLite: {e}", exc_info=True)
            return None
    
    async def get_vehicles_for_route(self, route_id: str) -> List[Vehicle]:
        """Get all vehicles for a specific route"""
        vehicle_ids = self._vehicles_by_route.get(route_id, set())
        vehicles = []
        
        for vehicle_id in list(vehicle_ids):  # Copy to avoid iteration issues
            vehicle_key = f"{route_id}:{vehicle_id}"
            vehicle = self._vehicles.get(vehicle_key)
            if vehicle:
                vehicles.append(vehicle)
            else:
                # Clean up stale index entry
                self._vehicles_by_route[route_id].discard(vehicle_id)
        
        return vehicles
    
    async def get_active_vehicles(self) -> List[Vehicle]:
        """Get all vehicles that are currently on active trips"""
        active_vehicles = []
        
        for vehicle_key, vehicle in self._vehicles.items():
            if vehicle.is_trip_active():
                active_vehicles.append(vehicle)
        
        return active_vehicles
    
    async def store_trip(self, trip: Trip):
        """Store trip data with automatic indexing"""
        trip_key = self._get_trip_key(trip.route_id, trip.start_time.strftime("%H:%M"))
        
        # Store trip
        self._trips[trip_key] = trip
        
        # Update indexes
        self._trips_by_route[trip.route_id].add(trip.id)
        
        if trip.is_active and not trip.is_completed:
            self._active_trips.add(trip.id)
        else:
            self._active_trips.discard(trip.id)
    
    async def get_trip(self, trip_id: str) -> Optional[Trip]:
        """Get trip by ID"""
        for trip_key, trip in self._trips.items():
            if trip.id == trip_id:
                return trip
        return None
    
    async def get_trip_by_route_and_start_time(self, route_id: str, start_time: str) -> Optional[Trip]:
        """Get trip by route and start time"""
        # print(f"VEHICLE TRIP RUNNING {start_time}")
        trip_key = self._get_trip_key(route_id, start_time)
        # print("SELF TRIP KEYS ", self._trips.__dict__["_data"].keys())
        return self._trips.get(trip_key)
    
    async def get_trips_for_route(self, route_id: str) -> List[Trip]:
        """Get all trips for a specific route"""
        trip_ids = self._trips_by_route.get(route_id, set())
        trips = []
        
        for trip_id in list(trip_ids):  # Copy to avoid iteration issues
            trip = await self.get_trip(trip_id)
            if trip:
                trips.append(trip)
            else:
                # Clean up stale index entry
                self._trips_by_route[route_id].discard(trip_id)
        
        return trips
    
    async def get_active_trips(self) -> List[Trip]:
        """Get all currently active trips"""
        active_trips = []
        # print("SENDING ACTIVE TRIPS!!")
        for trip_id in list(self._active_trips):  # Copy to avoid iteration issues
            trip = await self.get_trip(trip_id)
            if trip and trip.is_active and not trip.is_completed:
                active_trips.append(trip)
            else:
                # Clean up stale index entry
                self._active_trips.discard(trip_id)
        # print("GOT LENGTH OF ACTIVE TRIPS", len(active_trips))
        return active_trips
    
    async def match_vehicle_to_trip(self, vehicle: Vehicle) -> Optional[Trip]:
        """Match a vehicle to an appropriate trip based on schedule"""
        if not vehicle.scheduled_trip_start_time:
            return None
        
        # Try direct trip lookup first
        trip = await self.get_trip_by_route_and_start_time(
            vehicle.route_id, 
            vehicle.scheduled_trip_start_time
        )
        
        if trip:
            # print(f"FOUND TRIP FOR VEHICLE {trip.id}")
            utrip = trip.add_vehicle(vehicle.id)
            await self.store_trip(utrip)
            return utrip
        
        # Try to find trip from static schedule
        route_key = self._get_route_key_from_route_id(vehicle.route_id)
        if route_key and route_key in self._trip_schedules:
            schedule = self._trip_schedules[route_key]
            
            # Parse vehicle start time
            try:
                from datetime import datetime
                import pytz
                local_tz = pytz.timezone("Asia/Kolkata")
                
                hh, mm = map(int, vehicle.scheduled_trip_start_time.split(":"))
                now = datetime.now(local_tz)
                start_datetime = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                
                # Handle midnight crossings
                if start_datetime < now - timedelta(hours=6):
                    start_datetime += timedelta(days=1)
                elif start_datetime > now + timedelta(hours=18):
                    start_datetime -= timedelta(days=1)
                
                # Find matching trip in schedule
                matched_trip = schedule.get_trip_by_start_time(start_datetime, tolerance_minutes=2)
                if matched_trip:
                    # print(f"MATCHED TRIP FOR VEHICLE {matched_trip.id}")
                    # Add vehicle to trip and store
                    updated_trip = matched_trip.add_vehicle(vehicle.id)
                    await self.store_trip(updated_trip)
                    return updated_trip
                    
            except (ValueError, TypeError):
                pass
        # print(f"NOT MATCHED FOR VEHICLE {vehicle.id} {vehicle.route_id}")
        return None
    
    async def update_trip_with_vehicle_data(self, trip: Trip, vehicle: Vehicle) -> Trip:
        """Update trip data with vehicle information"""
        if not vehicle.current_station_id:
            print(f"RETURNING TRIP AS NO CURRENT_STATION_ID {trip.id} {vehicle.id}")
            return trip
        
        # Update trip stop status
        updated_trip = trip.update_stop_status(vehicle.current_station_id, vehicle)
        
        # Check if this update completed any stops and log to database
        if self.database_service and vehicle.actual_arrival_time and vehicle.actual_departure_time:
            try:
                # Find the corresponding stop that was just completed
                for stop in updated_trip.stops:
                    if stop.stop_id == vehicle.current_station_id and stop.is_completed():
                        stop_data = {
                            "stop_id": stop.stop_id,
                            "trip_id": updated_trip.id,
                            "route_id": updated_trip.route_id,
                            "date": updated_trip.start_time.strftime("%Y-%m-%d"),
                            "actual_arrival": vehicle.actual_arrival_time,
                            "actual_departure": vehicle.actual_departure_time,
                            "scheduled_arrival": stop.scheduled_arrival.strftime("%H:%M") if stop.scheduled_arrival else None,
                            "scheduled_departure": stop.scheduled_departure.strftime("%H:%M") if stop.scheduled_departure else None
                        }
                        await self.database_service.log_completed_trip_stop(stop_data)
                        break
            except Exception as e:
                logger.error(f"Failed to log completed trip stop to database: {e}")
        
        # Store updated trip
        await self.store_trip(updated_trip)
        
        return updated_trip
    
    def set_trip_schedules(self, trip_schedules: Dict[str, TripSchedule]):
        """Set static trip schedules for trip matching"""
        self._trip_schedules = trip_schedules

    async def drop_trip(self, trip_id: str, vehicle_id: str, reason: str = "erratic_positions"):
        """
        Drop a trip and all its in-memory references.
        Used for Case 2 (erratic trips) where vehicle is too far from route.

        Args:
            trip_id: Trip to drop
            vehicle_id: Vehicle to stop tracking
            reason: Reason for dropping (for logging)
        """
        logger.warning(f"Dropping trip {trip_id} (vehicle: {vehicle_id}). Reason: {reason}")

        # Find and remove trip from storage
        trip_key_to_remove = None
        for trip_key, trip in list(self._trips.items()):
            if trip.id == trip_id:
                trip_key_to_remove = trip_key
                break

        if trip_key_to_remove:
            del self._trips[trip_key_to_remove]
            logger.debug(f"Removed trip {trip_id} from _trips storage")

        # Remove from active trips
        if trip_id in self._active_trips:
            self._active_trips.discard(trip_id)
            logger.debug(f"Removed trip {trip_id} from _active_trips")

        # Remove from trips_by_route index
        for route_id, trip_ids in list(self._trips_by_route.items()):
            if trip_id in trip_ids:
                trip_ids.discard(trip_id)
                if not trip_ids:
                    del self._trips_by_route[route_id]
                logger.debug(f"Removed trip {trip_id} from _trips_by_route[{route_id}]")

        # Remove vehicle-trip mapping
        if vehicle_id in self._vehicle_trip_mapping:
            del self._vehicle_trip_mapping[vehicle_id]
            logger.debug(f"Removed vehicle {vehicle_id} from _vehicle_trip_mapping")

        # Clear position history for this vehicle to prevent contamination
        vehicle_key = None
        for key in list(self._historical_positions.keys()):
            if key.startswith(f"{vehicle_id}:"):
                vehicle_key = key
                break

        if vehicle_key and vehicle_key in self._historical_positions:
            position_count = len(self._historical_positions[vehicle_key])
            del self._historical_positions[vehicle_key]
            logger.debug(f"Cleared {position_count} historical positions for vehicle {vehicle_id}")

        logger.info(f"Successfully dropped trip {trip_id} and stopped tracking vehicle {vehicle_id}")

    def _get_trip_key(self, route_id: str, start_time: str) -> str:
        """Generate consistent trip key"""
        return f"{route_id}:{start_time}"
    
    def _get_route_key_from_route_id(self, route_id: str) -> Optional[str]:
        """Get route key from route ID (reverse lookup)"""
        # This would typically use a mapping from route_id to route_key
        # For now, we'll search through trip schedules
        for route_key, schedule in self._trip_schedules.items():
            if schedule.route_id == route_id:
                return route_key
        return None

    def _extract_direction_from_route_key(self, route_key: str) -> int:
        """
        Extract direction ID from route_key.

        Convention:
        - Routes ending with "UP" are direction 0
        - Routes ending with "DOWN" are direction 1
        - Default to 0 if neither found

        Args:
            route_key: Route key like "KIA-4 UP" or "KIA-9 DOWN"

        Returns:
            Direction ID (0 or 1)
        """
        route_key_upper = route_key.upper()
        if "DOWN" in route_key_upper:
            return 1
        else:
            # Default to UP (direction 0) if not DOWN or if no direction specified
            return 0

    async def store_vehicle_position_history(self, vehicle: Vehicle, trip_id: Optional[str] = None):
        """
        Store vehicle position in historical positions for stop time detection.
        Called automatically when storing a vehicle.

        Args:
            vehicle: Vehicle object containing position data
            trip_id: Optional trip ID for this position (should be provided for active trips)
        """
        import pytz
        local_tz = pytz.timezone("Asia/Kolkata")

        vehicle_key = f"{vehicle.id}:{vehicle.route_id}"

        # Create a VehiclePositionRecord with the actual trip_id
        position_record = VehiclePositionRecord(
            latitude=vehicle.position.latitude,
            longitude=vehicle.position.longitude,
            bearing=vehicle.position.bearing,
            timestamp=vehicle.position.timestamp,
            vehicle_id=vehicle.id,
            route_id=vehicle.route_id,
            trip_id=trip_id  # Use the actual trip ID passed in
        )

        # Add to historical positions
        self._historical_positions[vehicle_key].append(position_record)

        # Limit the number of positions per vehicle
        if len(self._historical_positions[vehicle_key]) > self._max_historical_positions_per_vehicle:
            # Remove oldest positions
            self._historical_positions[vehicle_key] = self._historical_positions[vehicle_key][-self._max_historical_positions_per_vehicle:]

        # Clean up old positions (older than TTL)
        cutoff_time = datetime.now(local_tz) - timedelta(seconds=self._historical_positions_ttl)
        self._historical_positions[vehicle_key] = [
            pos for pos in self._historical_positions[vehicle_key]
            if pos.timestamp >= cutoff_time
        ]

    async def get_vehicle_position_history(
        self,
        vehicle_id: str,
        route_id: str,
        max_age_hours: int = 8,
        trip_id: Optional[str] = None,
        trip_start_time: Optional[datetime] = None
    ) -> List[VehiclePositionRecord]:
        """
        Get historical vehicle positions for a specific vehicle.

        Filtering rules:
        1. Vehicle position must be from the same vehicle_id
        2. Vehicle position must be from the same route_id
        3. Vehicle position must be from the same trip_id (if provided)
        4. Vehicle position must be from within the last max_age_hours (default 8 hours)

        Fallback mechanism:
        - If trip_id is provided but no positions match, try fallback format {route_id}_{start_time}
        - This handles cases where positions were stored with fallback ID before actual trip was created

        Args:
            vehicle_id: Vehicle identifier
            route_id: Route identifier
            max_age_hours: Maximum age of positions in hours (default 8)
            trip_id: Optional trip ID to filter positions to only this trip
            trip_start_time: Optional trip start time as additional filter

        Returns:
            List of VehiclePositionRecord sorted by timestamp
        """
        import pytz
        local_tz = pytz.timezone("Asia/Kolkata")

        vehicle_key = f"{vehicle_id}:{route_id}"
        positions = self._historical_positions.get(vehicle_key, [])

        # Filter by age (rule 4)
        cutoff_time = datetime.now(local_tz) - timedelta(hours=max_age_hours)
        filtered_positions = [pos for pos in positions if pos.timestamp >= cutoff_time]

        # Query SQLite for positions by vehicle_id (preferred over trip_id)
        # This ensures we get all positions even if they weren't in memory
        # Note: Using vehicle_id is more reliable as trip_ids aren't always updated promptly
        if self.database_service:
            try:
                logger.debug(f"Querying SQLite for vehicle {vehicle_id} positions...")
                db_positions = await self.database_service.get_positions_by_vehicle(
                    vehicle_id=vehicle_id,
                    route_id=route_id,
                    limit=1000,
                    max_age_hours=8  # Changed from 12 to match in-memory max_age
                )

                # Convert database positions to VehiclePositionRecord
                from ...services.stop_time_detector import VehiclePositionRecord
                db_position_records = []
                for pos_data in db_positions:
                    pos_timestamp = datetime.fromtimestamp(pos_data["timestamp"], local_tz)
                    position_record = VehiclePositionRecord(
                        latitude=pos_data["lat"],
                        longitude=pos_data["lon"],
                        bearing=pos_data["bearing"],
                        timestamp=pos_timestamp,
                        vehicle_id=pos_data["vehicle_id"],
                        route_id=pos_data["route_id"],
                        trip_id=pos_data["trip_id"]
                    )
                    db_position_records.append(position_record)

                # Merge with in-memory positions (deduplicate by timestamp)
                existing_timestamps = {pos.timestamp for pos in filtered_positions}
                for db_pos in db_position_records:
                    if db_pos.timestamp not in existing_timestamps and db_pos.timestamp >= cutoff_time:
                        filtered_positions.append(db_pos)
                        existing_timestamps.add(db_pos.timestamp)

                # logger.info(
                #     f"Trip {trip_id}: Merged {len(db_position_records)} positions from SQLite "
                #     f"with {len(positions)} in-memory positions. Total: {len(filtered_positions)}"
                # )
            except Exception as e:
                logger.error(f"Error querying SQLite for trip {trip_id} positions: {e}", exc_info=True)

        # Filter by trip_id if provided (rule 3)
        # IMPORTANT: Only include positions that have a valid trip_id and it matches
        # Exclude positions with trip_id=None to prevent cross-trip contamination
        if trip_id:
            # Try exact match first
            exact_match_positions = [
                pos for pos in filtered_positions
                if pos.trip_id is not None and pos.trip_id == trip_id
            ]

            # If no exact matches and we have trip_start_time, try fallback format
            if not exact_match_positions and trip_start_time:
                # Generate fallback trip ID format: {route_id}_{start_time}
                # e.g., "1863_1910" for route 1863 starting at 19:10
                start_time_str = trip_start_time.strftime("%H%M")  # Format as HHMM
                fallback_trip_id = f"{route_id}_{start_time_str}"

                logger.info(
                    f"No positions found for trip_id={trip_id}, trying fallback format: {fallback_trip_id}"
                )

                filtered_positions = [
                    pos for pos in filtered_positions
                    if pos.trip_id is not None and pos.trip_id == fallback_trip_id
                ]

                if filtered_positions:
                    logger.info(
                        f"Fallback successful! Found {len(filtered_positions)} positions for {fallback_trip_id}"
                    )
                else:
                    logger.warning(
                        f"Fallback failed. No positions found for trip_id={trip_id} or fallback={fallback_trip_id}"
                    )
            else:
                filtered_positions = exact_match_positions

        # Filter by trip start time if provided (additional temporal filter)
        # Only include positions from 30 minutes before trip start, but not older than 12 hours ago
        # This prevents cross-contamination from same trip_id on different days
        if trip_start_time:
            # Don't look further back than 12 hours ago from now
            max_lookback_time = datetime.now(local_tz) - timedelta(hours=12)

            # Use the more recent of the two cutoffs
            effective_cutoff = max_lookback_time

            filtered_positions = [
                pos for pos in filtered_positions
                if pos.timestamp >= effective_cutoff
            ]

            if filtered_positions:
                logger.debug(
                    f"Temporal filter: {len(filtered_positions)} positions from "
                    f"{effective_cutoff} onwards (trip start: {trip_start_time}, max lookback: 12h)"
                )

        return sorted(filtered_positions, key=lambda p: p.timestamp)

    async def detect_and_update_trip_stop_times(self, trip: Trip) -> Trip:
        """
        Simple middleware to inject detected stop times from vehicle positions.

        This ONLY updates actual_arrival and actual_departure times based on
        detected vehicle positions. All prediction logic remains in PredictionService.

        Args:
            trip: The trip to detect stop times for

        Returns:
            Updated Trip with detected actual_arrival/actual_departure times
        """
        if not trip.vehicles:
            logger.debug(f"Trip {trip.id}: No vehicles assigned, skipping detection")
            return trip

        # Get the vehicle associated with this trip
        vehicle_id = next(iter(trip.vehicles))

        # Get historical positions for this vehicle, filtered by trip_id and trip start time
        historical_positions = await self.get_vehicle_position_history(
            vehicle_id, trip.route_id, max_age_hours=8, trip_id=trip.id, trip_start_time=trip.start_time
        )

        # logger.info(f"Trip {trip.id}: Found {len(historical_positions)} historical positions for vehicle {vehicle_id}")

        if not historical_positions:
            logger.debug(f"Trip {trip.id}: No historical positions available")
            return trip  # No positions, return original trip

        # Validate trip positions if shape validator is available
        if self._shape_validator and self._stop_time_detector.shape_validator:
            # Extract direction from route_key (e.g., "KIA-4 UP" -> direction 0, "KIA-4 DOWN" -> direction 1)
            direction_id = self._extract_direction_from_route_key(trip.route_key)

            # Validate positions
            should_continue, new_route_id, new_direction_id, reason = \
                self._stop_time_detector.validate_trip_positions(
                    vehicle_id=vehicle_id,
                    trip_id=trip.id,
                    route_id=trip.route_key,  # Use route_key as it matches routelines.json format
                    direction_id=direction_id,
                    positions=historical_positions
                )

            # Handle Case 2: Drop erratic trip
            if not should_continue:
                logger.warning(f"Trip {trip.id}: {reason}. Dropping trip.")
                await self.drop_trip(trip.id, vehicle_id, reason)
                return trip  # Return original trip (it will be removed from active trips)

            # Handle Case 1: Opposite direction detected
            if new_direction_id is not None and new_direction_id != direction_id:
                logger.info(f"Trip {trip.id}: {reason}. Updating to opposite direction.")
                # Note: We log this but continue with detection using current route_key
                # The route_key in Trip already contains the direction, so we don't need to update it
                # This is mainly for logging and monitoring purposes

        # Convert trip stops to StopLocation format
        from ...services.stop_time_detector import StopLocation
        stop_locations = []
        stops_without_station_info = 0
        for i, stop in enumerate(trip.stops):
            if stop.station_info:
                stop_location = StopLocation(
                    stop_id=stop.stop_id,
                    latitude=stop.station_info.latitude,
                    longitude=stop.station_info.longitude,
                    sequence=i,
                    is_first=(i == 0),
                    is_last=(i == len(trip.stops) - 1)
                )
                stop_locations.append(stop_location)
            else:
                stops_without_station_info += 1

        if stops_without_station_info > 0:
            logger.warning(f"Trip {trip.id}: {stops_without_station_info}/{len(trip.stops)} stops missing station_info")

        if not stop_locations:
            logger.warning(f"Trip {trip.id}: No stop locations available for detection")
            return trip  # No stop locations, return original trip

        # Detect stop times using the detector
        detected_times = self._stop_time_detector.detect_stop_times(
            trip.id,
            trip.route_id,
            historical_positions,
            stop_locations
        )

        # logger.info(f"Trip {trip.id}: Detected times for {len(detected_times)} stops: {list(detected_times.keys())}")

        if not detected_times:
            logger.debug(f"Trip {trip.id}: No stop times detected")
            return trip  # No detections, return original trip

        # Simple middleware: ONLY update actual_arrival and actual_departure
        # DO NOT touch predicted times or any other logic
        from ..models.trip import TripStop
        updated_stops = []
        injected_count = 0

        for stop in trip.stops:
            # Check if we detected this stop
            if stop.stop_id in detected_times:
                arrival_time, departure_time = detected_times[stop.stop_id]

                # Only use detected times if they're not None
                if arrival_time is not None:
                    updated_stop = TripStop(
                        stop_id=stop.stop_id,
                        sequence=stop.sequence,
                        station_info=stop.station_info,
                        scheduled_arrival=stop.scheduled_arrival,
                        scheduled_departure=stop.scheduled_departure,
                        actual_arrival=arrival_time,  # Inject detected time
                        actual_departure=departure_time,  # Inject detected time
                        predicted_arrival=stop.predicted_arrival,  # Keep existing prediction
                        predicted_departure=stop.predicted_departure,  # Keep existing prediction
                        is_passed=True,  # Mark as passed since we detected it
                        is_skipped=stop.is_skipped,
                        confidence=stop.confidence
                    )
                    updated_stops.append(updated_stop)
                    injected_count += 1
                    # logger.debug(f"Trip {trip.id}: Injected detected time for stop {stop.stop_id} at {arrival_time}")
                else:
                    # Detection failed, keep original stop unchanged
                    updated_stops.append(stop)
            else:
                # Not in detected_times, keep original stop unchanged
                updated_stops.append(stop)

        # logger.info(f"Trip {trip.id}: Injected {injected_count}/{len(trip.stops)} stop times from vehicle positions")

        # Return updated trip with only actual times changed
        return Trip(
            id=trip.id,
            route_id=trip.route_id,
            route_key=trip.route_key,
            start_time=trip.start_time,
            duration_minutes=trip.duration_minutes,
            stops=updated_stops,
            vehicles=trip.vehicles,
            is_active=trip.is_active,
            is_completed=trip.is_completed,
            service_id=trip.service_id,
            shape_id=trip.shape_id
        )

    async def _flush_vehicle_positions_batch(self):
        """Flush pending vehicle positions to database"""
        if not self._pending_vehicle_positions or not self.database_service:
            return
            
        try:
            positions_to_flush = self._pending_vehicle_positions.copy()
            self._pending_vehicle_positions.clear()
            self._last_batch_time = time.time()
            
            # Use batch insert for efficiency
            await self.database_service.batch_insert_positions(positions_to_flush)
            logger.debug(f"Flushed {len(positions_to_flush)} vehicle positions to database")
            
        except Exception as e:
            logger.error(f"Failed to flush vehicle positions batch to database: {e}")
            # Re-add positions to retry later if they're not too old
            current_time = time.time()
            recent_positions = [
                pos for pos in positions_to_flush 
                if current_time - pos.get("timestamp", 0) < 300  # Keep positions less than 5 minutes old
            ]
            self._pending_vehicle_positions.extend(recent_positions)

    async def _cleanup_loop(self):
        """Background cleanup task"""
        while True:
            try:
                await asyncio.sleep(self._cleanup_interval)
                await self._cleanup_expired_data()
                await self._cleanup_indexes()
                
                # Flush any pending vehicle positions
                await self._flush_vehicle_positions_batch()
                
                # Force garbage collection periodically
                gc.collect()
                
            except asyncio.CancelledError:
                # Final flush before exiting
                try:
                    await self._flush_vehicle_positions_batch()
                except:
                    pass
                break
            except Exception as e:
                print(f"[LiveDataRepository] Cleanup error: {e}")
    
    async def _cleanup_expired_data(self):
        """Clean up expired data from bounded dictionaries"""
        # BoundedDict handles its own TTL cleanup, but we can trigger it
        self._vehicles.cleanup_expired()
        self._trips.cleanup_expired()
        self._vehicle_trip_mapping.cleanup_expired()
    
    async def _cleanup_indexes(self):
        """Clean up stale index entries"""
        # Clean vehicle indexes
        for route_id, vehicle_ids in list(self._vehicles_by_route.items()):
            valid_ids = set()
            for vehicle_id in list(vehicle_ids):
                vehicle_key = f"{route_id}:{vehicle_id}"
                if vehicle_key in self._vehicles:
                    valid_ids.add(vehicle_id)
            
            if valid_ids:
                self._vehicles_by_route[route_id] = valid_ids
            else:
                del self._vehicles_by_route[route_id]
        
        # Clean trip indexes
        for route_id, trip_ids in list(self._trips_by_route.items()):
            valid_ids = set()
            for trip_id in list(trip_ids):
                # Check if trip still exists
                trip_found = False
                for trip_key, trip in self._trips.items():
                    if trip.id == trip_id:
                        valid_ids.add(trip_id)
                        trip_found = True
                        break
                
                if not trip_found:
                    self._active_trips.discard(trip_id)
            
            if valid_ids:
                self._trips_by_route[route_id] = valid_ids
            else:
                del self._trips_by_route[route_id]
    
    def get_memory_stats(self) -> Dict[str, any]:
        """Get memory usage statistics"""
        return {
            "vehicles": self._vehicles.get_stats(),
            "trips": self._trips.get_stats(),
            "vehicle_trip_mapping": self._vehicle_trip_mapping.get_stats(),
            "indexes": {
                "vehicles_by_route": len(self._vehicles_by_route),
                "trips_by_route": len(self._trips_by_route),
                "active_trips": len(self._active_trips)
            },
            "trip_schedules_loaded": len(self._trip_schedules)
        }
    
    async def get_repository_stats(self) -> Dict[str, any]:
        """Get comprehensive repository statistics"""
        memory_stats = self.get_memory_stats()
        
        # Count active vs inactive items
        active_vehicles = len(await self.get_active_vehicles())
        active_trips = len(await self.get_active_trips())
        
        return {
            "memory": memory_stats,
            "counts": {
                "total_vehicles": len(self._vehicles),
                "active_vehicles": active_vehicles,
                "total_trips": len(self._trips),
                "active_trips": active_trips,
                "routes_with_vehicles": len(self._vehicles_by_route),
                "routes_with_trips": len(self._trips_by_route)
            },
            "health": {
                "cleanup_task_running": self._cleanup_task and not self._cleanup_task.done(),
                "memory_pressure": sum(
                    stats.get("memory_usage_mb", 0) 
                    for stats in memory_stats.values() 
                    if isinstance(stats, dict)
                ) / self.max_memory_mb
            }
        }


class CacheRepository:
    """Simple cache repository for frequently accessed data"""
    
    def __init__(self, max_size_mb: int = 10, default_ttl_seconds: int = 300):
        self._cache = BoundedCache(max_size_mb=max_size_mb, ttl_seconds=default_ttl_seconds)
    
    async def get(self, key: str) -> Optional[any]:
        """Get cached value"""
        return self._cache.get(key)
    
    async def set(self, key: str, value: any, ttl_seconds: Optional[int] = None):
        """Set cached value with optional TTL override"""
        self._cache.set(key, value, ttl_seconds)
    
    async def delete(self, key: str):
        """Delete cached value"""
        self._cache.delete(key)
    
    async def clear(self):
        """Clear all cached values"""
        self._cache.clear()
    
    def get_stats(self) -> Dict[str, any]:
        """Get cache statistics"""
        return self._cache.get_stats()
