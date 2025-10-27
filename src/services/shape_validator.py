"""
Shape Validator Service
Validates vehicle positions against route shapes to detect erratic trips.
"""

import logging
import json
from typing import List, Tuple, Optional, Dict
from math import radians, cos, sin, asin, sqrt
from urllib.parse import unquote

logger = logging.getLogger(__name__)


class ShapeValidator:
    """
    Validates vehicle positions against route shapes from routelines.json
    to detect trips that are too far from their expected route.
    """

    def __init__(self, routelines_path: str = "in/routelines.json"):
        """
        Initialize the shape validator.

        Args:
            routelines_path: Path to routelines.json file
        """
        self.routelines_path = routelines_path
        self.shapes: Dict[str, List[Tuple[float, float]]] = {}
        self._load_shapes()

    def _load_shapes(self):
        """Load and decode route shapes from routelines.json"""
        try:
            with open(self.routelines_path, 'r') as f:
                routelines_data = json.load(f)

            for route_name, encoded_polyline in routelines_data.items():
                try:
                    # Decode URL-encoded polyline
                    decoded_polyline = unquote(encoded_polyline)
                    # Decode polyline to list of (lat, lon) tuples
                    coordinates = self._decode_polyline(decoded_polyline)
                    self.shapes[route_name] = coordinates
                    logger.debug(f"Loaded shape for route {route_name}: {len(coordinates)} points")
                except Exception as e:
                    logger.error(f"Failed to decode polyline for route {route_name}: {e}")

            logger.info(f"Loaded {len(self.shapes)} route shapes from {self.routelines_path}")
        except FileNotFoundError:
            logger.warning(f"Routelines file not found: {self.routelines_path}")
        except Exception as e:
            logger.error(f"Error loading routelines: {e}")

    def _decode_polyline(self, encoded: str) -> List[Tuple[float, float]]:
        """
        Decode a Mapbox polyline string to list of (lat, lon) coordinates.

        Note: Mapbox polylines store coordinates in (lon, lat) order, but we return
        them as (lat, lon) tuples for consistency with standard coordinate notation.

        Args:
            encoded: Encoded polyline string (Mapbox format with lon, lat order)

        Returns:
            List of (latitude, longitude) tuples
        """
        coordinates = []
        index = 0
        lon = 0  # Mapbox format: first value is longitude
        lat = 0  # Mapbox format: second value is latitude

        while index < len(encoded):
            # Decode longitude (first in Mapbox format)
            shift = 0
            result = 0
            while True:
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1f) << shift
                shift += 5
                if b < 0x20:
                    break

            dlon = ~(result >> 1) if (result & 1) else (result >> 1)
            lon += dlon

            # Decode latitude (second in Mapbox format)
            shift = 0
            result = 0
            while True:
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1f) << shift
                shift += 5
                if b < 0x20:
                    break

            dlat = ~(result >> 1) if (result & 1) else (result >> 1)
            lat += dlat

            # Convert to degrees and append as (lat, lon) for standard notation
            coordinates.append((lat / 1e5, lon / 1e5))

        return coordinates

    def _calculate_distance(
        self,
        lat1: float, lon1: float,
        lat2: float, lon2: float
    ) -> float:
        """
        Calculate distance between two points in kilometers using Haversine formula.

        Returns:
            Distance in kilometers
        """
        lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])

        dlat = lat2 - lat1
        dlon = lon2 - lon1
        a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
        c = 2 * asin(sqrt(a))
        r = 6371  # Earth's radius in kilometers

        return c * r

    def _point_to_line_segment_distance(
        self,
        point_lat: float,
        point_lon: float,
        seg_start_lat: float,
        seg_start_lon: float,
        seg_end_lat: float,
        seg_end_lon: float
    ) -> float:
        """
        Calculate minimum distance from a point to a line segment.

        Args:
            point_lat, point_lon: Coordinates of the point
            seg_start_lat, seg_start_lon: Start of line segment
            seg_end_lat, seg_end_lon: End of line segment

        Returns:
            Distance in kilometers
        """
        # Convert to radians for calculation
        point_lat_rad = radians(point_lat)
        point_lon_rad = radians(point_lon)
        seg_start_lat_rad = radians(seg_start_lat)
        seg_start_lon_rad = radians(seg_start_lon)
        seg_end_lat_rad = radians(seg_end_lat)
        seg_end_lon_rad = radians(seg_end_lon)

        # Calculate distances
        dist_to_start = self._calculate_distance(
            point_lat, point_lon, seg_start_lat, seg_start_lon
        )
        dist_to_end = self._calculate_distance(
            point_lat, point_lon, seg_end_lat, seg_end_lon
        )

        # If segment has zero length, return distance to point
        if seg_start_lat == seg_end_lat and seg_start_lon == seg_end_lon:
            return dist_to_start

        # Calculate projection using dot product
        # Vector from start to end
        seg_lat = seg_end_lat_rad - seg_start_lat_rad
        seg_lon = seg_end_lon_rad - seg_start_lon_rad

        # Vector from start to point
        point_lat_diff = point_lat_rad - seg_start_lat_rad
        point_lon_diff = point_lon_rad - seg_start_lon_rad

        # Dot product
        dot = point_lat_diff * seg_lat + point_lon_diff * seg_lon
        seg_length_sq = seg_lat * seg_lat + seg_lon * seg_lon

        if seg_length_sq == 0:
            return dist_to_start

        # Parameter t of projection point on line segment (0 <= t <= 1)
        t = max(0, min(1, dot / seg_length_sq))

        # Projection point
        proj_lat = seg_start_lat + t * (seg_end_lat - seg_start_lat)
        proj_lon = seg_start_lon + t * (seg_end_lon - seg_start_lon)

        # Distance from point to projection
        return self._calculate_distance(point_lat, point_lon, proj_lat, proj_lon)

    def get_min_distance_to_shape(
        self,
        lat: float,
        lon: float,
        route_name: str
    ) -> Optional[float]:
        """
        Calculate minimum distance from a point to a route shape.

        Args:
            lat: Latitude of point
            lon: Longitude of point
            route_name: Route name (e.g., "1864_0", "1864_1")

        Returns:
            Minimum distance in kilometers, or None if shape not found
        """
        if route_name not in self.shapes:
            logger.debug(f"Shape not found for route: {route_name}")
            return None

        shape_points = self.shapes[route_name]

        if len(shape_points) < 2:
            logger.warning(f"Shape for route {route_name} has fewer than 2 points")
            return None

        min_distance = float('inf')

        # Check distance to each line segment in the shape
        for i in range(len(shape_points) - 1):
            seg_start_lat, seg_start_lon = shape_points[i]
            seg_end_lat, seg_end_lon = shape_points[i + 1]

            distance = self._point_to_line_segment_distance(
                lat, lon,
                seg_start_lat, seg_start_lon,
                seg_end_lat, seg_end_lon
            )

            min_distance = min(min_distance, distance)

        return min_distance if min_distance != float('inf') else None

    def is_position_on_route(
        self,
        lat: float,
        lon: float,
        route_name: str,
        max_distance_km: float = 10.0
    ) -> bool:
        """
        Check if a position is within acceptable distance of a route shape.

        Args:
            lat: Latitude of position
            lon: Longitude of position
            route_name: Route name
            max_distance_km: Maximum acceptable distance in kilometers

        Returns:
            True if position is within max_distance_km of route, False otherwise
        """
        distance = self.get_min_distance_to_shape(lat, lon, route_name)

        if distance is None:
            # If shape not found, be permissive
            logger.warning(f"Cannot validate position for route {route_name}: shape not found")
            return True

        return distance <= max_distance_km

    def validate_positions_for_trip(
        self,
        positions: List[Tuple[float, float]],
        route_name: str,
        max_distance_km: float = 10.0,
        min_valid_ratio: float = 0.7
    ) -> Tuple[bool, float, int, int]:
        """
        Validate a list of positions against a route shape.

        Args:
            positions: List of (lat, lon) tuples
            route_name: Route name
            max_distance_km: Maximum acceptable distance in kilometers
            min_valid_ratio: Minimum ratio of valid positions required (0.0 to 1.0)

        Returns:
            Tuple of (is_valid, avg_distance, valid_count, total_count)
        """
        if not positions:
            return (True, 0.0, 0, 0)

        if route_name not in self.shapes:
            logger.warning(f"Cannot validate trip: shape not found for route {route_name}")
            return (True, 0.0, len(positions), len(positions))

        valid_count = 0
        total_distance = 0.0

        for lat, lon in positions:
            distance = self.get_min_distance_to_shape(lat, lon, route_name)

            if distance is not None:
                total_distance += distance
                if distance <= max_distance_km:
                    valid_count += 1

        total_count = len(positions)
        avg_distance = total_distance / total_count if total_count > 0 else 0.0
        valid_ratio = valid_count / total_count if total_count > 0 else 0.0

        is_valid = valid_ratio >= min_valid_ratio

        return (is_valid, avg_distance, valid_count, total_count)

    def get_route_shape_name(self, route_id: str, direction_id: int) -> str:
        """
        Get the route shape name from route_id and direction_id.

        Args:
            route_id: Route ID - can be either route key ("KIA-4 UP") or just route number
            direction_id: Direction ID (0 or 1, where 0=UP, 1=DOWN)

        Returns:
            Route shape name matching routelines.json format (e.g., "KIA-4 UP", "KIA-4 DOWN")
        """
        # If route_id already contains direction (e.g., "KIA-4 UP"), return as-is
        if " UP" in route_id or " DOWN" in route_id:
            return route_id

        # Otherwise, construct from route_id and direction_id
        direction_str = "DOWN" if direction_id == 1 else "UP"
        return f"{route_id} {direction_str}"

    def get_opposite_direction_shape_name(self, route_shape_name: str) -> Optional[str]:
        """
        Get the opposite direction shape name.

        Args:
            route_shape_name: Current route shape name (e.g., "KIA-4 UP", "KIA-4 DOWN")

        Returns:
            Opposite direction shape name (e.g., "KIA-4 DOWN", "KIA-4 UP"), or None if not found
        """
        try:
            # Handle format like "KIA-4 UP" or "KIA-4 DOWN"
            if " UP" in route_shape_name:
                opposite_shape_name = route_shape_name.replace(" UP", " DOWN")
            elif " DOWN" in route_shape_name:
                opposite_shape_name = route_shape_name.replace(" DOWN", " UP")
            else:
                # Fallback: Handle old format like "1864_0" or "1864_1"
                route_id, direction_str = route_shape_name.rsplit('_', 1)
                direction_id = int(direction_str)
                opposite_direction_id = 1 - direction_id
                opposite_shape_name = f"{route_id}_{opposite_direction_id}"

            if opposite_shape_name in self.shapes:
                return opposite_shape_name
            else:
                logger.debug(f"Opposite direction shape not found: {opposite_shape_name}")
                return None
        except (ValueError, AttributeError) as e:
            logger.error(f"Invalid route shape name format: {route_shape_name}, error: {e}")
            return None

    def check_opposite_direction_match(
        self,
        positions: List[Tuple[float, float]],
        current_route_name: str,
        max_distance_km: float = 10.0
    ) -> Tuple[bool, Optional[str], float]:
        """
        Check if positions match better with opposite direction route.

        Args:
            positions: List of (lat, lon) tuples
            current_route_name: Current route shape name
            max_distance_km: Maximum acceptable distance in kilometers

        Returns:
            Tuple of (should_swap, opposite_route_name, improvement_ratio)
        """
        if not positions:
            return (False, None, 0.0)

        # Validate against current direction
        current_valid, current_avg_dist, current_valid_count, total_count = \
            self.validate_positions_for_trip(positions, current_route_name, max_distance_km)

        # Get opposite direction shape name
        opposite_route_name = self.get_opposite_direction_shape_name(current_route_name)

        if opposite_route_name is None:
            return (False, None, 0.0)

        # Validate against opposite direction
        opposite_valid, opposite_avg_dist, opposite_valid_count, _ = \
            self.validate_positions_for_trip(positions, opposite_route_name, max_distance_km)

        # Calculate improvement ratio
        if current_valid_count == 0:
            improvement_ratio = float('inf') if opposite_valid_count > 0 else 0.0
        else:
            improvement_ratio = opposite_valid_count / current_valid_count

        # Should swap if opposite direction has significantly better match
        # Require at least 50% improvement and opposite has >70% valid positions
        should_swap = (
            improvement_ratio > 1.5 and
            opposite_valid_count / total_count > 0.7
        )

        if should_swap:
            logger.info(
                f"Opposite direction match detected: "
                f"Current={current_route_name} ({current_valid_count}/{total_count} valid, avg={current_avg_dist:.2f}km), "
                f"Opposite={opposite_route_name} ({opposite_valid_count}/{total_count} valid, avg={opposite_avg_dist:.2f}km), "
                f"Improvement={improvement_ratio:.2f}x"
            )

        return (should_swap, opposite_route_name, improvement_ratio)