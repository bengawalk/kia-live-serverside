"""
Data validation module based on old transformer illegal state handling.
Implements the validation logic from live_data_transformer.py.
"""

from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any, Tuple
import pytz

from .models.vehicle import Vehicle
from .models.trip import Trip, TripStop

local_tz = pytz.timezone("Asia/Kolkata")


class DataValidationError(Exception):
    """Exception raised for data validation errors"""
    pass


class TripStateValidator:
    """Validates trip states to prevent illegal configurations"""
    
    @staticmethod
    def validate_trip_timing(trip: Trip) -> List[str]:
        """
        Validate trip timing consistency.
        Returns list of validation errors (empty if valid).
        
        Based on old transformer logic:
        - Stop times must progress forward
        - No stops can have same time (except arrival/departure at same stop)
        - Future stops cannot be in the past (with tolerance)
        """
        errors = []
        now = datetime.now(local_tz)
        tolerance_minutes = 5  # Allow 5 minutes tolerance for past times
        
        if not trip.stops:
            return errors
        
        # Check time progression across stops
        prev_time = None
        
        for i, stop in enumerate(trip.stops):
            # Get effective time for this stop
            current_time = stop.get_effective_departure() or stop.get_effective_arrival()
            
            if current_time:
                # Check forward progression
                if prev_time and current_time <= prev_time:
                    errors.append(
                        f"Stop {i+1} ({stop.stop_id}) time {current_time} is not after "
                        f"previous stop time {prev_time}"
                    )
                
                # Check if future stops are in the past (illegal state from old transformer)
                if not stop.is_completed() and current_time < now:
                    time_diff = (now - current_time).total_seconds() / 60  # minutes
                    if time_diff > tolerance_minutes:
                        errors.append(
                            f"Future stop {stop.stop_id} has time {time_diff:.1f} minutes in the past"
                        )
                
                prev_time = current_time
            
            # Check arrival/departure order within same stop
            if stop.actual_arrival and stop.actual_departure:
                if stop.actual_arrival > stop.actual_departure:
                    errors.append(
                        f"Stop {stop.stop_id} arrival time {stop.actual_arrival} "
                        f"is after departure time {stop.actual_departure}"
                    )
        
        return errors
    
    @staticmethod
    def validate_vehicle_trip_assignment(vehicle: Vehicle, trip: Trip) -> List[str]:
        """
        Validate vehicle-trip assignment consistency.
        Based on old transformer matching logic.
        """
        errors = []
        
        # Check route consistency
        if vehicle.route_id != trip.route_id:
            errors.append(
                f"Vehicle route {vehicle.route_id} doesn't match trip route {trip.route_id}"
            )
        
        # Check if vehicle is at a valid stop for this trip
        if vehicle.current_station_id:
            trip_stop_ids = {stop.stop_id for stop in trip.stops}
            if vehicle.current_station_id not in trip_stop_ids:
                errors.append(
                    f"Vehicle at station {vehicle.current_station_id} which is not in trip stops"
                )
        
        # Check timing consistency (if vehicle has trip start time)
        if vehicle.scheduled_trip_start_time and trip.start_time:
            try:
                # Parse vehicle trip start time
                vehicle_start = TripStateValidator._parse_hhmm_to_datetime(
                    vehicle.scheduled_trip_start_time
                )
                
                # Allow 2-minute tolerance for trip matching (from old transformer)
                time_diff = abs((vehicle_start - trip.start_time).total_seconds() / 60)
                if time_diff > 2:
                    errors.append(
                        f"Vehicle trip start time {vehicle.scheduled_trip_start_time} "
                        f"differs from trip start by {time_diff:.1f} minutes"
                    )
                    
            except Exception as e:
                errors.append(f"Invalid vehicle trip start time format: {e}")
        
        return errors
    
    @staticmethod
    def validate_stop_time_consistency(stop: TripStop) -> List[str]:
        """
        Validate individual stop time consistency.
        Based on old transformer time validation.
        """
        errors = []
        
        # Check scheduled time consistency
        if stop.scheduled_arrival and stop.scheduled_departure:
            if stop.scheduled_arrival > stop.scheduled_departure:
                errors.append(
                    f"Stop {stop.stop_id} scheduled arrival after departure"
                )
        
        # Check actual time consistency
        if stop.actual_arrival and stop.actual_departure:
            if stop.actual_arrival > stop.actual_departure:
                errors.append(
                    f"Stop {stop.stop_id} actual arrival after departure"
                )
        
        # Check predicted time consistency
        if stop.predicted_arrival and stop.predicted_departure:
            if stop.predicted_arrival > stop.predicted_departure:
                errors.append(
                    f"Stop {stop.stop_id} predicted arrival after departure"
                )
        
        # Check that actual times are reasonable compared to scheduled
        if stop.scheduled_arrival and stop.actual_arrival:
            delay_minutes = (stop.actual_arrival - stop.scheduled_arrival).total_seconds() / 60
            # Flag extreme delays (more than 60 minutes early or 120 minutes late)
            if delay_minutes < -60:
                errors.append(
                    f"Stop {stop.stop_id} actual arrival {delay_minutes:.1f} minutes too early"
                )
            elif delay_minutes > 120:
                errors.append(
                    f"Stop {stop.stop_id} actual arrival {delay_minutes:.1f} minutes too late"
                )
        
        return errors
    
    @staticmethod
    def validate_trip_completeness(trip: Trip) -> List[str]:
        """
        Validate trip data completeness.
        Check for missing required data based on old transformer requirements.
        """
        errors = []
        
        if not trip.id:
            errors.append("Trip missing ID")
        
        if not trip.route_id:
            errors.append("Trip missing route ID")
        
        if not trip.stops:
            errors.append("Trip has no stops")
        
        # Check stop sequence consistency
        expected_sequence = 1
        for stop in trip.stops:
            if stop.sequence != expected_sequence:
                errors.append(
                    f"Stop sequence gap: expected {expected_sequence}, got {stop.sequence}"
                )
            expected_sequence += 1
        
        # Check that we have timing information
        has_timing = any(
            stop.scheduled_arrival or stop.actual_arrival or stop.predicted_arrival
            for stop in trip.stops
        )
        
        if not has_timing:
            errors.append("Trip has no timing information for any stop")
        
        return errors
    
    @staticmethod
    def _parse_hhmm_to_datetime(time_str: str) -> datetime:
        """Parse HH:MM format to datetime (today)"""
        if not time_str or ":" not in time_str:
            raise ValueError(f"Invalid time format: {time_str}")
        
        try:
            hh, mm = map(int, time_str.split(":"))
            now = datetime.now(local_tz)
            parsed_time = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            
            # Handle midnight crossings (from old transformer logic)
            if parsed_time < now - timedelta(hours=6):
                parsed_time += timedelta(days=1)
            elif parsed_time > now + timedelta(hours=18):
                parsed_time -= timedelta(days=1)
            
            return parsed_time
            
        except (ValueError, TypeError) as e:
            raise ValueError(f"Cannot parse time '{time_str}': {e}")


class VehicleStateValidator:
    """Validates vehicle states for consistency"""
    
    @staticmethod
    def validate_vehicle_position(vehicle: Vehicle) -> List[str]:
        """Validate vehicle position data"""
        errors = []
        
        pos = vehicle.position
        
        # Check coordinate validity (Bangalore area bounds)
        if not (12.0 <= pos.latitude <= 14.0):
            errors.append(f"Vehicle latitude {pos.latitude} outside Bangalore bounds")
        
        if not (77.0 <= pos.longitude <= 78.5):
            errors.append(f"Vehicle longitude {pos.longitude} outside Bangalore bounds")
        
        # Check timestamp validity
        now = datetime.now(local_tz)
        if pos.timestamp:
            age_minutes = (now - pos.timestamp).total_seconds() / 60
            
            # Flag very old positions (more than 30 minutes)
            if age_minutes > 30:
                errors.append(f"Vehicle position is {age_minutes:.1f} minutes old")
            
            # Flag future timestamps
            if pos.timestamp > now + timedelta(minutes=5):
                errors.append(f"Vehicle position timestamp is in the future")
        
        return errors
    
    @staticmethod
    def validate_vehicle_completeness(vehicle: Vehicle) -> List[str]:
        """Validate vehicle data completeness"""
        errors = []
        
        if not vehicle.id:
            errors.append("Vehicle missing ID")
        
        if not vehicle.number:
            errors.append("Vehicle missing number")
        
        if not vehicle.route_id:
            errors.append("Vehicle missing route ID")
        
        # Position is required
        if not vehicle.position:
            errors.append("Vehicle missing position data")
        
        return errors


class DataValidator:
    """Main data validation coordinator"""
    
    def __init__(self):
        self.trip_validator = TripStateValidator()
        self.vehicle_validator = VehicleStateValidator()
    
    def validate_trip(self, trip: Trip) -> Dict[str, List[str]]:
        """Comprehensive trip validation"""
        return {
            'timing': self.trip_validator.validate_trip_timing(trip),
            'completeness': self.trip_validator.validate_trip_completeness(trip),
            'stops': [
                self.trip_validator.validate_stop_time_consistency(stop)
                for stop in trip.stops
            ]
        }
    
    def validate_vehicle(self, vehicle: Vehicle) -> Dict[str, List[str]]:
        """Comprehensive vehicle validation"""
        return {
            'position': self.vehicle_validator.validate_vehicle_position(vehicle),
            'completeness': self.vehicle_validator.validate_vehicle_completeness(vehicle)
        }
    
    def validate_assignment(self, vehicle: Vehicle, trip: Trip) -> List[str]:
        """Validate vehicle-trip assignment"""
        return self.trip_validator.validate_vehicle_trip_assignment(vehicle, trip)
    
    def is_valid_trip(self, trip: Trip) -> bool:
        """Quick check if trip is valid (no critical errors)"""
        validation = self.validate_trip(trip)
        
        # Check for critical errors
        critical_errors = validation['timing'] + validation['completeness']
        return len(critical_errors) == 0
    
    def is_valid_vehicle(self, vehicle: Vehicle) -> bool:
        """Quick check if vehicle is valid (no critical errors)"""
        validation = self.validate_vehicle(vehicle)
        
        # Check for critical errors
        critical_errors = validation['completeness']
        return len(critical_errors) == 0
    
    def filter_valid_trips(self, trips: List[Trip]) -> Tuple[List[Trip], List[Tuple[Trip, Dict]]]:
        """Filter trips, returning valid trips and invalid trips with errors"""
        valid_trips = []
        invalid_trips = []
        
        for trip in trips:
            if self.is_valid_trip(trip):
                valid_trips.append(trip)
            else:
                validation = self.validate_trip(trip)
                invalid_trips.append((trip, validation))
        
        return valid_trips, invalid_trips
    
    def filter_valid_vehicles(self, vehicles: List[Vehicle]) -> Tuple[List[Vehicle], List[Tuple[Vehicle, Dict]]]:
        """Filter vehicles, returning valid vehicles and invalid vehicles with errors"""
        valid_vehicles = []
        invalid_vehicles = []
        
        for vehicle in vehicles:
            if self.is_valid_vehicle(vehicle):
                valid_vehicles.append(vehicle)
            else:
                validation = self.validate_vehicle(vehicle)
                invalid_vehicles.append((vehicle, validation))
        
        return valid_vehicles, invalid_vehicles
