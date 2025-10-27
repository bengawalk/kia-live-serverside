import numpy as np
import pandas as pd
import json
from typing import Dict, List, Optional
from datetime import datetime
from .universal_training import UniversalStopTimeModel
from .data_preparation import StopTimeConverter


class UniversalPredictionEngine:
    def __init__(self, model_dir: str = "universal_models"):
        self.model_dir = model_dir
        self.universal_model = UniversalStopTimeModel(model_dir)
        self.route_mapping = None
        self.stops_data = None

    def load_model_and_data(self, stops_data_path: str, route_mapping_path: str = "in/routes_children_ids.json"):
        """Load universal model and route data"""
        success = self.universal_model.load_model()
        if not success:
            return False

        # Load route data
        with open(stops_data_path, 'r') as f:
            self.stops_data = json.load(f)
        with open(route_mapping_path, 'r') as f:
            self.route_mapping = json.load(f)

        return True

    def predict_trip_from_start(self, route_id: int, start_time: float, start_position: Optional[Dict] = None) -> List[Dict]:
        """Predict entire trip from start time and optional first position"""
        if not self.universal_model.model:
            print("Universal model not loaded")
            return []

        # Get route stops
        route_stops = self._get_route_stops(route_id)
        if not route_stops or len(route_stops) < 2:
            print(f"No stops found for route {route_id}")
            return []

        predictions = []
        current_time = start_time

        # If we have a start position, find the closest stop to start from
        start_stop_index = 0
        if start_position:
            start_stop_index = self._find_closest_stop_index(start_position, route_stops)

        # Predict from current stop to all subsequent stops
        for i in range(start_stop_index, len(route_stops) - 1):
            from_stop = route_stops[i]
            to_stop = route_stops[i + 1]

            # Create stop pair for prediction
            stop_pair = {
                'from_stop_lat': from_stop['loc'][0],
                'from_stop_lon': from_stop['loc'][1],
                'to_stop_lat': to_stop['loc'][0],
                'to_stop_lon': to_stop['loc'][1],
                'from_stop_time': current_time,
                'route_id': route_id,
                'sequence_position': i / (len(route_stops) - 1),
                'trip_progress': current_time - start_time
            }

            # Extract features
            features = self._extract_prediction_features(stop_pair)

            # Scale and predict
            features_scaled = self.universal_model.scaler.transform(features.reshape(1, -1))
            predicted_travel_time = self.universal_model.model.predict(features_scaled)[0]

            # Update current time
            current_time += predicted_travel_time

            # Add prediction
            predictions.append({
                'stop_id': to_stop.get('stop_id', to_stop.get('id')),
                'stop_name': to_stop.get('name', ''),
                'predicted_arrival_time': current_time,
                'predicted_departure_time': current_time,
                'latitude': to_stop['loc'][0],
                'longitude': to_stop['loc'][1],
                'sequence': i + 1,
                'predicted_travel_time': predicted_travel_time
            })

        return predictions

    def _extract_prediction_features(self, stop_pair: Dict) -> np.ndarray:
        """Extract features for prediction (same as training)"""
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

        # Distance calculation
        distance = self._calculate_distance(from_lat, from_lon, to_lat, to_lon)

        # Trip context
        sequence_position = stop_pair['sequence_position']
        trip_progress = stop_pair['trip_progress']
        route_id_normalized = stop_pair['route_id'] / 20000.0

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

    def _get_route_stops(self, route_id: int) -> Optional[List[Dict]]:
        """Get stops for a given route"""
        if not self.stops_data or not self.route_mapping:
            return None

        # Find route name
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

    def _find_closest_stop_index(self, position: Dict, route_stops: List[Dict]) -> int:
        """Find the index of the closest stop to a given position"""
        min_distance = float('inf')
        closest_index = 0

        pos_lat = position.get('latitude', position.get('lat', 0))
        pos_lon = position.get('longitude', position.get('lon', 0))

        for i, stop in enumerate(route_stops):
            distance = self._calculate_distance(
                pos_lat, pos_lon,
                stop['loc'][0], stop['loc'][1]
            )
            if distance < min_distance:
                min_distance = distance
                closest_index = i

        return closest_index

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

    def predict_from_current_position(self, positions_df: pd.DataFrame, route_id: int) -> List[Dict]:
        """Predict remaining stops from current vehicle positions"""
        if not self.universal_model.model:
            print("Universal model not loaded")
            return []

        # Convert current positions to stop times
        converter = StopTimeConverter("in/client_stops.json", "in/routes_children_ids.json")
        current_stop_times = converter.convert_positions_to_stop_times(positions_df, route_id)

        if len(current_stop_times) == 0:
            print("No stops detected from current positions")
            return []

        # Get the latest stop time as starting point
        latest_stop = current_stop_times[-1]
        current_time = latest_stop['arrival_time']
        current_sequence = latest_stop['sequence']

        # Get route stops
        route_stops = self._get_route_stops(route_id)
        if not route_stops or current_sequence >= len(route_stops) - 1:
            print("Trip already complete or no route data")
            return []

        # Predict remaining stops
        predictions = []
        trip_start_time = current_stop_times[0]['arrival_time'] if len(current_stop_times) > 0 else current_time

        for i in range(current_sequence, len(route_stops) - 1):
            from_stop = route_stops[i]
            to_stop = route_stops[i + 1]

            stop_pair = {
                'from_stop_lat': from_stop['loc'][0],
                'from_stop_lon': from_stop['loc'][1],
                'to_stop_lat': to_stop['loc'][0],
                'to_stop_lon': to_stop['loc'][1],
                'from_stop_time': current_time,
                'route_id': route_id,
                'sequence_position': i / (len(route_stops) - 1),
                'trip_progress': current_time - trip_start_time
            }

            features = self._extract_prediction_features(stop_pair)
            features_scaled = self.universal_model.scaler.transform(features.reshape(1, -1))
            predicted_travel_time = self.universal_model.model.predict(features_scaled)[0]

            current_time += predicted_travel_time

            predictions.append({
                'stop_id': to_stop.get('stop_id', to_stop.get('id')),
                'stop_name': to_stop.get('name', ''),
                'predicted_arrival_time': current_time,
                'predicted_departure_time': current_time,
                'latitude': to_stop['loc'][0],
                'longitude': to_stop['loc'][1],
                'sequence': i + 1,
                'predicted_travel_time': predicted_travel_time
            })

        return predictions

    def _extract_prediction_features(self, stop_pair: Dict) -> np.ndarray:
        """Extract features for prediction (same as training)"""
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

        # Distance calculation
        distance = self._calculate_distance(from_lat, from_lon, to_lat, to_lon)

        # Trip context
        sequence_position = stop_pair['sequence_position']
        trip_progress = stop_pair['trip_progress']
        route_id_normalized = stop_pair['route_id'] / 20000.0

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

    def _get_route_stops(self, route_id: int) -> Optional[List[Dict]]:
        """Get stops for a given route"""
        if not self.stops_data or not self.route_mapping:
            return None

        # Find route name
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

    def _find_closest_stop_index(self, position: Dict, route_stops: List[Dict]) -> int:
        """Find the index of the closest stop to a given position"""
        min_distance = float('inf')
        closest_index = 0

        pos_lat = position.get('latitude', position.get('lat', 0))
        pos_lon = position.get('longitude', position.get('lon', 0))

        for i, stop in enumerate(route_stops):
            distance = self._calculate_distance(
                pos_lat, pos_lon,
                stop['loc'][0], stop['loc'][1]
            )
            if distance < min_distance:
                min_distance = distance
                closest_index = i

        return closest_index

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