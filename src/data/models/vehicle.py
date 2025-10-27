"""
Vehicle data model for the new architecture.
Immutable data structures representing vehicle information from BMTC API.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple
import pytz

local_tz = pytz.timezone("Asia/Kolkata")


@dataclass(frozen=True)
class VehiclePosition:
    """Immutable vehicle position data model"""
    latitude: float
    longitude: float
    bearing: float
    timestamp: datetime
    
    @classmethod
    def from_api_data(cls, api_data: dict) -> 'VehiclePosition':
        """Create VehiclePosition from BMTC API data"""
        # Parse lastrefreshon timestamp
        timestamp_str = api_data.get("lastrefreshon", "")
        try:
            # Format: "22-09-2025 17:39:25"
            timestamp = datetime.strptime(timestamp_str, '%d-%m-%Y %H:%M:%S')
            timestamp = local_tz.localize(timestamp)
        except (ValueError, TypeError):
            # Fallback to current time if parsing fails
            timestamp = datetime.now(local_tz)
        
        return cls(
            latitude=float(api_data.get("centerlat", 0.0)),
            longitude=float(api_data.get("centerlong", 0.0)),
            bearing=float(api_data.get("heading", 0.0)),
            timestamp=timestamp
        )



@dataclass(frozen=True)
class Vehicle:
    """Immutable vehicle data model"""
    id: str
    number: str  # Vehicle registration number (e.g., "KA57F1771")
    route_id: str
    service_type: str  # "AC" or "NON-AC"
    position: VehiclePosition

    # Schedule information
    scheduled_arrival_time: Optional[str] = None  # HH:MM format
    scheduled_departure_time: Optional[str] = None  # HH:MM format
    actual_arrival_time: Optional[str] = None  # HH:MM format
    actual_departure_time: Optional[str] = None  # HH:MM format
    scheduled_trip_start_time: Optional[str] = None  # HH:MM format

    # Location context
    current_station_id: Optional[str] = None
    stop_covered_status: int = 0  # 0 = not reached, 1 = passed
    trip_position: int = 1  # Position in trip sequence

    # ETA information
    eta: Optional[str] = None  # ISO datetime string or empty
    
    @classmethod
    def from_api_data(cls, api_data: dict, route_id: str, station_id: str) -> 'Vehicle':
        """Create Vehicle from BMTC API vehicle data"""
        position = VehiclePosition.from_api_data(api_data)
        
        return cls(
            id=str(api_data.get("vehicleid", "")),
            number=api_data.get("vehiclenumber", ""),
            route_id=str(route_id),
            service_type=api_data.get("servicetype", ""),
            position=position,
            scheduled_arrival_time=api_data.get("sch_arrivaltime") or None,
            scheduled_departure_time=api_data.get("sch_departuretime") or None,
            actual_arrival_time=api_data.get("actual_arrivaltime") or None,
            actual_departure_time=api_data.get("actual_departuretime") or None,
            scheduled_trip_start_time=api_data.get("sch_tripstarttime") or None,
            current_station_id=str(station_id),
            stop_covered_status=api_data.get("stopCoveredStatus", 0),
            trip_position=api_data.get("tripposition", 1),
            eta=api_data.get("eta") or None
        )
    
    def is_trip_completed(self) -> bool:
        """Check if the vehicle has completed its trip (has actual departure time)"""
        return bool(self.actual_arrival_time and self.actual_departure_time)
    
    def is_trip_active(self) -> bool:
        """Check if the vehicle is currently on an active trip"""
        return bool(self.scheduled_trip_start_time and not self.is_trip_completed())
    
    def get_delay_seconds(self) -> Optional[int]:
        """Calculate delay in seconds compared to scheduled time"""
        if not self.scheduled_arrival_time or not self.actual_arrival_time:
            return None
            
        try:
            from old_src.shared.utils import from_hhmm
            scheduled_timestamp = from_hhmm(self.scheduled_arrival_time) * 60  # Convert to seconds
            actual_timestamp = from_hhmm(self.actual_arrival_time) * 60
            return actual_timestamp - scheduled_timestamp
        except:
            return None


@dataclass(frozen=True)
class StationInfo:
    """Immutable station/stop information"""
    station_id: str
    name: str
    name_kn: str  # Kannada name
    latitude: float
    longitude: float
    distance_on_route: float  # Distance from route start
    phone: str = ""
    
    @classmethod
    def from_api_data(cls, api_data: dict) -> 'StationInfo':
        """Create StationInfo from BMTC API station data"""
        return cls(
            station_id=str(api_data.get("stationid", "")),
            name=api_data.get("stationname", ""),
            name_kn=api_data.get("name_kn", ""),  # May not be in API data
            latitude=float(api_data.get("centerlat", 0.0)),
            longitude=float(api_data.get("centerlong", 0.0)),
            distance_on_route=float(api_data.get("distance_on_station", 0.0)),
            phone=api_data.get("phone", "")
        )
    
    @classmethod
    def from_static_data(cls, stop_data: dict, distance: float = 0.0) -> 'StationInfo':
        """Create StationInfo from static client_stops.json data"""
        return cls(
            station_id=str(stop_data.get("stop_id", "")),
            name=stop_data.get("name", ""),
            name_kn=stop_data.get("name_kn", ""),
            latitude=float(stop_data["loc"][0]) if stop_data.get("loc") else 0.0,
            longitude=float(stop_data["loc"][1]) if stop_data.get("loc") else 0.0,
            distance_on_route=distance,
            phone=stop_data.get("phone", "")
        )


@dataclass(frozen=True)
class RouteInfo:
    """Immutable route information"""
    route_id: str
    route_number: str  # e.g., "KIA-9"
    direction: str  # "UP" or "DOWN"
    from_station: str
    to_station: str
    
    @classmethod
    def from_api_data(cls, api_data: dict) -> 'RouteInfo':
        """Create RouteInfo from BMTC API data"""
        return cls(
            route_id=str(api_data.get("routeid", "")),
            route_number=api_data.get("routeno", ""),
            direction="",  # Need to infer from route name
            from_station=api_data.get("from", ""),
            to_station=api_data.get("to", "")
        )
