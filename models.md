# Stop Time Prediction Models

This document describes the stop time prediction system built for transit route analysis. The system includes both legacy route-specific models and a new universal stop-to-stop model.

## Overview

The system provides two modeling approaches:

1. **Legacy Route-Specific Models**: Individual models trained per route
2. **Universal Stop-to-Stop Model**: Single model that learns stop-to-stop patterns across all routes

## Universal Model (Recommended)

The universal model uses a single machine learning model to predict stop-to-stop travel times across all routes, making it more scalable and efficient.

### Architecture

- **Training Data**: Stop-to-stop pairs from all routes with temporal and geographic features
- **Model**: Random Forest Regressor with 16-dimensional feature vector
- **Features**: Time of day, day of week, rush hours, weekend flags, stop locations, distance, trip context
- **Output**: Travel time between consecutive stops

### Feature Vector (16 dimensions):
```
[start_time, hour_sin, hour_cos, day_sin, day_cos, is_weekend,
 is_morning_rush, is_evening_rush, from_lat, from_lon, to_lat, to_lon,
 distance, sequence_position, trip_progress, route_id_normalized]
```

## Data Requirements

### Expected Data Structure

#### 1. Vehicle Positions CSV (`vehicle_positions.csv`)
```csv
id,vehicle_id,route_id,latitude,longitude,bearing,speed,timestamp,status,_source_db
1,12345,1863,12.9716,77.5946,45.0,25,1756451000,,live_data_local.db
2,12345,1863,12.9720,77.5950,47.0,28,1756451030,,live_data_local.db
```

**Required columns:**
- `vehicle_id`: Unique vehicle identifier
- `route_id`: Numeric route identifier
- `latitude`: Vehicle latitude coordinate
- `longitude`: Vehicle longitude coordinate
- `timestamp`: Unix timestamp
- Other columns are optional

#### 2. Route Stops JSON (`client_stops.json`)
```json
{
  "KIA-9 UP": {
    "stops": [
      {
        "id": "stop_1",
        "name": "Majestic",
        "loc": [12.9762, 77.5723]
      },
      {
        "id": "stop_2",
        "name": "City Railway Station",
        "loc": [12.9759, 77.5773]
      }
    ]
  }
}
```

#### 3. Route Mapping JSON (`routes_children_ids.json`)
```json
{
  "KIA-9 UP": 1863,
  "KIA-9 DOWN": 1864,
  "KIA-4 UP": 2985
}
```

Maps route names to numeric route IDs.

## CLI Usage

### Universal Model Commands

#### Training
```bash
poetry run python -m src.model.cli train-universal \
  --vehicle-positions db/vehicle_positions.csv \
  --stops-data in/client_stops.json \
  --model-dir universal_models
```

**Parameters:**
- `--vehicle-positions`: Path to vehicle positions CSV
- `--stops-data`: Path to stops data JSON
- `--model-dir`: Directory to save model files (default: `universal_models`)

#### Prediction from Start Time
```bash
poetry run python -m src.model.cli predict-from-start \
  --route-id 1863 \
  --start-time 1756451128 \
  --stops-data in/client_stops.json \
  --model-dir universal_models \
  --output predictions.json
```

**Parameters:**
- `--route-id`: Numeric route ID
- `--start-time`: Unix timestamp for trip start
- `--start-position`: Optional "lat,lon" for intermediate start point
- `--stops-data`: Path to stops data JSON
- `--model-dir`: Directory containing trained model
- `--output`: Optional output file for predictions

#### Prediction from Current Position
```bash
poetry run python -m src.model.cli predict-from-position \
  --vehicle-positions current_trip.csv \
  --route-id 1863 \
  --stops-data in/client_stops.json \
  --model-dir universal_models \
  --output predictions.json
```

#### Validation
```bash
poetry run python -m src.model.cli validate-universal \
  --vehicle-positions db/vehicle_positions.csv \
  --stops-data in/client_stops.json \
  --model-dir universal_models \
  --train-split 0.7 \
  --output validation_results.json
```

**Parameters:**
- `--vehicle-positions`: Path to vehicle positions CSV for validation
- `--stops-data`: Path to stops data JSON
- `--model-dir`: Directory containing trained universal model
- `--train-split`: Train/test split ratio (default: 0.7)
- `--output`: Optional output file for validation metrics

### Legacy Model Commands

#### Training
```bash
poetry run python -m src.model.cli train \
  --vehicle-positions db/vehicle_positions.csv \
  --stops-data in/client_stops.json \
  --model-dir models
```

#### Prediction
```bash
poetry run python -m src.model.cli predict \
  --vehicle-positions incomplete_trip.csv \
  --route-id 1863 \
  --stops-data in/client_stops.json \
  --model-dir models
```

#### Validation
```bash
poetry run python -m src.model.cli validate \
  --vehicle-positions db/vehicle_positions.csv \
  --stops-data in/client_stops.json \
  --model-dir models \
  --output validation_results.json
```

## Python API Usage

### Universal Model Training

```python
from src.model.universal_training import UniversalStopTimeModel

# Initialize model
model = UniversalStopTimeModel(model_dir="universal_models")

# Train model
model.train(
    vehicle_positions_path="db/vehicle_positions.csv",
    stops_data_path="in/client_stops.json"
)

# Save model
model.save_model()
```

### Universal Model Prediction

```python
from src.model.universal_prediction import UniversalPredictionEngine
import time

# Initialize prediction engine
engine = UniversalPredictionEngine(model_dir="universal_models")

# Load model and data
success = engine.load_model_and_data(
    stops_data_path="in/client_stops.json"
)

if success:
    # Predict full trip from start time
    predictions = engine.predict_trip_from_start(
        route_id=1863,
        start_time=time.time(),  # Current time
        start_position=None      # Optional: {"latitude": 12.97, "longitude": 77.59}
    )

    # Predict from current vehicle positions
    import pandas as pd
    positions_df = pd.read_csv("current_positions.csv")
    predictions = engine.predict_from_current_position(
        positions_df=positions_df,
        route_id=1863
    )
```

### Data Processing Classes

```python
from src.model.stop_to_stop_data import StopToStopDataProcessor

# Process stop-to-stop training data
processor = StopToStopDataProcessor(
    vehicle_positions_path="db/vehicle_positions.csv",
    stops_data_path="in/client_stops.json"
)

# Get all stop-to-stop pairs
stop_pairs = processor.process_all_routes()

# Extract features for a stop pair
features = processor.extract_features(stop_pairs[0])
```

## Model Output Format

### Prediction Output
```json
[
  {
    "stop_id": "stop_1",
    "stop_name": "Majestic",
    "predicted_arrival_time": 1756451307.52,
    "predicted_departure_time": 1756451307.52,
    "latitude": 12.9762,
    "longitude": 77.5723,
    "sequence": 1,
    "predicted_travel_time": 179.5
  }
]
```

### Validation Output
```json
{
  "1863": {
    "route_id": 1863,
    "predictions_made": 150,
    "mean_absolute_error": 45.3,
    "std_error": 23.1,
    "median_error": 38.7,
    "mean_relative_error": 0.12,
    "accuracy_within_60s": 0.78,
    "accuracy_within_120s": 0.91,
    "accuracy_within_300s": 0.98
  }
}
```

## Model Files

### Universal Model Files
- `universal_model.pkl`: Trained Random Forest model
- `universal_scaler.pkl`: Feature scaler for preprocessing

### Legacy Model Files (per route)
- `model_route_{route_id}.pkl`: Route-specific model
- `scaler_route_{route_id}.pkl`: Route-specific feature scaler

## Performance Characteristics

### Universal Model Advantages
- **Single Model**: One model serves all routes
- **Scalable**: Works with any route that has stop sequence data
- **Flexible Start Points**: Can predict from any stop in the route
- **Traffic-Aware**: Incorporates temporal patterns (rush hours, weekends)
- **Reduced Storage**: ~500KB vs 35+ models × 300KB each

### Training Data Requirements
- **Minimum**: 100+ complete trips across routes
- **Recommended**: 1000+ complete trips for robust predictions
- **Stop Coverage**: Each route should have clear stop sequence data
- **Time Coverage**: Data spanning different times of day and days of week

## Implementation Notes

### Trip Identification Logic
The system identifies complete trips using:
1. Vehicle positions near first and last stops of routes
2. Time constraints (max 6 hours between start/end)
3. Temporal ordering validation
4. Minimum trip length requirements (>5 position points)

### Feature Engineering
- **Temporal Features**: Cyclic encoding for time/day to handle wraparound
- **Geographic Features**: Haversine distance between stops
- **Context Features**: Trip progress, route position, route characteristics
- **Traffic Features**: Rush hour indicators, weekend flags

### Error Handling
- Invalid timestamps are filtered out
- Routes without sufficient data are skipped
- Malformed position data is handled gracefully
- Missing route mappings are reported but don't crash processing