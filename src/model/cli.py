import argparse
import sys
import os
import pandas as pd
import json
from .training import StopTimePredictionModel
from .prediction import StopTimePredictionEngine
from .validation import ModelValidator
from .universal_training import UniversalStopTimeModel
from .universal_prediction import UniversalPredictionEngine
from .universal_validation import UniversalModelValidator


def train_models(args):
    """Train stop time prediction models"""
    print("Starting model training...")

    model = StopTimePredictionModel(model_dir=args.model_dir)

    # Prepare training data
    print("Preparing training data...")
    training_data = model.prepare_training_data(args.vehicle_positions, args.stops_data)

    print(f"Found training data for {len(training_data)} routes")

    # Train models for each route
    for route_id, route_data in training_data.items():
        model.train_route_model(route_id, route_data)

    # Save models
    model.save_models()
    print("Training completed!")


def predict_stops(args):
    """Predict remaining stop times"""
    print("Starting prediction...")

    engine = StopTimePredictionEngine(model_dir=args.model_dir)
    engine.load_models([args.route_id])

    # Load incomplete trip data
    if args.vehicle_positions.endswith('.csv'):
        positions_df = pd.read_csv(args.vehicle_positions)
    else:
        print("Vehicle positions must be a CSV file")
        return

    # Make predictions
    predictions = engine.predict_remaining_stop_times(
        positions_df, args.route_id, args.stops_data
    )

    if predictions:
        print(f"Predicted {len(predictions)} remaining stops:")
        for pred in predictions:
            print(f"  Stop {pred['sequence']}: {pred['stop_name']} at {pred['predicted_arrival_time']}")

        # Save predictions if output file specified
        if args.output:
            with open(args.output, 'w') as f:
                json.dump(predictions, f, indent=2)
            print(f"Predictions saved to {args.output}")
    else:
        print("No predictions generated")


def validate_models(args):
    """Validate model performance"""
    print("Starting model validation...")

    validator = ModelValidator(model_dir=args.model_dir)

    # Prepare validation data
    print("Preparing validation data...")
    validation_data = validator.prepare_validation_data(args.vehicle_positions, args.stops_data)

    print(f"Found validation data for {len(validation_data)} routes")

    all_results = {}

    # Validate each route
    for route_id, route_data in validation_data.items():
        if len(route_data) < 3:  # Need minimum trips for validation
            print(f"Skipping route {route_id} - insufficient data")
            continue

        result = validator.validate_route(route_id, route_data, args.stops_data)
        if result and 'error' not in result:
            all_results[route_id] = result

            print(f"\nRoute {route_id} Results:")
            print(f"  Predictions made: {result['predictions_made']}")
            print(f"  Mean absolute error: {result['mean_absolute_error']:.2f} seconds")
            print(f"  Median error: {result['median_error']:.2f} seconds")
            print(f"  Accuracy within 60s: {result['accuracy_within_60s']:.2%}")
            print(f"  Accuracy within 120s: {result['accuracy_within_120s']:.2%}")
            print(f"  Accuracy within 300s: {result['accuracy_within_300s']:.2%}")

    # Save results
    if args.output and all_results:
        with open(args.output, 'w') as f:
            json.dump(all_results, f, indent=2)
        print(f"\nValidation results saved to {args.output}")

    print("\nValidation completed!")


def train_universal_model(args):
    """Train universal stop-to-stop model"""
    print("Starting universal model training...")

    model = UniversalStopTimeModel(model_dir=args.model_dir)
    model.train(args.vehicle_positions, args.stops_data)
    model.save_model()

    print("Universal model training completed!")


def predict_universal_from_start(args):
    """Predict trip from start time using universal model"""
    print("Starting universal prediction from start...")

    engine = UniversalPredictionEngine(model_dir=args.model_dir)
    success = engine.load_model_and_data(args.stops_data)

    if not success:
        print("Failed to load universal model")
        return

    # Parse start position if provided
    start_position = None
    if args.start_position:
        lat, lon = map(float, args.start_position.split(','))
        start_position = {'latitude': lat, 'longitude': lon}

    predictions = engine.predict_trip_from_start(
        args.route_id, args.start_time, start_position
    )

    if predictions:
        print(f"Predicted {len(predictions)} stops:")
        for pred in predictions:
            print(f"  Stop {pred['sequence']}: {pred['stop_name']} at {pred['predicted_arrival_time']}")

        if args.output:
            with open(args.output, 'w') as f:
                json.dump(predictions, f, indent=2)
            print(f"Predictions saved to {args.output}")
    else:
        print("No predictions generated")


def predict_universal_from_position(args):
    """Predict remaining stops from current position using universal model"""
    print("Starting universal prediction from current position...")

    engine = UniversalPredictionEngine(model_dir=args.model_dir)
    success = engine.load_model_and_data(args.stops_data)

    if not success:
        print("Failed to load universal model")
        return

    # Load current positions
    positions_df = pd.read_csv(args.vehicle_positions)

    predictions = engine.predict_from_current_position(positions_df, args.route_id)

    if predictions:
        print(f"Predicted {len(predictions)} remaining stops:")
        for pred in predictions:
            print(f"  Stop {pred['sequence']}: {pred['stop_name']} at {pred['predicted_arrival_time']}")

        if args.output:
            with open(args.output, 'w') as f:
                json.dump(predictions, f, indent=2)
            print(f"Predictions saved to {args.output}")
    else:
        print("No predictions generated")


def validate_universal_model(args):
    """Validate universal model performance"""
    print("Starting universal model validation...")

    validator = UniversalModelValidator(model_dir=args.model_dir)

    # Validate model
    results = validator.validate_model(
        args.vehicle_positions,
        args.stops_data,
        train_split=args.train_split
    )

    if 'error' in results:
        print(f"Validation failed: {results['error']}")
        return

    print(f"\nUniversal Model Validation Results:")
    print(f"  Samples tested: {results['samples_tested']}")
    print(f"  Mean absolute error: {results['mean_absolute_error']:.2f} seconds")
    print(f"  Median error: {results['median_error']:.2f} seconds")
    print(f"  Standard deviation: {results['std_error']:.2f} seconds")
    print(f"  Mean relative error: {results['mean_relative_error']:.2%}")
    print(f"  R² score: {results['r2_score']:.3f}")
    print(f"  Accuracy within 30s: {results['accuracy_within_30s']:.2%}")
    print(f"  Accuracy within 60s: {results['accuracy_within_60s']:.2%}")
    print(f"  Accuracy within 120s: {results['accuracy_within_120s']:.2%}")
    print(f"  Accuracy within 300s: {results['accuracy_within_300s']:.2%}")

    # Save results if output file specified
    if args.output:
        with open(args.output, 'w') as f:
            json.dump(results, f, indent=2)
        print(f"\nValidation results saved to {args.output}")

    print("\nUniversal model validation completed!")


def main():
    parser = argparse.ArgumentParser(description='Stop Time Prediction Model CLI')
    subparsers = parser.add_subparsers(dest='command', help='Available commands')

    # Training command
    train_parser = subparsers.add_parser('train', help='Train models')
    train_parser.add_argument('--vehicle-positions', required=True,
                             help='Path to vehicle positions CSV file')
    train_parser.add_argument('--stops-data', required=True,
                             help='Path to stops data JSON file')
    train_parser.add_argument('--model-dir', default='models',
                             help='Directory to save models (default: models)')

    # Prediction command
    predict_parser = subparsers.add_parser('predict', help='Predict remaining stop times')
    predict_parser.add_argument('--vehicle-positions', required=True,
                               help='Path to incomplete trip vehicle positions CSV file')
    predict_parser.add_argument('--route-id', required=True, type=int,
                               help='Route ID for prediction')
    predict_parser.add_argument('--stops-data', required=True,
                               help='Path to stops data JSON file')
    predict_parser.add_argument('--model-dir', default='models',
                               help='Directory containing models (default: models)')
    predict_parser.add_argument('--output', help='Output file for predictions (JSON)')

    # Validation command
    validate_parser = subparsers.add_parser('validate', help='Validate model performance')
    validate_parser.add_argument('--vehicle-positions', required=True,
                                help='Path to vehicle positions CSV file for validation')
    validate_parser.add_argument('--stops-data', required=True,
                                help='Path to stops data JSON file')
    validate_parser.add_argument('--model-dir', default='models',
                                help='Directory containing models (default: models)')
    validate_parser.add_argument('--output', help='Output file for validation results (JSON)')

    # Universal model commands
    train_universal_parser = subparsers.add_parser('train-universal', help='Train universal stop-to-stop model')
    train_universal_parser.add_argument('--vehicle-positions', required=True,
                                       help='Path to vehicle positions CSV file')
    train_universal_parser.add_argument('--stops-data', required=True,
                                       help='Path to stops data JSON file')
    train_universal_parser.add_argument('--model-dir', default='universal_models',
                                       help='Directory to save universal model (default: universal_models)')

    predict_start_parser = subparsers.add_parser('predict-from-start', help='Predict trip from start time')
    predict_start_parser.add_argument('--route-id', required=True, type=int,
                                     help='Route ID for prediction')
    predict_start_parser.add_argument('--start-time', required=True, type=float,
                                     help='Start time (Unix timestamp)')
    predict_start_parser.add_argument('--start-position',
                                     help='Start position as "lat,lon" (optional)')
    predict_start_parser.add_argument('--stops-data', required=True,
                                     help='Path to stops data JSON file')
    predict_start_parser.add_argument('--model-dir', default='universal_models',
                                     help='Directory containing universal model')
    predict_start_parser.add_argument('--output', help='Output file for predictions (JSON)')

    predict_position_parser = subparsers.add_parser('predict-from-position', help='Predict from current position')
    predict_position_parser.add_argument('--vehicle-positions', required=True,
                                        help='Path to current vehicle positions CSV file')
    predict_position_parser.add_argument('--route-id', required=True, type=int,
                                        help='Route ID for prediction')
    predict_position_parser.add_argument('--stops-data', required=True,
                                        help='Path to stops data JSON file')
    predict_position_parser.add_argument('--model-dir', default='universal_models',
                                        help='Directory containing universal model')
    predict_position_parser.add_argument('--output', help='Output file for predictions (JSON)')

    validate_universal_parser = subparsers.add_parser('validate-universal', help='Validate universal model performance')
    validate_universal_parser.add_argument('--vehicle-positions', required=True,
                                          help='Path to vehicle positions CSV file for validation')
    validate_universal_parser.add_argument('--stops-data', required=True,
                                          help='Path to stops data JSON file')
    validate_universal_parser.add_argument('--model-dir', default='universal_models',
                                          help='Directory containing universal model')
    validate_universal_parser.add_argument('--train-split', default=0.7, type=float,
                                          help='Train/test split ratio (default: 0.7)')
    validate_universal_parser.add_argument('--output', help='Output file for validation results (JSON)')

    args = parser.parse_args()

    if args.command == 'train':
        train_models(args)
    elif args.command == 'predict':
        predict_stops(args)
    elif args.command == 'validate':
        validate_models(args)
    elif args.command == 'train-universal':
        train_universal_model(args)
    elif args.command == 'predict-from-start':
        predict_universal_from_start(args)
    elif args.command == 'predict-from-position':
        predict_universal_from_position(args)
    elif args.command == 'validate-universal':
        validate_universal_model(args)
    else:
        parser.print_help()


if __name__ == '__main__':
    main()