"""
Data models module for the new architecture.
Exports all data model classes.
"""

from .vehicle import Vehicle, VehiclePosition, StationInfo, RouteInfo
from .trip import Trip, TripStop, TripSchedule

__all__ = [
    "Vehicle",
    "VehiclePosition", 
    "StationInfo",
    "RouteInfo",
    "Trip",
    "TripStop",
    "TripSchedule"
]
