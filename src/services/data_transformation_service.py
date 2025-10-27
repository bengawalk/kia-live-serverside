"""
Data transformation service that bridges the gap between live vehicle data and GTFS-RT entities.
This replaces the missing transformation pipeline from the old system.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set
from collections import defaultdict

from ..core.config import ApplicationConfig
from ..data.models.vehicle import Vehicle
from ..data.models.trip import Trip, TripStop
from ..data.repositories.live_data_repo import LiveDataRepository

logger = logging.getLogger(__name__)


class DataTransformationService:
    """
    Service that transforms live vehicle data into proper trip updates.
    Replicates the functionality of old_src/live_data_service/live_data_transformer.py
    """
    
    def __init__(self, config: ApplicationConfig, live_repo: LiveDataRepository):
        self.config = config
        self.live_repo = live_repo
        
    async def transform_vehicles_to_trip_updates(self, route_id: str) -> List[Trip]:
        """
        Transform vehicle data into trip updates, similar to transform_response_to_feed_entities.
        Groups vehicles by trip timing and consolidates stop time data.
        """
        try:
            # Get all vehicles for this route
            vehicles = await self.live_repo.get_vehicles_for_route(route_id)
            if not vehicles:
                return []
            
            # Group vehicles by trip (similar to old system logic)
            vehicle_groups = await self._group_vehicles_by_trip(vehicles)
            
            # Transform each vehicle group into a trip update
            updated_trips = []
            for trip_key, vehicle_group in vehicle_groups.items():
                trip = await self._create_trip_from_vehicle_group(route_id, trip_key, vehicle_group)
                if trip:
                    updated_trips.append(trip)
            
            return updated_trips
            
        except Exception as e:
            logger.error(f"Error transforming vehicles to trip updates for route {route_id}: {e}")
            return []
    
    async def _group_vehicles_by_trip(self, vehicles: List[Vehicle]) -> Dict[str, List[Vehicle]]:
        """
        Group vehicles by trip timing, similar to old system's vehicle grouping logic.
        Vehicles with the same scheduled trip start time belong to the same trip.
        """
        vehicle_groups = defaultdict(list)
        
        for vehicle in vehicles:
            if not vehicle.scheduled_trip_start_time:
                continue
                
            # Create trip key from route_id and trip start time
            trip_key = f"{vehicle.route_id}:{vehicle.scheduled_trip_start_time}"
            vehicle_groups[trip_key].append(vehicle)
        
        return dict(vehicle_groups)
    
    async def _create_trip_from_vehicle_group(self, route_id: str, trip_key: str, vehicles: List[Vehicle]) -> Optional[Trip]:
        """
        Create a Trip object from a group of vehicles, consolidating their stop time data.
        This replicates the old system's entity building logic.
        """
        try:
            if not vehicles:
                return None
            
            # Extract trip start time from key
            _, trip_start_time = trip_key.split(":", 1)
            
            # Try to find existing trip or create new one
            existing_trip = await self.live_repo.get_trip_by_route_and_start_time(route_id, trip_start_time)
            
            if not existing_trip:
                # No existing trip found - need to create from static schedule
                logger.warning(f"No trip template found for {route_id} at {trip_start_time}")
                return None
            
            # Start with existing trip as base
            trip = existing_trip
            
            # Add all vehicles to this trip
            vehicle_ids = {vehicle.id for vehicle in vehicles}
            if trip.vehicles != vehicle_ids:
                for vehicle_id in vehicle_ids:
                    trip = trip.add_vehicle(vehicle_id)
            
            # Consolidate stop time data from all vehicles into trip stops
            trip = await self._consolidate_stop_time_data(trip, vehicles)
            
            return trip
            
        except Exception as e:
            logger.error(f"Error creating trip from vehicle group {trip_key}: {e}")
            return None
    
    async def _consolidate_stop_time_data(self, trip: Trip, vehicles: List[Vehicle]) -> Trip:
        """
        Consolidate stop timing data from multiple vehicles into the trip's stop list.
        This is the key missing piece - stop time updates should be in Trip, not scattered in Vehicle objects.
        """
        try:
            # Create a map of stop timing data from all vehicles
            stop_updates = {}  # stop_id -> {actual_arrival, actual_departure, etc.}
            
            for vehicle in vehicles:
                stop_id = vehicle.current_station_id
                if not stop_id:
                    continue
                
                # Collect timing data for this stop
                update = {}
                if vehicle.actual_arrival_time:
                    update['actual_arrival'] = self._parse_time_to_datetime(vehicle.actual_arrival_time)
                if vehicle.actual_departure_time:
                    update['actual_departure'] = self._parse_time_to_datetime(vehicle.actual_departure_time)
                if vehicle.scheduled_arrival_time:
                    update['scheduled_arrival'] = self._parse_time_to_datetime(vehicle.scheduled_arrival_time)
                if vehicle.scheduled_departure_time:
                    update['scheduled_departure'] = self._parse_time_to_datetime(vehicle.scheduled_departure_time)
                
                stop_updates[stop_id] = update
            
            # Apply updates to trip stops
            updated_stops = []
            for stop in trip.stops:
                if stop.stop_id in stop_updates:
                    update = stop_updates[stop.stop_id]
                    
                    # Create updated stop with consolidated timing data
                    updated_stop = TripStop(
                        stop_id=stop.stop_id,
                        sequence=stop.sequence,
                        station_info=stop.station_info,
                        scheduled_arrival=update.get('scheduled_arrival', stop.scheduled_arrival),
                        scheduled_departure=update.get('scheduled_departure', stop.scheduled_departure),
                        actual_arrival=update.get('actual_arrival', stop.actual_arrival),
                        actual_departure=update.get('actual_departure', stop.actual_departure),
                        predicted_arrival=stop.predicted_arrival,
                        predicted_departure=stop.predicted_departure,
                        is_passed=bool(update.get('actual_departure')),
                        is_skipped=stop.is_skipped,
                        confidence=stop.confidence
                    )
                    updated_stops.append(updated_stop)
                else:
                    updated_stops.append(stop)
            
            # Create updated trip with consolidated stop data
            updated_trip = Trip(
                id=trip.id,
                route_id=trip.route_id,
                route_key=trip.route_key,
                start_time=trip.start_time,
                duration_minutes=trip.duration_minutes,
                stops=updated_stops,
                vehicles=trip.vehicles,
                is_active=True,  # Mark as active since it has live data
                is_completed=trip.is_completed,
                service_id=trip.service_id,
                shape_id=trip.shape_id
            )
            
            return updated_trip
            
        except Exception as e:
            logger.error(f"Error consolidating stop time data for trip {trip.id}: {e}")
            return trip
    
    def _parse_time_to_datetime(self, time_str: str) -> Optional[datetime]:
        """Parse HH:MM time string to datetime object"""
        if not time_str or ":" not in time_str:
            return None
            
        try:
            import pytz
            local_tz = pytz.timezone("Asia/Kolkata")
            
            hh, mm = map(int, time_str.split(":"))
            now = datetime.now(local_tz)
            parsed_time = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            
            # Handle midnight crossings
            if parsed_time < now - timedelta(hours=6):
                parsed_time += timedelta(days=1)
            elif parsed_time > now + timedelta(hours=18):
                parsed_time -= timedelta(days=1)
                
            return parsed_time
        except (ValueError, TypeError):
            return None
    
    def get_transformation_stats(self) -> Dict[str, any]:
        """Get transformation service statistics"""
        return {
            "service_active": True
        }
