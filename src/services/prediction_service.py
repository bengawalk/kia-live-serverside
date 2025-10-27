"""
Time prediction service for the new architecture.
Handles time predictions with caching and multiple prediction strategies.
"""

import asyncio
import logging
import traceback
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta
from pathlib import Path
import pytz

from src.core.config import ApplicationConfig
from src.data.models.trip import Trip, TripStop
from src.utils.memory_utils import BoundedCache

logger = logging.getLogger(__name__)
local_tz = pytz.timezone("Asia/Kolkata")


class PredictionService:
    """Handle time predictions with caching and multiple strategies"""
    
    def __init__(self, config: ApplicationConfig):
        self.config = config
        self.static_cache = BoundedCache(ttl_seconds=3600, max_size_mb=5)  # 1 hour cache for static predictions
        self.ml_cache = BoundedCache(ttl_seconds=900, max_size_mb=10)  # 15 minute cache for ML predictions
        self._prediction_stats = {"cache_hits": 0, "cache_misses": 0, "predictions_made": 0}
        
    async def predict_arrival_times(self, trip: Trip) -> Trip:
        """Predict arrival times for trip stops using multiple strategies"""
        try:
            self._prediction_stats["predictions_made"] += 1
            
            # Try static schedule first
            predicted_trip = await self._predict_from_static_schedule(trip)
            if predicted_trip:
                return predicted_trip
                
            # Fall back to ML predictions
            return await self._predict_from_ml_model(trip)
            
        except Exception as e:
            traceback.print_exc()
            logger.error(f"Error predicting arrival times for trip {trip.id}: {e}")
            return trip  # Return original trip if prediction fails

    async def _predict_from_static_schedule(self, trip: Trip) -> Optional[Trip]:
        """Predict using static schedule data with caching"""
        cache_key = f"static_{trip.route_id}_{trip.start_time.strftime('%H:%M')}"
        
        # Check cache first
        if cache_key in self.static_cache.__dict__['_storage']:
            self._prediction_stats["cache_hits"] += 1
            cached_predictions = self.static_cache[cache_key]
            return self._apply_cached_predictions(trip, cached_predictions)
        
        self._prediction_stats["cache_misses"] += 1
        
        # Load static times data (simplified - would load from actual static data)
        static_times = await self._load_static_times(trip.route_key)
        if not static_times:
            return None
            
        # Find matching trip in static data
        matching_trip = self._find_matching_static_trip(trip, static_times)
        if not matching_trip:
            return None
            
        # Apply static predictions
        predictions = {}
        for stop_data in matching_trip.get("stops", []):
            stop_id = str(stop_data.get("stop_id", ""))
            stop_time_minutes = stop_data.get("stop_time", 0)
            
            # Convert to datetime
            predicted_time = self._minutes_to_datetime(stop_time_minutes)
            predictions[stop_id] = predicted_time
            
        # Cache the predictions
        self.static_cache[cache_key] = predictions
        
        return self._apply_predictions_to_trip(trip, predictions)

    async def _predict_from_ml_model(self, trip: Trip) -> Trip:
        """Predict using ML model (simplified implementation)"""
        cache_key = f"ml_{trip.route_id}_{trip.id}"
        
        # Check cache first
        if cache_key in self.ml_cache.__dict__['_storage']:
            self._prediction_stats["cache_hits"] += 1
            cached_predictions = self.ml_cache.get(cache_key)
            return self._apply_cached_predictions(trip, cached_predictions)
        
        self._prediction_stats["cache_misses"] += 1
        
        # Simple ML prediction (in real implementation, would use actual ML model)
        predictions = {}
        current_time = trip.start_time
        
        for i, stop in enumerate(trip.stops):
            if stop.actual_departure:
                # Use actual time if available
                predictions[stop.stop_id] = stop.actual_departure
                current_time = stop.actual_departure
            else:
                # Predict based on average travel time
                if i > 0:
                    # Add average time between stops (simplified)
                    travel_time = timedelta(minutes=3)  # 3 minutes average
                    current_time += travel_time
                    
                predictions[stop.stop_id] = current_time
        
        # Cache the predictions
        self.ml_cache.set(cache_key, predictions)
        
        return self._apply_predictions_to_trip(trip, predictions)

    async def _load_static_times(self, route_key: str) -> Optional[List[Dict]]:
        """Load static times data for a route"""
        try:
            # In real implementation, would load from static times.json
            # For now, return None to indicate no static data available
            return None
        except Exception as e:
            logger.error(f"Error loading static times for route {route_key}: {e}")
            return None

    def _find_matching_static_trip(self, trip: Trip, static_times: List[Dict]) -> Optional[Dict]:
        """Find matching trip in static times data"""
        trip_start_minutes = trip.start_time.hour * 60 + trip.start_time.minute
        
        # Find closest start time
        best_match = None
        min_diff = float('inf')
        
        for static_trip in static_times:
            static_start = static_trip.get("start", 0)
            diff = abs(static_start - trip_start_minutes)
            
            if diff < min_diff:
                min_diff = diff
                best_match = static_trip
                
        # Only return if difference is reasonable (within 10 minutes)
        if best_match and min_diff <= 10:
            return best_match
            
        return None

    def _minutes_to_datetime(self, minutes: int) -> datetime:
        """Convert minutes from midnight to datetime"""
        hours = minutes // 60
        mins = minutes % 60
        
        # Start with today
        today = datetime.now().replace(hour=hours, minute=mins, second=0, microsecond=0)
        
        # Handle day rollover
        now = datetime.now()
        if today < now - timedelta(hours=6):
            today += timedelta(days=1)
        elif today > now + timedelta(hours=18):
            today -= timedelta(days=1)
            
        return today

    def _apply_predictions_to_trip(self, trip: Trip, predictions: Dict[str, datetime]) -> Trip:
        """Apply predictions to trip stops"""
        updated_stops = []
        
        for stop in trip.stops:
            if stop.stop_id in predictions:
                predicted_time = predictions[stop.stop_id]
                
                # Create updated stop with predictions
                updated_stop = TripStop(
                    stop_id=stop.stop_id,
                    sequence=stop.sequence,
                    station_info=stop.station_info,
                    scheduled_arrival=stop.scheduled_arrival,
                    scheduled_departure=stop.scheduled_departure,
                    actual_arrival=stop.actual_arrival,
                    actual_departure=stop.actual_departure,
                    predicted_arrival=predicted_time,
                    predicted_departure=predicted_time,
                    is_passed=stop.is_passed,
                    is_skipped=stop.is_skipped,
                    confidence=stop.confidence
                )
                updated_stops.append(updated_stop)
            else:
                updated_stops.append(stop)
        
        # Return updated trip
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

    def _apply_cached_predictions(self, trip: Trip, cached_predictions: Dict[str, datetime]) -> Trip:
        """Apply cached predictions to trip"""
        return self._apply_predictions_to_trip(trip, cached_predictions)

    async def get_prediction_confidence(self, trip: Trip) -> float:
        """Get confidence score for predictions (0.0 to 1.0)"""
        try:
            # Simple confidence calculation based on data availability
            total_stops = len(trip.stops)
            if total_stops == 0:
                return 0.0
                
            # Count stops with actual data
            actual_data_stops = sum(1 for stop in trip.stops if stop.actual_arrival or stop.actual_departure)
            
            # Higher confidence with more actual data
            confidence = min(1.0, 0.5 + (actual_data_stops / total_stops) * 0.5)
            
            return confidence
            
        except Exception as e:
            logger.error(f"Error calculating prediction confidence for trip {trip.id}: {e}")
            return 0.0

    def get_prediction_stats(self) -> Dict[str, Any]:
        """Get prediction service statistics"""
        cache_total = self._prediction_stats["cache_hits"] + self._prediction_stats["cache_misses"]
        hit_rate = self._prediction_stats["cache_hits"] / cache_total if cache_total > 0 else 0.0
        
        return {
            "cache_hit_rate": hit_rate,
            "predictions_made": self._prediction_stats["predictions_made"],
            "static_cache_size": len(self.static_cache._data),
            "ml_cache_size": len(self.ml_cache._data)
        }

    async def get_effective_arrival(
        self, 
        trip: Trip, 
        stop_id: str, 
        current_time: datetime
    ) -> Optional[datetime]:
        """Get effective arrival time for a specific stop that has not been passed."""
        try:
            # Find the target stop in the trip
            target_stop = None
            current_stop_index = -1
            target_stop_index = -1
            
            for i, stop in enumerate(trip.stops):
                if stop.is_passed:
                    current_stop_index = i
                if stop.stop_id == stop_id:
                    target_stop = stop
                    target_stop_index = i
                    break
            
            if not target_stop:
                return None
                
            # Check if stop has been passed
            if target_stop.is_passed:
                return None
            
            # If stop already has effective arrival time, return it
            effective_time = target_stop.get_effective_arrival()
            if effective_time:
                return effective_time
                
            # Predict based on current position and static schedule
            if target_stop_index > current_stop_index:
                # Calculate travel time from current position to target stop
                travel_time_minutes = 0
                
                for i in range(current_stop_index + 1, target_stop_index + 1):
                    if i < len(trip.stops):
                        # Estimate 3 minutes per stop (can be enhanced with static data)
                        travel_time_minutes += 3
                
                # Add current delay if available
                overall_delay = trip.get_overall_delay_seconds()
                delay_minutes = overall_delay / 60 if overall_delay else 0
                
                predicted_arrival = current_time + timedelta(minutes=travel_time_minutes + delay_minutes)
                return predicted_arrival
            
            return None
            
        except Exception as e:
            logger.error(f"Error calculating effective arrival for stop {stop_id} in trip {trip.id}: {e}")
            return None

    async def predict_arrival_times_lightweight(self, trip: Trip, current_time: datetime) -> Trip:
        """
        Lightweight prediction logic using observed delays and static schedule durations.
        
        Logic:
        1. Find all stops with actual data to identify gaps (skipped stops)
        2. For skipped stops, use scheduled times or interpolate between known points
        3. For remaining stops, use static schedule durations + observed delay
        4. Always maintain chronological order
        """
        try:
            self._prediction_stats["predictions_made"] += 1
            
            # Ensure current_time is timezone-aware
            if current_time.tzinfo is None:
                current_time = local_tz.localize(current_time)
            
            # Find all stops with actual departure times
            actual_stops = {}  # {index: stop} for stops with actual data
            for i, stop in enumerate(trip.stops):
                if stop.actual_departure:
                    actual_stops[i] = stop
            
            # If no real-time data yet, use scheduled times
            if not actual_stops:
                return self._predict_from_scheduled_times_only(trip, current_time)
            
            # Apply predictions
            predictions = {}
            
            # Process all stops
            for i, stop in enumerate(trip.stops):
                # Use actual time for stops that have it
                if stop.actual_departure:
                    predictions[stop.stop_id] = stop.actual_departure
                    continue
                
                # For stops without actual data, determine prediction strategy
                predicted_time = None
                
                # Check if this is a skipped stop between actual stops
                prev_actual_idx = None
                next_actual_idx = None
                
                # Find previous stop with actual data
                for j in range(i-1, -1, -1):
                    if j in actual_stops:
                        prev_actual_idx = j
                        break
                
                # Find next stop with actual data
                for j in range(i+1, len(trip.stops)):
                    if j in actual_stops:
                        next_actual_idx = j
                        break
                
                if prev_actual_idx is not None and next_actual_idx is not None:
                    # Skipped stop between two actual stops - interpolate carefully
                    prev_actual_stop = actual_stops[prev_actual_idx]
                    next_actual_stop = actual_stops[next_actual_idx]
                    
                    # Use scheduled time if available and it fits between actual stops
                    if (stop.scheduled_arrival and 
                        prev_actual_stop.actual_departure <= stop.scheduled_arrival <= next_actual_stop.actual_departure):
                        predicted_time = stop.scheduled_arrival
                    else:
                        # Interpolate based on position between actual stops
                        time_gap = next_actual_stop.actual_departure - prev_actual_stop.actual_departure
                        position_gap = next_actual_idx - prev_actual_idx
                        current_position = i - prev_actual_idx
                        
                        interpolated_offset = (time_gap * current_position) / position_gap
                        predicted_time = prev_actual_stop.actual_departure + interpolated_offset
                        
                        # Ensure minimum 1-minute gap from previous stop
                        prev_time = predictions.get(trip.stops[i-1].stop_id) if i > 0 else prev_actual_stop.actual_departure
                        if prev_time:
                            min_time = prev_time + timedelta(minutes=1)
                            if predicted_time < min_time:
                                predicted_time = min_time
                
                elif prev_actual_idx is not None:
                    # Stop after the last actual stop - use static duration
                    prev_actual_stop = actual_stops[prev_actual_idx]
                    
                    # Calculate cumulative time from last actual stop
                    cumulative_time = prev_actual_stop.actual_departure
                    for j in range(prev_actual_idx + 1, i + 1):
                        current_stop = trip.stops[j]
                        prev_stop = trip.stops[j - 1]
                        
                        if prev_stop.scheduled_departure and current_stop.scheduled_arrival:
                            # Use static duration between stops
                            static_duration = current_stop.scheduled_arrival - prev_stop.scheduled_departure
                            cumulative_time += static_duration
                        else:
                            # Fallback: add 3 minutes per stop
                            cumulative_time += timedelta(minutes=3)
                        
                        # Ensure minimum 1-minute gap
                        if j > prev_actual_idx + 1:
                            prev_predicted_stop = trip.stops[j - 1]
                            prev_predicted_time = predictions.get(prev_predicted_stop.stop_id)
                            if prev_predicted_time:
                                min_time = prev_predicted_time + timedelta(minutes=1)
                                if cumulative_time < min_time:
                                    cumulative_time = min_time
                        
                        if j == i:  # This is our target stop
                            predicted_time = cumulative_time
                            break
                        else:  # Store intermediate predictions
                            predictions[current_stop.stop_id] = cumulative_time
                
                else:
                    # Stop before any actual data - use scheduled time or estimate
                    if stop.scheduled_arrival:
                        predicted_time = stop.scheduled_arrival
                        
                        # Add buffer if we're past scheduled time, but only if no future actual data constrains us
                        if current_time > stop.scheduled_arrival and next_actual_idx is None:
                            buffer_minutes = 2
                            predicted_time += timedelta(minutes=buffer_minutes)
                    else:
                        # Last resort: estimate from trip start
                        if trip.start_time:
                            estimated_minutes = stop.sequence * 3
                            predicted_time = trip.start_time + timedelta(minutes=estimated_minutes)
                        else:
                            predicted_time = current_time + timedelta(minutes=stop.sequence * 2 + 5)
                
                # Store the prediction
                if predicted_time:
                    predictions[stop.stop_id] = predicted_time
            
            return self._apply_predictions_to_trip(trip, predictions)
            
        except Exception as e:
            logger.error(f"Error in lightweight prediction for trip {trip.id}: {e}")
            return trip  # Return original trip if prediction fails
    
    def _predict_from_scheduled_times_only(self, trip: Trip, current_time: datetime) -> Trip:
        """Predict using only scheduled times when no real-time data is available"""
        predictions = {}
        last_time = None
        
        # Ensure current_time is timezone-aware
        if current_time.tzinfo is None:
            current_time = local_tz.localize(current_time)
        
        for i, stop in enumerate(trip.stops):
            predicted_time = None
            
            if stop.scheduled_arrival:
                predicted_time = stop.scheduled_arrival
                
                # Add buffer if we're past scheduled time (both timezone-aware now)
                if current_time > stop.scheduled_arrival:
                    buffer_minutes = 2  # 2-minute buffer for delays
                    predicted_time += timedelta(minutes=buffer_minutes)
            else:
                # Fallback: estimate based on trip start time and stop sequence
                if trip.start_time:
                    # Estimate 3 minutes per stop
                    estimated_minutes = stop.sequence * 3
                    predicted_time = trip.start_time + timedelta(minutes=estimated_minutes)
                    
                    # Ensure it's not in the past
                    if predicted_time < current_time:
                        predicted_time = current_time + timedelta(minutes=5)
                else:
                    # Last resort: current time + buffer based on sequence
                    predicted_time = current_time + timedelta(minutes=stop.sequence * 2 + 5)
            
            # Ensure minimum 1-minute gap between consecutive stops
            if last_time:
                min_time = last_time + timedelta(minutes=1)
                if predicted_time and predicted_time < min_time:
                    predicted_time = min_time
            
            if predicted_time:
                predictions[stop.stop_id] = predicted_time
                last_time = predicted_time  # Update last_time for next iteration
        
        return self._apply_predictions_to_trip(trip, predictions)

    async def clear_cache(self):
        """Clear prediction caches"""
        self.static_cache.clear()
        self.ml_cache.clear()
        logger.info("Prediction caches cleared")
