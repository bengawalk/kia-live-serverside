import pandas as pd
import json
import numpy as np
from typing import Dict, List, Tuple, Optional
from datetime import datetime
from .data_preparation import VehiclePositionProcessor, StopTimeConverter


class StopToStopDataProcessor:
    def __init__(self, vehicle_positions_path: str, stops_data_path: str, route_mapping_path: str = "in/routes_children_ids.json"):
        self.vehicle_positions_path = vehicle_positions_path
        self.stops_data_path = stops_data_path
        self.route_mapping_path = route_mapping_path
        self.stop_to_stop_data = []

    def process_all_routes(self) -> List[Dict]:
        """Process all routes and create stop-to-stop training data"""
        processor = VehiclePositionProcessor(
            self.vehicle_positions_path,
            self.stops_data_path,
            self.route_mapping_path
        )
        converter = StopTimeConverter(self.stops_data_path, self.route_mapping_path)

        # Get all trips
        trips = processor.group_positions_by_trip()
        print(f"Processing {len(trips)} trips for stop-to-stop data...")

        for trip_id, trip_df in trips.items():
            route_id = trip_df.iloc[0]['route_id']

            # Convert to stop times
            stop_times = converter.convert_positions_to_stop_times(trip_df, route_id)

            if len(stop_times) >= 2:
                # Create stop-to-stop pairs
                stop_pairs = self._create_stop_to_stop_pairs(stop_times, route_id)
                self.stop_to_stop_data.extend(stop_pairs)

        print(f"Created {len(self.stop_to_stop_data)} stop-to-stop training samples")
        return self.stop_to_stop_data

    def _create_stop_to_stop_pairs(self, stop_times: List[Dict], route_id: int) -> List[Dict]:
        """Create stop-to-stop training pairs from a trip"""
        pairs = []

        for i in range(len(stop_times) - 1):
            from_stop = stop_times[i]
            to_stop = stop_times[i + 1]

            # Calculate travel time
            travel_time = to_stop['arrival_time'] - from_stop['arrival_time']

            if travel_time > 0:  # Valid travel time
                pair = {
                    'from_stop_id': from_stop.get('stop_id'),
                    'to_stop_id': to_stop.get('stop_id'),
                    'from_stop_lat': from_stop['latitude'],
                    'from_stop_lon': from_stop['longitude'],
                    'to_stop_lat': to_stop['latitude'],
                    'to_stop_lon': to_stop['longitude'],
                    'from_stop_time': from_stop['arrival_time'],
                    'to_stop_time': to_stop['arrival_time'],
                    'travel_time': travel_time,
                    'route_id': route_id,
                    'sequence_position': i / (len(stop_times) - 1),  # Position in trip (0 to 1)
                    'trip_progress': from_stop['arrival_time'] - stop_times[0]['arrival_time']  # Time since trip start
                }
                pairs.append(pair)

        return pairs

    def extract_features(self, stop_pair: Dict) -> np.ndarray:
        """Extract features for stop-to-stop prediction"""
        from_time = stop_pair['from_stop_time']
        dt = datetime.fromtimestamp(from_time)

        # Temporal features
        hour_of_day = dt.hour
        day_of_week = dt.weekday()

        # Cyclic encoding
        hour_sin = np.sin(2 * np.pi * hour_of_day / 24)
        hour_cos = np.cos(2 * np.pi * hour_of_day / 24)
        day_sin = np.sin(2 * np.pi * day_of_week / 7)
        day_cos = np.cos(2 * np.pi * day_of_week / 7)

        # Weekend and rush hour indicators
        is_weekend = 1 if day_of_week >= 5 else 0
        is_morning_rush = 1 if 7 <= hour_of_day <= 10 else 0
        is_evening_rush = 1 if 17 <= hour_of_day <= 20 else 0

        # Geographic features
        from_lat = stop_pair['from_stop_lat']
        from_lon = stop_pair['from_stop_lon']
        to_lat = stop_pair['to_stop_lat']
        to_lon = stop_pair['to_stop_lon']

        # Distance between stops
        distance = self._calculate_distance(from_lat, from_lon, to_lat, to_lon)

        # Trip context
        sequence_position = stop_pair['sequence_position']
        trip_progress = stop_pair['trip_progress']

        # Route context (could be expanded with route-specific features)
        route_id_normalized = stop_pair['route_id'] / 20000.0  # Normalize route ID

        features = np.array([
            from_time,           # Start time
            hour_sin, hour_cos,  # Cyclic hour
            day_sin, day_cos,    # Cyclic day
            is_weekend,          # Weekend flag
            is_morning_rush,     # Morning rush
            is_evening_rush,     # Evening rush
            from_lat, from_lon,  # From stop location
            to_lat, to_lon,      # To stop location
            distance,            # Distance between stops
            sequence_position,   # Position in trip sequence
            trip_progress,       # Time since trip start
            route_id_normalized  # Route context
        ])

        return features

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