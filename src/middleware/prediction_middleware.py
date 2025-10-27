"""
Prediction middleware for generating fresh times.json on service startup.
Runs predictions based on current date instead of static Wednesday predictions.
"""

import os
import json
import datetime
import logging
from pathlib import Path
from typing import Dict, Any, Optional

from src.model.universal_prediction import UniversalPredictionEngine

logger = logging.getLogger(__name__)


class PredictionMiddleware:
    """Middleware to run fresh predictions on startup when --predict-times flag is set"""

    def __init__(self, config, model_dir: str = "models"):
        self.config = config
        self.model_dir = model_dir
        self.engine: Optional[UniversalPredictionEngine] = None

    async def generate_fresh_predictions(self) -> bool:
        """
        Generate fresh predictions for all routes using the universal model.
        Updates times.json in the in/ directory with predictions based on current date.

        Returns:
            bool: True if successful, False otherwise
        """
        logger.info("Starting fresh prediction generation with current date...")

        try:
            # Load required input files
            in_dir = self.config.in_dir

            client_stops_path = in_dir / "client_stops.json"
            route_children_ids_path = in_dir / "routes_children_ids.json"
            timings_tsv_path = Path("helpers/construct_timings/timings.tsv")

            # Check if required files exist
            if not client_stops_path.exists():
                logger.error(f"client_stops.json not found at {client_stops_path}")
                return False
            if not route_children_ids_path.exists():
                logger.error(f"routes_children_ids.json not found at {route_children_ids_path}")
                return False
            if not timings_tsv_path.exists():
                logger.warning(f"timings.tsv not found at {timings_tsv_path}, will try times.json")

            # Load route data
            with open(client_stops_path, 'r') as f:
                client_stops = json.load(f)
            with open(route_children_ids_path, 'r') as f:
                route_children_ids = json.load(f)

            # Load timings (trip start times)
            timings = self._load_timings(timings_tsv_path, in_dir / "times.json")
            if not timings:
                logger.error("Could not load timings data")
                return False

            # Initialize universal prediction engine
            self.engine = UniversalPredictionEngine(model_dir=self.model_dir)

            # Load model and route data
            success = self.engine.load_model_and_data(
                stops_data_path=str(client_stops_path),
                route_mapping_path=str(route_children_ids_path)
            )

            if not success:
                logger.error("Failed to load universal model. Please train the model first.")
                logger.error("Run: poetry run python -m src.model.cli train-universal --vehicle-positions db/vehicle_positions.csv --stops-data in/client_stops.json")
                return False

            logger.info("Universal model loaded successfully")

            # Get current date for predictions (not Wednesday like in generate_in_files.py)
            current_date = datetime.datetime.now()
            logger.info(f"Generating predictions for {current_date.strftime('%A, %Y-%m-%d')} (day of week: {current_date.weekday()})")

            # Generate predictions for each route
            times = {}
            routename_by_id = {v: k for k, v in route_children_ids.items()}

            for route_name, trip_starts in timings.items():
                if route_name not in route_children_ids:
                    logger.warning(f"Route {route_name} not found in route_children_ids, skipping")
                    continue

                route_id = route_children_ids[route_name]
                times[route_name] = []

                logger.info(f"Processing route {route_name} (ID: {route_id}) with {len(trip_starts)} trips")

                for trip_start_str in trip_starts:
                    # Parse trip start time (HH:MM format)
                    try:
                        hour, minute = map(int, trip_start_str.split(':'))
                    except ValueError:
                        logger.warning(f"Invalid time format for route {route_name}: {trip_start_str}")
                        continue

                    # Create timestamp for current date at this time
                    trip_datetime = current_date.replace(hour=hour, minute=minute, second=0, microsecond=0)
                    trip_start_timestamp = trip_datetime.timestamp()

                    # Predict trip times using universal model
                    predictions = self.engine.predict_trip_from_start(
                        route_id=route_id,
                        start_time=trip_start_timestamp,
                        start_position=None
                    )

                    if not predictions:
                        logger.warning(f"No predictions generated for route {route_name} at {trip_start_str}")
                        continue

                    # Calculate duration (difference between last and first stop)
                    start_timestamp = trip_start_timestamp
                    end_timestamp = predictions[-1]['predicted_arrival_time']
                    duration_seconds = end_timestamp - start_timestamp
                    duration_minutes = duration_seconds / 60

                    # Convert predictions to required format
                    stops_output = []

                    # First stop (start of trip)
                    first_stop = client_stops[route_name]['stops'][0]
                    stops_output.append({
                        'stop_id': first_stop['stop_id'],
                        'stop_loc': first_stop['loc'],
                        'stop_time': int(f"{hour:02d}{minute:02d}"),  # HHMM format
                        'confidence': None
                    })

                    # Remaining stops from predictions
                    for pred in predictions:
                        pred_datetime = datetime.datetime.fromtimestamp(pred['predicted_arrival_time'])
                        stop_time_hhmm = int(f"{pred_datetime.hour:02d}{pred_datetime.minute:02d}")

                        stops_output.append({
                            'stop_id': pred['stop_id'],
                            'stop_loc': [pred['latitude'], pred['longitude']],
                            'stop_time': stop_time_hhmm,
                            'confidence': None  # Universal model doesn't provide confidence scores
                        })

                    # Build output structure
                    output = {
                        'route_id': route_id,
                        'duration': duration_minutes,
                        'start': int(f"{hour:02d}{minute:02d}"),
                        'stops': stops_output
                    }
                    times[route_name].append(output)

                logger.info(f"Generated {len(times[route_name])} trip predictions for route {route_name}")

            # Write updated times.json
            output_path = in_dir / "times.json"
            with open(output_path, 'w') as f:
                json.dump(times, f, indent=4, ensure_ascii=False)

            logger.info(f"Successfully wrote fresh predictions to {output_path}")
            logger.info(f"Predictions generated for {current_date.strftime('%A, %Y-%m-%d')}")
            logger.info(f"Total routes processed: {len(times)}")

            return True

        except Exception as e:
            logger.error(f"Error generating fresh predictions: {e}", exc_info=True)
            return False

    def _load_timings(self, timings_tsv_path: Path, times_json_path: Path) -> Dict[str, list]:
        """
        Load timings from either timings.tsv or extract from times.json.

        Args:
            timings_tsv_path: Path to timings.tsv file
            times_json_path: Path to times.json file (fallback)

        Returns:
            Dictionary mapping route names to list of trip start times (HH:MM format)
        """
        timings = {}

        # Try loading from timings.tsv first
        if timings_tsv_path.exists():
            logger.info(f"Loading timings from {timings_tsv_path}")
            try:
                with open(timings_tsv_path, 'r') as f:
                    f.readline()  # Skip headers
                    for line in f.readlines():
                        parts = line.strip().split('\t')
                        if len(parts) >= 2:
                            route_no = parts[0]
                            time_str = parts[1]
                            timings[route_no] = time_str.split(' ')
                return timings
            except Exception as e:
                logger.warning(f"Error loading timings.tsv: {e}")

        # Fallback to extracting from times.json
        if times_json_path.exists():
            logger.info(f"Loading timings from {times_json_path} (fallback)")
            try:
                with open(times_json_path, 'r') as f:
                    times_data = json.load(f)

                for route_name, trips in times_data.items():
                    if not trips:
                        continue
                    # Extract start times from existing trips
                    start_times = []
                    for trip in trips:
                        start = trip.get('start')
                        if start is not None:
                            # Convert HHMM int to HH:MM string
                            if isinstance(start, int):
                                hour = start // 100
                                minute = start % 100
                                start_times.append(f"{hour:02d}:{minute:02d}")
                    if start_times:
                        timings[route_name] = start_times

                return timings
            except Exception as e:
                logger.warning(f"Error loading times.json: {e}")

        return {}