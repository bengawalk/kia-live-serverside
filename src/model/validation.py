import numpy as np
import pandas as pd
import json
from typing import Dict, List, Any, Tuple
from .data_preparation import VehiclePositionProcessor, StopTimeConverter
from .prediction import StopTimePredictionEngine


class ModelValidator:
    def __init__(self, model_dir: str = "models"):
        self.model_dir = model_dir
        self.engine = StopTimePredictionEngine(model_dir)

    def prepare_validation_data(self, vehicle_positions_path: str, stops_data_path: str) -> Dict[int, List[Dict]]:
        """Prepare validation data - same as training data preparation"""
        processor = VehiclePositionProcessor(vehicle_positions_path, stops_data_path)
        converter = StopTimeConverter(stops_data_path)

        trips = processor.group_positions_by_trip()
        validation_data = {}

        for trip_id, trip_df in trips.items():
            route_id = trip_df.iloc[0]['route_id']
            stop_times = converter.convert_positions_to_stop_times(trip_df, route_id)

            if len(stop_times) > 4:  # Need at least 5 stops for meaningful validation
                if route_id not in validation_data:
                    validation_data[route_id] = []

                validation_data[route_id].append({
                    'trip_id': trip_id,
                    'stop_times': stop_times,
                    'positions': trip_df
                })

        return validation_data

    def split_trip_data(self, stop_times: List[Dict], split_ratio: float = 0.6) -> Tuple[List[Dict], List[Dict]]:
        """Split trip data into first part (for prediction) and second part (for validation)"""
        if len(stop_times) < 3:
            return [], []

        split_index = max(2, int(len(stop_times) * split_ratio))  # At least 2 stops for prediction
        if split_index >= len(stop_times) - 1:  # Need at least 1 stop for validation
            split_index = len(stop_times) - 2

        first_part = stop_times[:split_index + 1]
        second_part = stop_times[split_index + 1:]

        return first_part, second_part

    def validate_route(self, route_id: int, route_validation_data: List[Dict], stops_data_path: str) -> Dict[str, Any]:
        """Validate model performance for a specific route"""
        print(f"Validating route {route_id} with {len(route_validation_data)} trips")

        # Load model for this route
        self.engine.load_models([route_id])

        if route_id not in self.engine.models:
            print(f"No model found for route {route_id}")
            return {}

        all_errors = []
        all_relative_errors = []
        predictions_made = 0

        for trip_data in route_validation_data:
            stop_times = trip_data['stop_times']
            positions = trip_data['positions']

            # Split trip data
            first_part, second_part = self.split_trip_data(stop_times)

            if len(first_part) < 2 or len(second_part) < 1:
                continue

            # Create positions dataframe for first part
            first_part_positions = self._create_partial_positions(positions, first_part)

            # Make predictions
            predictions = self.engine.predict_remaining_stop_times(
                first_part_positions, route_id, stops_data_path
            )

            if not predictions:
                continue

            # Compare predictions with actual times
            for pred in predictions:
                pred_sequence = pred['sequence']

                # Find corresponding actual stop
                actual_stop = None
                for actual in second_part:
                    if actual['sequence'] == pred_sequence:
                        actual_stop = actual
                        break

                if actual_stop:
                    error = abs(pred['predicted_arrival_time'] - actual_stop['arrival_time'])
                    relative_error = error / actual_stop['arrival_time'] if actual_stop['arrival_time'] > 0 else 0

                    all_errors.append(error)
                    all_relative_errors.append(relative_error)
                    predictions_made += 1

        if not all_errors:
            return {"error": "No valid predictions made"}

        # Calculate metrics
        mean_error = np.mean(all_errors)
        std_error = np.std(all_errors)
        mean_relative_error = np.mean(all_relative_errors)
        median_error = np.median(all_errors)

        return {
            "route_id": route_id,
            "predictions_made": predictions_made,
            "mean_absolute_error": mean_error,
            "std_error": std_error,
            "median_error": median_error,
            "mean_relative_error": mean_relative_error,
            "accuracy_within_60s": sum(1 for e in all_errors if e <= 60) / len(all_errors),
            "accuracy_within_120s": sum(1 for e in all_errors if e <= 120) / len(all_errors),
            "accuracy_within_300s": sum(1 for e in all_errors if e <= 300) / len(all_errors)
        }

    def _create_partial_positions(self, full_positions: pd.DataFrame, first_part_stops: List[Dict]) -> pd.DataFrame:
        """Create partial positions dataframe up to the last stop in first_part"""
        if not first_part_stops:
            return full_positions.iloc[:0].copy()

        last_stop_time = first_part_stops[-1]['arrival_time']

        # Filter positions up to the last stop time
        partial_positions = full_positions[full_positions['timestamp'] <= last_stop_time].copy()

        return partial_positions