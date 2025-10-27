import numpy as np
import pandas as pd
from typing import Dict, List, Any
from .stop_to_stop_data import StopToStopDataProcessor
from .universal_training import UniversalStopTimeModel


class UniversalModelValidator:
    def __init__(self, model_dir: str = "universal_models"):
        self.model_dir = model_dir
        self.universal_model = UniversalStopTimeModel(model_dir)

    def validate_model(self, vehicle_positions_path: str, stops_data_path: str,
                      train_split: float = 0.7) -> Dict[str, Any]:
        """Validate universal model using train/test split"""

        # Load model
        success = self.universal_model.load_model()
        if not success:
            return {"error": "Failed to load universal model"}

        print("Preparing validation data...")

        # Process stop-to-stop data
        processor = StopToStopDataProcessor(vehicle_positions_path, stops_data_path)
        stop_pairs = processor.process_all_routes()

        if len(stop_pairs) < 20:
            return {"error": "Insufficient data for validation"}

        # Split data into train/test
        np.random.seed(42)
        indices = np.random.permutation(len(stop_pairs))
        split_idx = int(len(stop_pairs) * train_split)

        test_pairs = [stop_pairs[i] for i in indices[split_idx:]]

        print(f"Validating on {len(test_pairs)} stop-to-stop samples...")

        # Extract features and actual travel times for test set
        predictions = []
        actuals = []

        for pair in test_pairs:
            features = processor.extract_features(pair)
            actual_travel_time = pair['travel_time']

            # Scale features and predict
            features_scaled = self.universal_model.scaler.transform(features.reshape(1, -1))
            predicted_travel_time = self.universal_model.model.predict(features_scaled)[0]

            predictions.append(predicted_travel_time)
            actuals.append(actual_travel_time)

        # Calculate metrics
        predictions = np.array(predictions)
        actuals = np.array(actuals)

        errors = np.abs(predictions - actuals)
        relative_errors = errors / actuals

        metrics = {
            "samples_tested": len(test_pairs),
            "mean_absolute_error": float(np.mean(errors)),
            "median_error": float(np.median(errors)),
            "std_error": float(np.std(errors)),
            "mean_relative_error": float(np.mean(relative_errors)),
            "max_error": float(np.max(errors)),
            "min_error": float(np.min(errors)),
            "accuracy_within_30s": float(np.mean(errors <= 30)),
            "accuracy_within_60s": float(np.mean(errors <= 60)),
            "accuracy_within_120s": float(np.mean(errors <= 120)),
            "accuracy_within_300s": float(np.mean(errors <= 300)),
            "r2_score": self._calculate_r2(actuals, predictions)
        }

        return metrics

    def _calculate_r2(self, actual: np.ndarray, predicted: np.ndarray) -> float:
        """Calculate R-squared score"""
        ss_res = np.sum((actual - predicted) ** 2)
        ss_tot = np.sum((actual - np.mean(actual)) ** 2)
        return float(1 - (ss_res / ss_tot)) if ss_tot != 0 else 0.0