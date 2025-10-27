"""
Stop Time Detection Service for GTFS-RT
Detects stop arrival/departure times from historical vehicle positions using distance-based matching.
"""

import logging
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Tuple
from dataclasses import dataclass
from math import radians, cos, sin, asin, sqrt
import pytz

from src.services.shape_validator import ShapeValidator

local_tz = pytz.timezone("Asia/Kolkata")
logger = logging.getLogger(__name__)


@dataclass
class VehiclePositionRecord:
    """Historical vehicle position record"""
    latitude: float
    longitude: float
    bearing: float
    timestamp: datetime
    vehicle_id: str
    route_id: str
    trip_id: Optional[str] = None


@dataclass
class StopLocation:
    """Stop location data"""
    stop_id: str
    latitude: float
    longitude: float
    sequence: int
    is_first: bool = False
    is_last: bool = False


class StopTimeDetector:
    """
    Detects stop times from historical vehicle positions using distance-based matching.

    Ruleset:
    1. First stop: Within 100m, use the LATEST point (vehicle leaving first stop)
    2. Last stop: Within 100m, use the EARLIEST point (vehicle arriving at last stop)
    3. In-between stops: Use the ABSOLUTE CLOSEST point within 1km
       - If latest point is closest, stop time not yet determined (haven't passed yet)
    4. If no match for first stop, use earliest tracked position as trip start
    """

    def __init__(self, shape_validator: Optional[ShapeValidator] = None):
        self.FIRST_STOP_DISTANCE_KM = 0.1  # 100 meters
        self.LAST_STOP_DISTANCE_KM = 0.1   # 100 meters
        self.MIDDLE_STOP_DISTANCE_KM = 1.0  # 1 kilometer
        self.shape_validator = shape_validator

    def validate_trip_positions(
        self,
        vehicle_id: str,
        trip_id: str,
        route_id: str,
        direction_id: int,
        positions: List[VehiclePositionRecord]
    ) -> Tuple[bool, Optional[str], Optional[int], str]:
        """
        Validate positions and detect cases for erratic trips or opposite direction.

        Case 1: Stop times in wrong order → opposite direction detection → swap route/trip to opposite direction
        Case 2: Stop times erratic (>10km from shape) → drop trip

        Args:
            vehicle_id: Vehicle identifier
            trip_id: Trip identifier
            route_id: Route identifier
            direction_id: Direction ID (0 or 1)
            positions: List of vehicle positions

        Returns:
            Tuple of (should_continue, new_route_id, new_direction_id, reason)
            - should_continue: False if trip should be dropped (Case 2)
            - new_route_id: Updated route if opposite direction detected (Case 1), else None
            - new_direction_id: Updated direction if opposite direction detected (Case 1), else None
            - reason: Description of the validation result
        """
        if not self.shape_validator:
            # If no shape validator, allow trip to continue
            return (True, None, None, "no_validator")

        if not positions:
            return (True, None, None, "no_positions")

        # Get route shape name
        route_shape = self.shape_validator.get_route_shape_name(route_id, direction_id)

        # Extract (lat, lon) tuples from positions
        pos_coords = [(p.latitude, p.longitude) for p in positions]

        # CASE 2: Check if erratic (>10km from shape)
        is_valid, avg_dist, valid_count, total_count = self.shape_validator.validate_positions_for_trip(
            pos_coords, route_shape, max_distance_km=10.0, min_valid_ratio=0.7
        )

        if not is_valid:
            reason = (
                f"CASE 2: Erratic positions - only {valid_count}/{total_count} positions valid "
                f"(avg distance: {avg_dist:.2f}km, threshold: 10km)"
            )
            logger.warning(
                f"Trip {trip_id} (vehicle {vehicle_id}): {reason}"
            )
            return (False, None, None, reason)  # Drop trip

        # CASE 1: Check opposite direction
        should_swap, opposite_route, improvement = self.shape_validator.check_opposite_direction_match(
            pos_coords, route_shape, max_distance_km=10.0
        )

        if should_swap and opposite_route:
            # Extract opposite direction_id from opposite_route (format: "routeid_direction")
            try:
                _, opposite_dir_str = opposite_route.rsplit('_', 1)
                opposite_dir = int(opposite_dir_str)
            except (ValueError, AttributeError):
                logger.error(f"Failed to parse opposite direction from {opposite_route}")
                return (True, None, None, "parse_error")

            reason = (
                f"CASE 1: Opposite direction match - direction {direction_id} -> {opposite_dir} "
                f"(improvement: {improvement:.2f}x)"
            )
            logger.info(
                f"Trip {trip_id} (vehicle {vehicle_id}): {reason}"
            )
            return (True, route_id, opposite_dir, reason)

        # No issues detected
        return (True, None, None, "valid")

    def detect_stop_times(
        self,
        trip_id: str,
        route_id: str,
        vehicle_positions: List[VehiclePositionRecord],
        stops: List[StopLocation]
    ) -> Dict[str, Tuple[Optional[datetime], Optional[datetime]]]:
        """
        Detect stop times for a trip based on historical vehicle positions.

        Algorithm to prevent false matches after trip completion:
        1. Match first stop (latest position within 100m)
        2. Match last stop (earliest position within 100m)
        3. If last stop matched and is earlier than first stop, splice positions to before last stop
        4. Re-match first stop if necessary
        5. Match remaining middle stops with existing logic
        6. Backfill gaps between detected stops
        7. If no last stop match, trip is running - use all available positions

        Args:
            trip_id: Trip identifier
            route_id: Route identifier
            vehicle_positions: List of historical vehicle positions (sorted by timestamp)
            stops: List of stop locations for this trip (in sequence order)

        Returns:
            Dictionary mapping stop_id to (arrival_time, departure_time) tuples
        """
        if not vehicle_positions or not stops:
            return {}

        # Ensure positions are sorted by timestamp
        sorted_positions = sorted(vehicle_positions, key=lambda p: p.timestamp)

        detected_times: Dict[str, Tuple[Optional[datetime], Optional[datetime]]] = {}

        # Find first and last stops
        first_stop = stops[0] if stops else None
        last_stop = stops[-1] if stops else None

        # Step 1: Match first stop (latest position within 100m)
        first_stop_time = None
        if first_stop:
            first_arrival, first_departure = self._detect_first_stop_time(first_stop, sorted_positions)
            if first_arrival:
                detected_times[first_stop.stop_id] = (first_arrival, first_departure)
                first_stop_time = first_arrival
                logger.debug(f"Trip {trip_id}: First stop {first_stop.stop_id} matched at {first_arrival}")

        # Step 2: Match last stop (earliest position within 100m)
        last_stop_time = None
        if last_stop and last_stop != first_stop:
            last_arrival, last_departure = self._detect_last_stop_time(last_stop, sorted_positions)
            if last_arrival:
                detected_times[last_stop.stop_id] = (last_arrival, last_departure)
                last_stop_time = last_arrival
                logger.debug(f"Trip {trip_id}: Last stop {last_stop.stop_id} matched at {last_arrival}")

        # Step 3 & 4: If last stop is earlier than first stop, splice and re-match first stop
        if last_stop_time and first_stop_time and last_stop_time < first_stop_time:
            # logger.info(
            #     f"Trip {trip_id}: Last stop time {last_stop_time} is earlier than first stop time {first_stop_time}. "
            #     f"Splicing positions to before last stop."
            # )

            # Splice positions to only include those before or at last stop time
            sorted_positions = [pos for pos in sorted_positions if pos.timestamp <= last_stop_time]

            # Re-match first stop with spliced positions
            if first_stop:
                first_arrival, first_departure = self._detect_first_stop_time(first_stop, sorted_positions)
                if first_arrival:
                    detected_times[first_stop.stop_id] = (first_arrival, first_departure)
                    first_stop_time = first_arrival
                    logger.debug(f"Trip {trip_id}: Re-matched first stop {first_stop.stop_id} at {first_arrival}")

        # Step 5 & 6: If last stop was matched, splice positions to end at last stop time
        # This prevents matching positions after the vehicle left the last stop (going to depot)
        if last_stop_time:
            # logger.info(
            #      f"Trip {trip_id}: Trip completed. Splicing positions to end at last stop time {last_stop_time}"
            # )
            sorted_positions = [pos for pos in sorted_positions if pos.timestamp <= last_stop_time]
        else:
            logger.debug(
                f"Trip {trip_id}: No last stop match found. Trip is likely still running."
            )

        # Step 7: Match middle stops with existing logic
        for stop in stops:
            # Skip first and last stops (already matched)
            if stop == first_stop or stop == last_stop:
                continue

            arrival_time, departure_time = self._detect_middle_stop_time(stop, sorted_positions)
            detected_times[stop.stop_id] = (arrival_time, departure_time or arrival_time)

        # Rule 4: If first stop has no match, use earliest position
        if first_stop and first_stop.stop_id in detected_times:
            first_arrival, first_departure = detected_times[first_stop.stop_id]
            if first_arrival is None and sorted_positions:
                earliest_time = sorted_positions[0].timestamp
                detected_times[first_stop.stop_id] = (earliest_time, earliest_time)
                logger.debug(
                    f"Trip {trip_id}: No first stop match, using earliest position at {earliest_time}"
                )

        # Backfill logic to fill in gaps between detected stops
        detected_times = self._backfill_skipped_stops(
            trip_id, stops, detected_times, sorted_positions
        )

        # Forward-fill unmatched stops with next detected stop's time
        # This prevents falling back to static schedules for stops that should have been passed
        detected_times = self._forward_fill_unmatched_stops(
            trip_id, stops, detected_times
        )

        return detected_times

    def _detect_stop_time(
        self,
        stop: StopLocation,
        positions: List[VehiclePositionRecord],
        is_first: bool,
        is_last: bool
    ) -> Tuple[Optional[datetime], Optional[datetime]]:
        """
        Detect stop time for a single stop based on vehicle positions.

        Returns:
            (arrival_time, departure_time) tuple
        """
        if is_first:
            return self._detect_first_stop_time(stop, positions)
        elif is_last:
            return self._detect_last_stop_time(stop, positions)
        else:
            return self._detect_middle_stop_time(stop, positions)

    def _detect_first_stop_time(
        self,
        stop: StopLocation,
        positions: List[VehiclePositionRecord]
    ) -> Tuple[Optional[datetime], Optional[datetime]]:
        """
        Detect first stop time: Within 100m, use LATEST point.
        If vehicle is still within 100m (latest point is within range), use latest point.
        """
        positions_within_range = []

        for pos in positions:
            distance = self._calculate_distance(
                pos.latitude, pos.longitude,
                stop.latitude, stop.longitude
            )
            if distance <= self.FIRST_STOP_DISTANCE_KM:
                positions_within_range.append((pos, distance))

        if not positions_within_range:
            return (None, None)

        # Use LATEST point within range
        latest_pos, _ = positions_within_range[-1]
        logger.debug(
            f"First stop {stop.stop_id}: Found latest position at {latest_pos.timestamp} "
            f"within {self.FIRST_STOP_DISTANCE_KM*1000}m"
        )

        return (latest_pos.timestamp, latest_pos.timestamp)

    def _detect_last_stop_time(
        self,
        stop: StopLocation,
        positions: List[VehiclePositionRecord]
    ) -> Tuple[Optional[datetime], Optional[datetime]]:
        """
        Detect last stop time: Within 100m, use EARLIEST point.
        """
        positions_within_range = []

        for pos in positions:
            distance = self._calculate_distance(
                pos.latitude, pos.longitude,
                stop.latitude, stop.longitude
            )
            if distance <= self.LAST_STOP_DISTANCE_KM:
                positions_within_range.append((pos, distance))

        if not positions_within_range:
            return (None, None)

        # Use EARLIEST point within range
        earliest_pos, _ = positions_within_range[0]
        logger.debug(
            f"Last stop {stop.stop_id}: Found earliest position at {earliest_pos.timestamp} "
            f"within {self.LAST_STOP_DISTANCE_KM*1000}m"
        )

        return (earliest_pos.timestamp, earliest_pos.timestamp)

    def _detect_middle_stop_time(
        self,
        stop: StopLocation,
        positions: List[VehiclePositionRecord]
    ) -> Tuple[Optional[datetime], Optional[datetime]]:
        """
        Detect middle stop time: Use ABSOLUTE CLOSEST point within 1km.
        If latest point is closest, return None (haven't passed yet).
        """
        closest_pos = None
        min_distance = float('inf')
        entered_stop_range = False

        for i, pos in enumerate(positions):
            distance = self._calculate_distance(
                pos.latitude, pos.longitude,
                stop.latitude, stop.longitude
            )

            # Check if we've entered the 1km range
            if distance <= self.MIDDLE_STOP_DISTANCE_KM:
                entered_stop_range = True

                # Track the absolute closest point
                if distance < min_distance:
                    min_distance = distance
                    closest_pos = (pos, i, distance)

        if not entered_stop_range or closest_pos is None:
            return (None, None)

        pos, pos_index, distance = closest_pos

        # Rule 2.2: If latest point is the closest, we haven't passed the stop yet
        if pos_index == len(positions) - 1:
            logger.debug(
                f"Middle stop {stop.stop_id}: Latest position is closest "
                f"({distance*1000:.1f}m), stop not yet passed"
            )
            return (None, None)

        logger.debug(
            f"Middle stop {stop.stop_id}: Found closest position at {pos.timestamp} "
            f"(distance: {distance*1000:.1f}m)"
        )

        return (pos.timestamp, pos.timestamp)

    def _backfill_skipped_stops(
        self,
        trip_id: str,
        stops: List[StopLocation],
        detected_times: Dict[str, Tuple[Optional[datetime], Optional[datetime]]],
        positions: List[VehiclePositionRecord]
    ) -> Dict[str, Tuple[Optional[datetime], Optional[datetime]]]:
        """
        Backfill missing stops between detected stops.

        If we detected Stop A at index i and Stop C at index j (j > i+1),
        but not Stop B at index i+1, we know the vehicle passed Stop B.
        Find the closest position to Stop B between Stop A and Stop C times,
        ignoring distance limits.

        Args:
            trip_id: Trip identifier
            stops: List of all stops in sequence
            detected_times: Dictionary of already detected times
            positions: List of vehicle positions

        Returns:
            Updated detected_times with backfilled stops
        """
        if not stops or not positions:
            return detected_times

        # Find gaps in detected stops
        for i in range(len(stops)):
            stop = stops[i]

            # Skip if already detected
            if stop.stop_id in detected_times and detected_times[stop.stop_id][0] is not None:
                continue

            # Find previous and next detected stops
            prev_detected_idx = None
            prev_detected_time = None
            next_detected_idx = None
            next_detected_time = None

            # Look backwards for previous detected stop
            for j in range(i - 1, -1, -1):
                prev_stop = stops[j]
                if prev_stop.stop_id in detected_times and detected_times[prev_stop.stop_id][0] is not None:
                    prev_detected_idx = j
                    prev_detected_time = detected_times[prev_stop.stop_id][0]
                    break

            # Look forwards for next detected stop
            for j in range(i + 1, len(stops)):
                next_stop = stops[j]
                if next_stop.stop_id in detected_times and detected_times[next_stop.stop_id][0] is not None:
                    next_detected_idx = j
                    next_detected_time = detected_times[next_stop.stop_id][0]
                    break

            # If we have both previous and next detected stops, backfill this stop
            if prev_detected_time is not None and next_detected_time is not None:
                # Filter positions between prev and next detected times
                positions_in_range = [
                    pos for pos in positions
                    if prev_detected_time <= pos.timestamp <= next_detected_time
                ]

                if positions_in_range:
                    # Find absolute closest position to this stop (no distance limit)
                    closest_pos = None
                    min_distance = float('inf')

                    for pos in positions_in_range:
                        distance = self._calculate_distance(
                            pos.latitude, pos.longitude,
                            stop.latitude, stop.longitude
                        )

                        if distance < min_distance:
                            min_distance = distance
                            closest_pos = pos

                    if closest_pos:
                        detected_times[stop.stop_id] = (closest_pos.timestamp, closest_pos.timestamp)
                        # logger.info(
                        #     f"Trip {trip_id}: Backfilled stop {stop.stop_id} (sequence {stop.sequence}) "
                        #     f"between detected stops at index {prev_detected_idx} and {next_detected_idx}. "
                        #     f"Closest position at {closest_pos.timestamp} (distance: {min_distance*1000:.1f}m)"
                        # )

        return detected_times

    def _forward_fill_unmatched_stops(
        self,
        trip_id: str,
        stops: List[StopLocation],
        detected_times: Dict[str, Tuple[Optional[datetime], Optional[datetime]]]
    ) -> Dict[str, Tuple[Optional[datetime], Optional[datetime]]]:
        """
        Forward-fill unmatched stops with the next detected stop's time.

        If stop 8 has no match but stop 9 has a match, use stop 9's time for stop 8.
        This prevents falling back to static schedules which can be out of temporal order.

        Args:
            trip_id: Trip identifier
            stops: List of all stops in sequence
            detected_times: Dictionary of already detected times

        Returns:
            Updated detected_times with forward-filled stops
        """
        if not stops:
            return detected_times

        for i in range(len(stops)):
            stop = stops[i]

            # Skip if already detected or backfilled
            if stop.stop_id in detected_times and detected_times[stop.stop_id][0] is not None:
                continue

            # Find next detected stop
            next_detected_time = None
            next_detected_idx = None

            for j in range(i + 1, len(stops)):
                next_stop = stops[j]
                if next_stop.stop_id in detected_times and detected_times[next_stop.stop_id][0] is not None:
                    next_detected_time = detected_times[next_stop.stop_id][0]
                    next_detected_idx = j
                    break

            # If we found a next detected stop, use its time
            if next_detected_time is not None:
                detected_times[stop.stop_id] = (next_detected_time, next_detected_time)
                # logger.info(
                #     f"Trip {trip_id}: Forward-filled stop {stop.stop_id} (sequence {stop.sequence}) "
                #     f"with next detected stop at sequence {stops[next_detected_idx].sequence}, time {next_detected_time}"
                # )

        return detected_times

    def _calculate_distance(
        self,
        lat1: float, lon1: float,
        lat2: float, lon2: float
    ) -> float:
        """
        Calculate distance between two points in kilometers using Haversine formula.

        Returns:
            Distance in kilometers
        """
        lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])

        dlat = lat2 - lat1
        dlon = lon2 - lon1
        a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
        c = 2 * asin(sqrt(a))
        r = 6371  # Earth's radius in kilometers

        return c * r

    def get_historical_positions_for_trip(
        self,
        trip_id: str,
        route_id: str,
        vehicle_id: str,
        all_positions: List[VehiclePositionRecord],
        max_age_hours: int = 6
    ) -> List[VehiclePositionRecord]:
        """
        Filter historical positions relevant to a specific trip.

        Args:
            trip_id: Trip identifier
            route_id: Route identifier
            vehicle_id: Vehicle identifier
            all_positions: All historical positions
            max_age_hours: Maximum age of positions to consider (hours)

        Returns:
            List of filtered positions for the trip
        """
        cutoff_time = datetime.now(local_tz) - timedelta(hours=max_age_hours)

        filtered_positions = [
            pos for pos in all_positions
            if (pos.vehicle_id == vehicle_id and
                pos.route_id == route_id and
                pos.timestamp >= cutoff_time)
        ]

        return sorted(filtered_positions, key=lambda p: p.timestamp)