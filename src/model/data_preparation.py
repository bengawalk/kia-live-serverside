import pandas as pd
import json
from typing import Dict, List, Tuple, Optional


class VehiclePositionProcessor:
    def __init__(self, vehicle_positions_path: str, stops_data_path: str, route_mapping_path: str = "in/routes_children_ids.json"):
        self.vehicle_positions_path = vehicle_positions_path
        self.stops_data_path = stops_data_path
        self.route_mapping_path = route_mapping_path
        self.vehicle_positions = None
        self.stops_data = None
        self.route_mapping = None

    def load_data(self):
        """Load vehicle positions and stops data"""
        self.vehicle_positions = pd.read_csv(self.vehicle_positions_path)
        with open(self.stops_data_path, 'r') as f:
            self.stops_data = json.load(f)
        with open(self.route_mapping_path, 'r') as f:
            self.route_mapping = json.load(f)

    def group_positions_by_trip(self) -> Dict[str, pd.DataFrame]:
        """Group vehicle positions by trip (vehicle_id + route_id + time window)"""
        if self.vehicle_positions is None:
            self.load_data()

        df = self.vehicle_positions.sort_values(['vehicle_id', 'route_id', 'timestamp'])
        trips = {}
        current_trip_id = 0

        for (vehicle_id, route_id), group in df.groupby(['vehicle_id', 'route_id']):
            trip_segments = self._split_into_trips(group, route_id)

            for segment in trip_segments:
                if self._is_complete_trip(segment, route_id):
                    trip_key = f"trip_{current_trip_id}_{vehicle_id}_{route_id}"
                    trips[trip_key] = segment.reset_index(drop=True)
                    current_trip_id += 1

        return trips

    def _split_into_trips(self, group: pd.DataFrame, route_id: int) -> List[pd.DataFrame]:
        """Split vehicle positions into trips based on first/last stop proximity"""
        # Get first and last stop locations for this route
        route_stops = self._get_route_stops(route_id)
        if not route_stops or len(route_stops) < 2:
            return []

        first_stop_loc = route_stops[0]['loc']
        last_stop_loc = route_stops[-1]['loc']

        # Find positions near first and last stops
        first_stop_positions = self._find_positions_near_stop(group, first_stop_loc)
        last_stop_positions = self._find_positions_near_stop(group, last_stop_loc)

        # Match first and last stop positions to form complete trips
        trips = self._match_start_end_positions(group, first_stop_positions, last_stop_positions)

        return trips

    def _find_positions_near_stop(self, group: pd.DataFrame, stop_loc: List[float]) -> List[Tuple]:
        """Find vehicle positions near a specific stop location"""
        near_positions = []
        for idx, row in group.iterrows():
            distance = self._calculate_distance(
                row['latitude'], row['longitude'],
                stop_loc[0], stop_loc[1]
            )
            if distance < 0.5:  # Within 500m
                near_positions.append((idx, row['timestamp'], distance))

        return sorted(near_positions, key=lambda x: x[1])  # Sort by timestamp

    def _match_start_end_positions(self, group: pd.DataFrame, first_positions: List[Tuple], last_positions: List[Tuple]) -> List[pd.DataFrame]:
        """Match first and last stop positions to create complete trips"""
        trips = []
        used_last_positions = set()

        for first_idx, first_timestamp, _ in first_positions:
            best_match = None
            best_match_idx = None

            for i, (last_idx, last_timestamp, _) in enumerate(last_positions):
                if i in used_last_positions:
                    continue

                # Check time constraints
                time_diff = last_timestamp - first_timestamp
                if time_diff <= 0 or time_diff > 21600:  # 0 < diff <= 6 hours
                    continue

                # Check if there's a closer first stop
                closer_first_exists = False
                for other_first_idx, other_first_timestamp, _ in first_positions:
                    if other_first_timestamp > first_timestamp and other_first_timestamp < last_timestamp:
                        if abs(last_timestamp - other_first_timestamp) < abs(last_timestamp - first_timestamp):
                            closer_first_exists = True
                            break

                if not closer_first_exists:
                    if best_match is None or time_diff < (best_match[1] - first_timestamp):
                        best_match = (last_idx, last_timestamp)
                        best_match_idx = i

            if best_match:
                used_last_positions.add(best_match_idx)
                trip_data = group[(group['timestamp'] >= first_timestamp) &
                                (group['timestamp'] <= best_match[1])].copy()
                if len(trip_data) > 5:
                    trips.append(trip_data)

        return trips

    def _get_route_stops(self, route_id: int) -> Optional[List[Dict]]:
        """Get stops for a given route using route mapping"""
        if self.stops_data is None or self.route_mapping is None:
            return None

        # Find route name from route_id using mapping
        route_name = None
        for name, mapped_id in self.route_mapping.items():
            if mapped_id == route_id:
                route_name = name
                break

        if route_name and route_name in self.stops_data:
            route_data = self.stops_data[route_name]
            if 'stops' in route_data and isinstance(route_data['stops'], list):
                return route_data['stops']

        return None

    def _calculate_distance(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Calculate distance between two points in kilometers using Haversine formula"""
        from math import radians, cos, sin, asin, sqrt

        lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])

        dlat = lat2 - lat1
        dlon = lon2 - lon1
        a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
        c = 2 * asin(sqrt(a))
        r = 6371

        return c * r

    def _is_complete_trip(self, trip_df: pd.DataFrame, route_id: int) -> bool:
        """Check if trip is complete"""
        return len(trip_df) >= 10


class StopTimeConverter:
    def __init__(self, stops_data_path: str, route_mapping_path: str = "in/routes_children_ids.json"):
        self.stops_data_path = stops_data_path
        self.route_mapping_path = route_mapping_path
        self.stops_data = None
        self.route_mapping = None

    def load_stops_data(self):
        """Load stops data"""
        with open(self.stops_data_path, 'r') as f:
            self.stops_data = json.load(f)
        with open(self.route_mapping_path, 'r') as f:
            self.route_mapping = json.load(f)

    def convert_positions_to_stop_times(self, trip_df: pd.DataFrame, route_id: int) -> List[Dict]:
        """Convert vehicle positions to stop times for a trip"""
        if self.stops_data is None:
            self.load_stops_data()

        route_stops = self._get_route_stops(route_id)
        if not route_stops:
            return []

        stop_times = []

        for i, stop in enumerate(route_stops):
            stop_location = stop['loc']

            closest_position, arrival_time = self._find_closest_position(
                trip_df, stop_location[0], stop_location[1]
            )

            if closest_position is not None:
                stop_times.append({
                    'stop_id': stop.get('stop_id', stop.get('id')),
                    'stop_name': stop.get('name', ''),
                    'arrival_time': arrival_time,
                    'departure_time': arrival_time,
                    'latitude': stop_location[0],
                    'longitude': stop_location[1],
                    'sequence': i
                })

        return stop_times

    def _get_route_stops(self, route_id: int) -> Optional[List[Dict]]:
        """Get stops for a given route using route mapping"""
        if self.stops_data is None or self.route_mapping is None:
            return None

        # Find route name from route_id using mapping
        route_name = None
        for name, mapped_id in self.route_mapping.items():
            if mapped_id == route_id:
                route_name = name
                break

        if route_name and route_name in self.stops_data:
            route_data = self.stops_data[route_name]
            if 'stops' in route_data and isinstance(route_data['stops'], list):
                return route_data['stops']

        return None

    def _find_closest_position(self, trip_df: pd.DataFrame, stop_lat: float, stop_lon: float) -> Tuple[Optional[pd.Series], Optional[int]]:
        """Find the vehicle position closest to a stop"""
        min_distance = float('inf')
        closest_position = None
        arrival_time = None

        for _, position in trip_df.iterrows():
            distance = self._calculate_distance(
                position['latitude'], position['longitude'],
                stop_lat, stop_lon
            )

            if distance < min_distance:
                min_distance = distance
                closest_position = position
                arrival_time = position['timestamp']

        if min_distance < 1.0:
            return closest_position, arrival_time

        return None, None

    def _calculate_distance(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Calculate distance between two points in kilometers"""
        from math import radians, cos, sin, asin, sqrt

        lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])

        dlat = lat2 - lat1
        dlon = lon2 - lon1
        a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
        c = 2 * asin(sqrt(a))
        r = 6371

        return c * r