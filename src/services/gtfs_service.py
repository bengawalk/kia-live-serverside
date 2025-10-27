"""
GTFS static generation service for the new architecture.
Converts input data to standard GTFS format using streaming patterns.
"""

import hashlib
import json
import zipfile
import io
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Any, Optional
from urllib.parse import unquote

from src.core.config import ApplicationConfig
from src.data.models.trip import TripSchedule, Trip, TripStop
from src.data.models.vehicle import StationInfo
from src.utils.memory_utils import BoundedCache


class GTFSService:
    """Generate GTFS static feeds from input data with memory management"""
    
    def __init__(self, config: ApplicationConfig):
        self.config = config
        self.feed_cache = BoundedCache(ttl_seconds=3600, max_size_mb=20)  # 1 hour cache
        
    async def generate_gtfs_dataset(self, input_data: Dict[str, Any]) -> Dict[str, List[Dict]]:
        """
        Generate GTFS dataset from input data.
        Returns a dictionary of GTFS files: { 'agency.txt': [...], ... }
        """
        cache_key = self._get_cache_key(input_data)
        if cache_key in self.feed_cache.__dict__:
            return self.feed_cache.__dict__[cache_key]
        
        client_stops = input_data["client_stops"]
        routes_children = input_data["routes_children"]
        routes_parent = input_data["routes_parent"]
        start_times = input_data["start_times"]
        routelines = input_data["routelines"]
        times = input_data["times"]

        # Build GTFS components
        all_stops, stop_id_map, translations = self._build_stops(client_stops)
        agency = self._build_agency()
        feed_info = self._build_feed_info()
        calendar = self._build_calendar()
        routes, route_translations = self._build_routes(client_stops, routes_children)
        shapes, routes_shapes_map = self._build_shapes(routelines, routes_children)
        trips, stop_times, trip_translations = self._build_trips_and_stop_times(
            client_stops, start_times, times, routes_children, stop_id_map, routes_shapes_map
        )

        translations.extend(route_translations)
        translations.extend(trip_translations)

        dataset = {
            "agency.txt": agency,
            "feed_info.txt": feed_info,
            "calendar.txt": calendar,
            "routes.txt": routes,
            "shapes.txt": shapes,
            "stops.txt": all_stops,
            "trips.txt": trips,
            "stop_times.txt": stop_times,
            "translations.txt": translations,
        }
        
        # Cache the result
        self.feed_cache.set(cache_key, dataset)
        return dataset
    
    async def generate_gtfs_zip(self, input_data: Dict[str, Any]) -> bytes:
        """Generate GTFS zip file as bytes"""
        dataset = await self.generate_gtfs_dataset(input_data)
        
        # Create zip file in memory
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            for filename, records in dataset.items():
                if not records:
                    continue
                    
                # Convert records to CSV format
                csv_content = self._records_to_csv(records)
                zip_file.writestr(filename, csv_content)
        
        zip_buffer.seek(0)
        return zip_buffer.getvalue()

    def _get_cache_key(self, input_data: Dict[str, Any]) -> str:
        """Generate cache key for input data"""
        # Create hash from input data structure
        data_str = json.dumps(input_data, sort_keys=True, default=str)
        return hashlib.md5(data_str.encode()).hexdigest()

    def _build_agency(self) -> List[Dict[str, str]]:
        """Build GTFS agency.txt data"""
        return [{
            "agency_id": "BMTC",
            "agency_name": "Bengaluru Metropolitan Transport Corporation",
            "agency_url": "https://mybmtc.karnataka.gov.in/",
            "agency_timezone": "Asia/Kolkata",
            "agency_phone": "7760991269",
            "agency_fare_url": "https://nammabmtcapp.karnataka.gov.in/commuter/fare-calculator"
        }]

    def _build_feed_info(self) -> List[Dict[str, str]]:
        """Build GTFS feed_info.txt data"""
        now = datetime.now()
        return [{
            "feed_publisher_name": "Bengawalk",
            "feed_publisher_url": "https://bengawalk.com/",
            "feed_contact_email": "hello@bengawalk.com",
            "feed_lang": "en",
            "feed_version": hashlib.md5(str(datetime.now()).encode()).hexdigest()[:8],
            "feed_start_date": now.strftime("%Y%m%d"),
            "feed_end_date": (now + timedelta(days=365)).strftime("%Y%m%d")
        }]

    def _build_calendar(self) -> List[Dict[str, str]]:
        """Build GTFS calendar.txt data"""
        now = datetime.now()
        return [{
            "service_id": "ALL",
            "monday": '1', "tuesday": '1', "wednesday": '1', "thursday": '1',
            "friday": '1', "saturday": '1', "sunday": '1',
            "start_date": now.strftime("%Y%m%d"),
            "end_date": (now + timedelta(days=365)).strftime("%Y%m%d")
        }]

    def _build_routes(self, client_stops: Dict, routes_children: Dict) -> tuple[List[Dict], List[Dict]]:
        """Build GTFS routes.txt data and translations"""
        routes = []
        translations = []

        for key, route_id in routes_children.items():
            route_short = key.replace(" UP", "").replace(" DOWN", "")
            if key not in client_stops:
                continue
                
            stops = client_stops[key]["stops"]
            route_long = f"{stops[0]['name']} to {stops[-1]['name']}"
            # Only create translation if both stop names have Kannada translations
            first_stop_kn = stops[0].get('name_kn', '').strip()
            last_stop_kn = stops[-1].get('name_kn', '').strip()

            routes.append({
                "route_id": str(route_id),
                "route_short_name": route_short,
                "route_long_name": route_long,
                "route_type": '3',  # Bus
                "agency_id": "BMTC"
            })

            # Only add translation if both Kannada names exist
            if first_stop_kn and last_stop_kn:
                route_long_kn = f"{first_stop_kn} ಇಂದ {last_stop_kn} ಇಗೆ"
                translations.append({
                    "table_name": "routes",
                    "field_name": "route_long_name",
                    "record_id": str(route_id),
                    "language": "kn",
                    "translation": route_long_kn
                })

        return routes, translations

    def _build_shapes(self, routelines: Dict, routes_children: Dict) -> tuple[List[Dict], Dict]:
        """Build GTFS shapes.txt data and route mapping"""
        shapes = []
        routes_shapes_map = {}
        
        for key, polyline in routelines.items():
            if key not in routes_children:
                continue
                
            shape_id = f"sh_{routes_children[key]}"
            routes_shapes_map[routes_children[key]] = shape_id
            
            # Decode polyline - proper implementation
            points = self._decode_polyline(unquote(polyline, encoding='utf-8', errors='replace'))
            
            for i, (lon, lat) in enumerate(points):
                shapes.append({
                    "shape_id": shape_id,
                    "shape_pt_lat": str(lat),
                    "shape_pt_lon": str(lon),
                    "shape_pt_sequence": str(i + 1)
                })
                
        return shapes, routes_shapes_map
    
    def _build_stops(self, client_stops: Dict) -> tuple[List[Dict], Dict, List[Dict]]:
        """Build GTFS stops.txt data, stop ID mapping, and translations"""
        seen = {}
        stops = []
        translations = {}
        stop_id_map = {}  # (lat, lon, name) → stop_id
        appended = set()

        for route_data in client_stops.values():
            for stop in route_data["stops"]:
                key = (round(stop["loc"][0], 6), round(stop["loc"][1], 6), stop["name"]) if not 'stop_id' in stop else stop['stop_id']
                if key in seen:
                    if "stop_id" in stop:
                        stop_id_map[key] = stop["stop_id"]
                    continue

                stop_id = stop.get("stop_id") or f"gen_{len(stops) + 1}"
                stop_id_map[key] = stop_id
                if stop_id in appended:
                    continue
                seen[key] = True

                stops.append({
                    "stop_id": str(stop_id),
                    "stop_name": stop["name"],
                    "stop_lat": str(stop["loc"][0]),
                    "stop_lon": str(stop["loc"][1]),
                })

                # Only add translation if name_kn exists and is not empty
                if stop.get("name_kn") and stop["name_kn"].strip():
                    translations[stop_id] = {
                        "table_name": "stops",
                        "field_name": "stop_name",
                        "record_id": str(stop_id),
                        "language": "kn",
                        "translation": stop["name_kn"]
                    }
                appended.add(stop_id)

        return stops, stop_id_map, list(translations.values())
    
    def _build_trips_and_stop_times(self, client_stops: Dict, start_times: Dict, times_data: Dict, 
                                   routes_children: Dict, stop_id_map: Dict, routes_shapes_map: Dict) -> tuple[List[Dict], List[Dict], List[Dict]]:
        """Build GTFS trips.txt and stop_times.txt data"""
        # print("BUILDING STOP TIMES AND TRIPS")
        trips = []
        stop_times = []
        translations = []

        for route_key, route_id in routes_children.items():
            # print("PROCESSING ROUTE", route_key)
            if route_key not in client_stops:
                continue
                
            stops = client_stops[route_key]["stops"]
            stop_points = [
                (
                    stop_id_map[(round(s["loc"][0], 6), round(s["loc"][1], 6), s["name"])] if 'stop_id' not in s else s['stop_id'],
                    s["distance"],
                    s["name"]
                )
                for s in stops
            ]
            stop_points.sort(key=lambda x: x[1])  # sort by distance
            route_trips = times_data.get(route_key) or []
            fallback_trips = start_times.get(route_key) or []

            if not route_trips:
                print("TRYING USING FALLBACK TRIPS")
                route_trips = [
                    {"start": t["start"], "stops": None, "duration": t["duration"]}
                    for t in fallback_trips
                ]

            used_ids = set()
            for i, trip_data in enumerate(route_trips):
                trip_start = trip_data["start"]
                trip_duration = trip_data.get("duration") or (fallback_trips[i]["duration"] if i < len(fallback_trips) else 60)
                
                trip_index = 1
                while f"{route_id}_{trip_index}" in used_ids:
                    trip_index += 1
                trip_id = f"{route_id}_{trip_index}"
                used_ids.add(trip_id)

                trips.append({
                    "trip_id": trip_id,
                    "route_id": str(route_id),
                    "shape_id": routes_shapes_map.get(route_id, ""),
                    "service_id": "ALL"
                })

                # Generate stop times
                times = trip_data.get("stops")
                if not times:
                    times = self._interpolate_trip_times(trip_start, trip_duration, stop_points)
                else:
                    times = {s['stop_id']: s['stop_time'] for s in times}
                
                # Ensure times are properly sequenced to avoid GTFS validation errors
                times = self._ensure_sequential_times(times, stop_points)
                    
                prev_stop_hours = None
                for j, (stop_id, distance, name) in enumerate(stop_points):
                    dep_time = times.get(stop_id, times.get(str(stop_id)))
                    if dep_time is None:
                        continue

                    # Convert to GTFS-compliant time format (handles 24+ hours for midnight crossing)
                    current_hours = dep_time // 100
                    current_minutes = dep_time % 100

                    # Apply 24+ hour logic for midnight crossing:
                    # If current stop time is earlier than previous stop, we crossed midnight
                    if prev_stop_hours is not None and current_hours < prev_stop_hours:
                        current_hours += 24

                    dep_time_str = self._format_gtfs_time(current_hours, current_minutes, 0)
                    # Arrival time should be same as departure time for most stops
                    arr_time_str = dep_time_str

                    # Track this stop's hours for next iteration (use original hours, not adjusted)
                    prev_stop_hours = dep_time // 100

                    stop_times.append({
                        "trip_id": trip_id,
                        "stop_id": str(stop_id),
                        "stop_sequence": str(j + 1),
                        "departure_time": dep_time_str,
                        "arrival_time": arr_time_str,
                        "timepoint": str(1 if j == 0 or j == len(stop_points) - 1 else 0)
                    })

                # Only add trip translation if last stop name exists
                if stop_points and stop_points[-1][2].strip():
                    translations.append({
                        "table_name": "trips",
                        "field_name": "trip_headsign",
                        "record_id": trip_id,
                        "language": "kn",
                        "translation": stop_points[-1][2]  # last stop name
                    })

        return trips, stop_times, translations

    def _interpolate_trip_times(self, start_time: int, duration: float, stop_points: List[tuple]) -> Dict:
        """Interpolate stop times based on distance"""
        if not stop_points:
            return {}
            
        total_distance = stop_points[-1][1]  # Last stop distance
        times = {}
        
        for stop_id, distance, name in stop_points:
            if total_distance > 0:
                # Proportional time based on distance
                progress = distance / total_distance
                minutes_from_start = int(progress * duration)
                stop_time = self._add_time_minutes(start_time, minutes_from_start)
            else:
                stop_time = start_time
                
            times[stop_id] = stop_time
            
        return times

    def _ensure_sequential_times(self, times: Dict, stop_points: List[tuple]) -> Dict:
        """Ensure stop times are properly sequential to avoid GTFS validation errors"""
        if not stop_points:
            return times
            
        # Sort stop points by distance to ensure correct sequence
        sorted_stops = sorted(stop_points, key=lambda x: x[1])
        
        # Get times in sequence order
        sequential_times = {}
        prev_time = None
        
        for stop_id, distance, name in sorted_stops:
            current_time = times.get(stop_id, times.get(str(stop_id)))
            if current_time is None:
                continue
                
            # Ensure current time is not before previous time
            if prev_time is not None and current_time <= prev_time:
                current_time = self._add_time_minutes(prev_time, 1)
                
            sequential_times[stop_id] = current_time
            prev_time = current_time
            
        return sequential_times
    
    def _add_time_minutes(self, hhmm_time: int, minutes: int) -> int:
        """Add minutes to HHMM format time"""
        if hhmm_time < 100:
            # Assume it's just minutes
            total_minutes = hhmm_time + minutes
        else:
            # HHMM format
            hours = hhmm_time // 100
            mins = hhmm_time % 100
            total_minutes = (hours * 60 + mins + minutes)
        
        # Convert back to HHMM - do NOT wrap around 24 hours for GTFS compliance
        final_hours = total_minutes // 60
        final_mins = total_minutes % 60
        return final_hours * 100 + final_mins

    def _format_gtfs_time(self, hours: int, minutes: int, seconds: int = 0) -> str:
        """Format time for GTFS in HH:MM:SS format

        Note: The 24+ hour logic is now handled at the stop iteration level,
        not here. This function just formats the time string.
        """
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    def _decode_polyline(self, polyline_str: str) -> List[tuple]:
        """Decode Google polyline format to lat/lng coordinates"""
        try:
            import polyline as _polyline
            return _polyline.decode(polyline_str)
        except Exception:
            return []

    def _records_to_csv(self, records: List[Dict]) -> str:
        """Convert list of dictionaries to CSV format with backwards-compatible field order"""
        if not records:
            return ""
        
        # Use fixed field order for backwards compatibility (matching old implementation)
        field_order_map = {
            'agency.txt': ['agency_id', 'agency_name', 'agency_url', 'agency_timezone', 'agency_phone', 'agency_fare_url'],
            'feed_info.txt': ['feed_publisher_name', 'feed_publisher_url', 'feed_contact_email', 'feed_lang', 'feed_version', 'feed_start_date', 'feed_end_date'],
            'calendar.txt': ['service_id', 'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday', 'start_date', 'end_date'],
            'routes.txt': ['route_id', 'route_short_name', 'route_long_name', 'route_type', 'agency_id'],
            'shapes.txt': ['shape_id', 'shape_pt_lat', 'shape_pt_lon', 'shape_pt_sequence'],
            'stops.txt': ['stop_id', 'stop_name', 'stop_lat', 'stop_lon'],
            'trips.txt': ['trip_id', 'route_id', 'shape_id', 'service_id'],
            'stop_times.txt': ['trip_id', 'stop_id', 'stop_sequence', 'departure_time', 'arrival_time', 'timepoint'],
            'translations.txt': ['table_name', 'field_name', 'record_id', 'language', 'translation']
        }
        
        # Determine field order - use predefined order if available, otherwise sorted
        first_record = records[0]
        available_fields = set(first_record.keys())
        
        # Try to match against known file types based on fields
        fieldnames = None
        for file_type, expected_fields in field_order_map.items():
            if set(expected_fields).issubset(available_fields):
                fieldnames = [f for f in expected_fields if f in available_fields]
                # Add any extra fields at the end
                extra_fields = sorted(available_fields - set(expected_fields))
                fieldnames.extend(extra_fields)
                break
        
        if not fieldnames:
            # Fallback to sorted order if no match found
            fieldnames = sorted(available_fields)
        
        # Build CSV content
        lines = [",".join(fieldnames)]
        
        for record in records:
            row = []
            for field in fieldnames:
                value = str(record.get(field, ""))
                # Escape quotes
                if '"' in value:
                    value = value.replace('"', '""')
                # Quote if contains comma or newline
                if ',' in value or '\n' in value:
                    value = f'"{value}"'
                row.append(value)
            lines.append(",".join(row))
            
        return "\n".join(lines)
