import pickle
import numpy as np
import pandas as pd
import os
from typing import Dict, List, Any
from sklearn.ensemble import RandomForestRegressor
from sklearn.preprocessing import StandardScaler
from .stop_to_stop_data import StopToStopDataProcessor


class UniversalStopTimeModel:
    def __init__(self, model_dir: str = "universal_models"):
        self.model_dir = model_dir
        self.model = None
        self.scaler = None
        os.makedirs(model_dir, exist_ok=True)

    def train(self, vehicle_positions_path: str, stops_data_path: str):
        """Train universal stop-to-stop model"""
        print("Starting universal model training...")

        # Process stop-to-stop data
        processor = StopToStopDataProcessor(vehicle_positions_path, stops_data_path)
        stop_pairs = processor.process_all_routes()

        if len(stop_pairs) < 10:
            print("Insufficient stop-to-stop data for training")
            return

        # Extract features and targets
        print("Extracting features...")
        X_all = []
        y_all = []

        for pair in stop_pairs:
            features = processor.extract_features(pair)
            travel_time = pair['travel_time']

            X_all.append(features)
            y_all.append(travel_time)

        X = np.array(X_all)
        y = np.array(y_all)

        print(f"Training with {len(X)} stop-to-stop samples")
        print(f"Feature dimension: {X.shape[1]}")

        # Scale features
        self.scaler = StandardScaler()
        X_scaled = self.scaler.fit_transform(X)

        # Train model
        self.model = RandomForestRegressor(
            n_estimators=200,
            max_depth=15,
            min_samples_split=5,
            min_samples_leaf=2,
            random_state=42,
            n_jobs=-1
        )
        self.model.fit(X_scaled, y)

        print("Universal model training completed!")

    def save_model(self):
        """Save the universal model and scaler"""
        if self.model is None or self.scaler is None:
            print("No model to save")
            return

        model_path = os.path.join(self.model_dir, "universal_model.pkl")
        scaler_path = os.path.join(self.model_dir, "universal_scaler.pkl")

        with open(model_path, 'wb') as f:
            pickle.dump(self.model, f)

        with open(scaler_path, 'wb') as f:
            pickle.dump(self.scaler, f)

        print(f"Universal model saved to {self.model_dir}")

    def load_model(self):
        """Load the universal model and scaler"""
        model_path = os.path.join(self.model_dir, "universal_model.pkl")
        scaler_path = os.path.join(self.model_dir, "universal_scaler.pkl")

        if os.path.exists(model_path) and os.path.exists(scaler_path):
            with open(model_path, 'rb') as f:
                self.model = pickle.load(f)

            with open(scaler_path, 'rb') as f:
                self.scaler = pickle.load(f)

            print("Universal model loaded successfully")
            return True
        else:
            print("Universal model files not found")
            return False