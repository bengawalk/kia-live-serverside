import pickle
import numpy as np
import pandas as pd
import os
from datetime import datetime
from typing import Dict, List, Any
from sklearn.linear_model import LinearRegression
from sklearn.ensemble import RandomForestRegressor
from sklearn.preprocessing import StandardScaler
from .data_preparation import VehiclePositionProcessor, StopTimeConverter


class StopTimePredictionModel:
    def __init__(self, model_dir: str = "models"):
        self.model_dir = model_dir
        self.models = {}
        self.scalers = {}
        self.route_models = {}

        os.makedirs(model_dir, exist_ok=True)

    def prepare_training_data(self, vehicle_positions_path: str, stops_data_path: str) -> Dict[int, List[Dict]]:
        """Prepare training data from vehicle positions"""
        processor = VehiclePositionProcessor(vehicle_positions_path, stops_data_path)
        converter = StopTimeConverter(stops_data_path)

        # Group positions by trip
        trips = processor.group_positions_by_trip()

        training_data = {}

        for trip_id, trip_df in trips.items():
            route_id = trip_df.iloc[0]['route_id']

            # Convert positions to stop times
            stop_times = converter.convert_positions_to_stop_times(trip_df, route_id)

            if len(stop_times) > 2:  # Need at least 3 stops for meaningful training
                if route_id not in training_data:
                    training_data[route_id] = []

                training_data[route_id].append({
                    'trip_id': trip_id,
                    'stop_times': stop_times,
                    'positions': trip_df
                })

        return training_data

    def extract_features(self, stop_times: List[Dict]) -> np.ndarray:
        """Extract features from stop times for training including temporal features"""
        features = []

        for i, stop_time in enumerate(stop_times):
            # Time-based features
            arrival_time = stop_time['arrival_time']

            # Convert timestamp to datetime for temporal feature extraction
            dt = datetime.fromtimestamp(arrival_time)

            # Temporal features
            hour_of_day = dt.hour
            day_of_week = dt.weekday()  # 0=Monday, 6=Sunday

            # Cyclic encoding for temporal features (to handle wraparound)
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
            sequence_ratio = i / len(stop_times) if len(stop_times) > 1 else 0

            # Time differences (if not first stop)
            time_since_start = 0
            if i > 0:
                time_since_start = arrival_time - stop_times[0]['arrival_time']

            features.append([
                arrival_time,          # Original timestamp
                sequence_ratio,        # Position in route
                time_since_start,      # Elapsed time since trip start
                stop_time['latitude'], # Geographic features
                stop_time['longitude'],
                hour_sin,             # Cyclic hour encoding
                hour_cos,
                day_sin,              # Cyclic day encoding
                day_cos,
                is_weekend,           # Weekend flag
                is_morning_rush,      # Morning rush hour flag
                is_evening_rush       # Evening rush hour flag
            ])

        return np.array(features)

    def train_route_model(self, route_id: int, route_training_data: List[Dict]):
        """Train model for a specific route"""
        print(f"Training model for route {route_id} with {len(route_training_data)} trips")

        X_all = []
        y_all = []

        for trip_data in route_training_data:
            stop_times = trip_data['stop_times']
            features = self.extract_features(stop_times)

            # Create input-output pairs for stop-to-stop travel times
            for i in range(len(features) - 1):
                current_features = features[i]
                next_arrival = stop_times[i + 1]['arrival_time']
                travel_time = next_arrival - stop_times[i]['arrival_time']

                X_all.append(current_features)
                y_all.append(travel_time)

        if len(X_all) < 5:  # Need minimum data points
            print(f"Insufficient data for route {route_id}")
            return

        X = np.array(X_all)
        y = np.array(y_all)

        # Scale features
        scaler = StandardScaler()
        X_scaled = scaler.fit_transform(X)

        # Train model
        model = RandomForestRegressor(n_estimators=100, random_state=42)
        model.fit(X_scaled, y)

        # Store model and scaler
        self.models[route_id] = model
        self.scalers[route_id] = scaler

        print(f"Model trained for route {route_id}")

    def save_models(self):
        """Save trained models and scalers to disk"""
        for route_id in self.models:
            model_path = os.path.join(self.model_dir, f"model_route_{route_id}.pkl")
            scaler_path = os.path.join(self.model_dir, f"scaler_route_{route_id}.pkl")

            with open(model_path, 'wb') as f:
                pickle.dump(self.models[route_id], f)

            with open(scaler_path, 'wb') as f:
                pickle.dump(self.scalers[route_id], f)

        print(f"Models saved to {self.model_dir}")