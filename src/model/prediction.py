import pickle
import numpy as np
import pandas as pd
import os
from datetime import datetime
from typing import Dict, List, Any, Optional
from .data_preparation import StopTimeConverter


class StopTimePredictionEngine:
    def __init__(self, model_dir: str = "models"):
        self.model_dir = model_dir
        self.models = {}
        self.scalers = {}

    def load_models(self, route_ids: List[int] = None):
        """Load trained models from disk"""
        if route_ids is None:
            # Load all available models
            route_ids = []
            for filename in os.listdir(self.model_dir):
                if filename.startswith("model_route_") and filename.endswith(".pkl"):
                    route_id = int(filename.replace("model_route_", "").replace(".pkl", ""))
                    route_ids.append(route_id)

        for route_id in route_ids:
            model_path = os.path.join(self.model_dir, f"model_route_{route_id}.pkl")
            scaler_path = os.path.join(self.model_dir, f"scaler_route_{route_id}.pkl")

            if os.path.exists(model_path) and os.path.exists(scaler_path):
                with open(model_path, 'rb') as f:
                    self.models[route_id] = pickle.load(f)

                with open(scaler_path, 'rb') as f:
                    self.scalers[route_id] = pickle.load(f)

                print(f"Loaded model for route {route_id}")
            else:
                print(f"Model files not found for route {route_id}")

    def predict_remaining_stop_times(self, incomplete_positions: pd.DataFrame, route_id: int, stops_data_path: str) -> List[Dict]:
        """Predict remaining stop times for an incomplete trip"""
        if route_id not in self.models:
            print(f"No model available for route {route_id}")
            return []

        # Convert current positions to stop times for completed portion
        converter = StopTimeConverter(stops_data_path)
        current_stop_times = converter.convert_positions_to_stop_times(incomplete_positions, route_id)

        if len(current_stop_times) < 2:
            print("Need at least 2 completed stops for prediction")
            return []

        # Get all stops for this route
        route_stops = converter._get_route_stops(route_id)
        if not route_stops:
            return []

        # Find current position in route
        last_completed_stop = current_stop_times[-1]
        current_stop_index = last_completed_stop['sequence']

        if current_stop_index >= len(route_stops) - 1:
            print("Trip already complete")
            return []

        # Predict remaining stops
        predictions = []
        current_time = last_completed_stop['arrival_time']

        for i in range(current_stop_index + 1, len(route_stops)):
            stop = route_stops[i]

            # Create features for prediction with temporal information
            features = self._extract_prediction_features(
                current_time, i, len(route_stops),
                current_time - current_stop_times[0]['arrival_time'],
                stop['loc'][0], stop['loc'][1]
            )

            # Scale features
            features_scaled = self.scalers[route_id].transform(features)

            # Predict travel time to next stop
            travel_time = self.models[route_id].predict(features_scaled)[0]
            current_time += travel_time

            predictions.append({
                'stop_id': stop.get('stop_id', stop.get('id')),
                'stop_name': stop.get('name', ''),
                'predicted_arrival_time': current_time,
                'predicted_departure_time': current_time,
                'latitude': stop['loc'][0],
                'longitude': stop['loc'][1],
                'sequence': i
            })

        return predictions

    def _extract_prediction_features(self, arrival_time: float, sequence: int, total_stops: int,
                                    time_since_start: float, latitude: float, longitude: float) -> np.ndarray:
        """Extract features for prediction matching training feature format"""
        # Convert timestamp to datetime for temporal feature extraction
        dt = datetime.fromtimestamp(arrival_time)

        # Temporal features
        hour_of_day = dt.hour
        day_of_week = dt.weekday()  # 0=Monday, 6=Sunday

        # Cyclic encoding for temporal features
        hour_sin = np.sin(2 * np.pi * hour_of_day / 24)
        hour_cos = np.cos(2 * np.pi * hour_of_day / 24)
        day_sin = np.sin(2 * np.pi * day_of_week / 7)
        day_cos = np.cos(2 * np.pi * day_of_week / 7)

        # Weekend indicator
        is_weekend = 1 if day_of_week >= 5 else 0  # Sat=5, Sun=6

        # Rush hour indicators
        is_morning_rush = 1 if 7 <= hour_of_day <= 10 else 0
        is_evening_rush = 1 if 17 <= hour_of_day <= 20 else 0

        # Position in sequence
        sequence_ratio = sequence / total_stops if total_stops > 1 else 0

        # Create feature vector matching training format
        features = np.array([[
            arrival_time,          # Original timestamp
            sequence_ratio,        # Position in route
            time_since_start,      # Elapsed time since trip start
            latitude,             # Geographic features
            longitude,
            hour_sin,             # Cyclic hour encoding
            hour_cos,
            day_sin,              # Cyclic day encoding
            day_cos,
            is_weekend,           # Weekend flag
            is_morning_rush,      # Morning rush hour flag
            is_evening_rush       # Evening rush hour flag
        ]])

        return features