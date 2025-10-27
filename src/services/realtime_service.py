"""
Real-time GTFS-RT feed generation service for the new architecture.
Generates GTFS-RT protobuf feeds from live vehicle data with memory management.
"""

import time
import asyncio
import logging
import traceback
from typing import Dict, List, Optional, AsyncIterator
from datetime import datetime, timedelta

from google.transit import gtfs_realtime_pb2

from src.core.config import ApplicationConfig
from src.data.models.vehicle import Vehicle
from src.data.models.trip import Trip, TripStop
from src.data.repositories.live_data_repo import LiveDataRepository
from src.utils.memory_utils import BoundedCache

logger = logging.getLogger(__name__)


class RealtimeService:
    """Generate GTFS-RT feeds with bounded memory usage"""
    
    def __init__(self, config: ApplicationConfig, live_data_repo: LiveDataRepository):
        self.config = config
        self.live_data_repo = live_data_repo
        self.feed_cache = BoundedCache(ttl_seconds=30, max_size_mb=10)  # 30 second cache
        self._last_feed_timestamp = 0
        self.feed_entity_cache = BoundedCache(ttl_seconds=600, max_size_mb=10)
        
    async def generate_feed(self) -> gtfs_realtime_pb2.FeedMessage:
        """Generate current GTFS-RT feed"""
        cache_key = "current_feed"

        # Check cache first - keep responsive for frequent updates
        if cache_key in self.feed_cache.__dict__:
            cached_feed, timestamp = self.feed_cache.get(cache_key)
            if time.time() - timestamp < 10:  # 10 second cache (increased from 3s to reduce CPU load)
                return cached_feed

        # Create new feed
        feed = gtfs_realtime_pb2.FeedMessage()

        # Feed header
        feed.header.gtfs_realtime_version = "2.0"
        feed.header.incrementality = gtfs_realtime_pb2.FeedHeader.FULL_DATASET
        feed.header.timestamp = int(time.time())

        # Get active trips
        active_trips = await self.live_data_repo.get_active_trips()
        # print("BUILDING GTFS FEED!!!", len(active_trips))
        entity_count = 0
        # print(len([trip.id if len(trip.vehicles) > 0 else None for trip in active_trips]))
        seen_trips = set()
        for trip in active_trips:
            if entity_count >= 1000:  # Limit entities to prevent memory issues
                break
            if trip.id in seen_trips:
                continue

            try:
                # CRITICAL PERFORMANCE FIX: Do NOT run stop detection during feed generation!
                # Stop detection is extremely expensive (DB queries + geospatial calculations).
                # It should run in background, not on every HTTP request.
                # The trip already has stop times updated by background processing.

                # Removed: trip = await self.live_data_repo.detect_and_update_trip_stop_times(trip)

                # Create trip update entity directly from trip data
                trip_entity = await self._create_trip_update_entity(trip)
                if trip_entity:
                    feed.entity.append(trip_entity)
                    self.feed_entity_cache.set(trip.id, trip_entity)
                    entity_count += 1
                    seen_trips.add(trip.id)

            except Exception as e:
                logger.error(f"Error creating entities for trip {trip.id}: {e}")
                continue
        for t_id in self.feed_entity_cache.__dict__['_storage'].keys():
            if t_id in seen_trips:
                continue
            feed.entity.append(self.feed_entity_cache.get(t_id))
            entity_count += 1
            seen_trips.add(t_id)

        # Cache the feed
        self.feed_cache.set(cache_key, (feed, time.time()))
        self._last_feed_timestamp = feed.header.timestamp
        
        logger.debug(f"Generated GTFS-RT feed with {len(feed.entity)} entities")
        return feed
    
    async def stream_updates(self) -> AsyncIterator[gtfs_realtime_pb2.FeedMessage]:
        """Stream feed updates"""
        last_feed_timestamp = self._last_feed_timestamp
        while True:
            try:
                feed = await self.generate_feed()
                if feed.header.timestamp > last_feed_timestamp:
                    yield feed.SerializeToString()
                    last_feed_timestamp = feed.header.timestamp
                
                # Wait before next update
                await asyncio.sleep(5)  # 5 second intervals
                
            except Exception as e:
                logger.error(f"Error in feed streaming: {e}")
                await asyncio.sleep(30)  # Wait longer on error

    async def _create_trip_update_entity(self, trip: Trip) -> Optional[gtfs_realtime_pb2.FeedEntity]:
        """Create trip update entity from trip data"""
        if not trip.vehicles:
            # print(f"NO VEHICLES FOUND FOR TRIP UPDATE {trip.id}")
            return None
            
        try:
            entity = gtfs_realtime_pb2.FeedEntity()
            entity.id = f"rt_{trip.id}"

            vehicle_id = next(iter(trip.vehicles))
            vehicle: Optional[Vehicle] = await self.live_data_repo.get_vehicle(vehicle_id, trip.route_id)
            if not vehicle:
                logger.error(f"Unable to find vehicle {vehicle_id} for route {trip.route_id} and trip {trip.id} in live_data_repo!")
                return None
            # Vehicle position
            vehicle_pos = entity.vehicle
            vehicle_pos.trip.CopyFrom(gtfs_realtime_pb2.TripDescriptor())
            vehicle_pos.trip.trip_id = trip.id
            vehicle_pos.trip.route_id = trip.route_id

            vehicle_pos.vehicle.id = vehicle_id
            vehicle_pos.vehicle.label = vehicle.number or ""
            # vehicle_pos.vehicle.license = vehicle.number or ""
            vehicle_pos.trip.trip_id = trip.id
            vehicle_pos.trip.route_id = trip.route_id

            # Position data
            vehicle_pos.position.latitude = float(vehicle.position.latitude)
            vehicle_pos.position.longitude = float(vehicle.position.longitude)
            vehicle_pos.position.bearing = float(vehicle.position.bearing)
            vehicle_pos.timestamp = int(vehicle.position.timestamp.timestamp())
            if vehicle_pos.timestamp < int(time.time() - 1200):
                logger.debug("Skipping entity too old", trip.id)
                return None
            
            # Trip update
            trip_update = entity.trip_update
            trip_update.trip.trip_id = trip.id
            trip_update.trip.route_id = trip.route_id

            trip_update.vehicle.id = vehicle_id
            trip_update.vehicle.label = vehicle.number or ""
            # vehicle_pos.vehicle.license = vehicle.number or ""
            
            # Add stop time updates
            for stop in trip.stops:
                # if stop.is_skipped:
                #     continue
                    
                stu = trip_update.stop_time_update.add()
                stu.stop_id = stop.stop_id
                
                # Use effective times (actual > predicted > scheduled)
                arrival_time = stop.get_effective_arrival()
                departure_time = stop.get_effective_departure()
                
                # If no effective time, generate prediction based on current time and trip progress
                if not arrival_time and not departure_time:
                    predicted_time = await self._generate_fallback_prediction(trip, stop, vehicle)
                    if predicted_time:
                        arrival_time = predicted_time
                        departure_time = predicted_time
                
                if arrival_time:
                    stu.arrival.time = int(arrival_time.timestamp())
                    if stop.scheduled_arrival:
                        delay_seconds = int((arrival_time - stop.scheduled_arrival).total_seconds())
                        stu.arrival.delay = delay_seconds
                
                if departure_time:
                    stu.departure.time = int(departure_time.timestamp())
                    if stop.scheduled_departure:
                        delay_seconds = int((departure_time - stop.scheduled_departure).total_seconds())
                        stu.departure.delay = delay_seconds
            
            return entity
            
        except Exception as e:
            traceback.print_exc()
            logger.error(f"Error generating GTFS-RT feed: {e}")
            return None
    
    async def get_current_feed(self) -> Optional[bytes]:
        """Get the current GTFS-RT feed data (serialized protobuf bytes)"""
        feed = await self.generate_feed()
        try:
            return feed.SerializeToString()
        except Exception:
            # Fallback: return empty bytes on serialization error
            return b""
    
    async def _create_vehicle_position_entity(self, trip: Trip) -> Optional[gtfs_realtime_pb2.FeedEntity]:
        """Create vehicle position entity from trip data"""
        if not trip.vehicles:
            return None

        try:
            # Get the vehicle data from live data repo (Vehicle model)
            vehicle_id = next(iter(trip.vehicles))
            vehicle: Optional[Vehicle] = await self.live_data_repo.get_vehicle(vehicle_id, trip.route_id)

            if not vehicle:
                return None

            entity = gtfs_realtime_pb2.FeedEntity()
            entity.id = f"vehicle_{vehicle_id}"

            # Vehicle position
            vehicle_pos = entity.vehicle
            vehicle_pos.trip.CopyFrom(gtfs_realtime_pb2.TripDescriptor())
            vehicle_pos.trip.trip_id = trip.id
            vehicle_pos.trip.route_id = trip.route_id

            vehicle_pos.vehicle.id = vehicle_id
            vehicle_pos.vehicle.label = vehicle.number or ""

            # Position data
            vehicle_pos.position.latitude = float(vehicle.position.latitude)
            vehicle_pos.position.longitude = float(vehicle.position.longitude)
            vehicle_pos.position.bearing = float(vehicle.position.bearing)
            vehicle_pos.timestamp = int(vehicle.position.timestamp.timestamp())

            return entity

        except Exception as e:
            logger.error(f"Error creating vehicle position entity for trip {trip.id}: {e}")
            return None

    async def _generate_fallback_prediction(self, trip: Trip, stop: TripStop, vehicle: Vehicle) -> Optional[datetime]:
        """Generate fallback prediction when no effective time is available"""
        try:
            current_time = datetime.now()
            if current_time.tzinfo is None:
                import pytz
                local_tz = pytz.timezone("Asia/Kolkata")
                current_time = local_tz.localize(current_time)

            # If stop has scheduled time, use it with some delay buffer
            if stop.scheduled_arrival:
                scheduled_time = stop.scheduled_arrival
                if current_time > scheduled_time:
                    # If we're past scheduled time, add 5 minute delay
                    return current_time + timedelta(minutes=5)
                else:
                    # If scheduled time is in future, use it
                    return scheduled_time

            # Fallback: estimate based on trip start time and average speed
            if trip.start_time:
                # Estimate 3 minutes per stop
                estimated_minutes = stop.sequence * 3
                return trip.start_time + timedelta(minutes=estimated_minutes)

            # Last resort: current time + buffer
            return current_time + timedelta(minutes=10)

        except Exception as e:
            logger.error(f"Error generating fallback prediction for stop {stop.stop_id}: {e}")
            return None

    def get_feed_info(self) -> Dict[str, any]:
        """Get information about the current feed"""
        return {
            "last_update": self._last_feed_timestamp,
            "cache_size": len(self.feed_cache._data),
            "service_status": "active"
        }
