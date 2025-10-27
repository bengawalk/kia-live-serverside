"""
Live data service that integrates BMTC API with event loop for real-time data processing.
Bridges the gap between API data source and live data repository.
"""

import asyncio
import json
import logging
from typing import Dict, List, Optional
from pathlib import Path
from datetime import datetime
import pytz

from ..core.config import ApplicationConfig
from ..core.resource_manager import ResourceManager
from ..data.sources.bmtc_api import BMTCAPISource
from ..data.repositories.live_data_repo import LiveDataRepository
from ..data.models.vehicle import Vehicle
from ..data.models.trip import Trip, TripSchedule
from .data_transformation_service import DataTransformationService
from .prediction_service import PredictionService

logger = logging.getLogger(__name__)
local_tz = pytz.timezone("Asia/Kolkata")


class LiveDataService:
    """Service that manages live data ingestion and processing pipeline"""
    
    def __init__(self, config: ApplicationConfig, resource_manager: ResourceManager, live_repo: LiveDataRepository):
        self.config = config
        self.resource_manager = resource_manager
        self.live_repo = live_repo
        self.parent_route_ids: List[str] = []
        self.children_route_mapping: Dict[str, str] = {}
        self.ingestion_task: Optional[asyncio.Task] = None
        self.processing_task: Optional[asyncio.Task] = None
        self.transformation_task: Optional[asyncio.Task] = None
        self.is_running = False
        
        # Initialize transformation service
        self.transformation_service = DataTransformationService(config, live_repo)
        
        # Initialize prediction service
        self.prediction_service = PredictionService(config)
        
    async def start(self):
        """Start the live data service"""
        logger.info("Starting Live Data Service...")
        
        # Load route mappings
        await self._load_route_mappings()
        
        # Start background tasks
        self.is_running = True
        self.ingestion_task = asyncio.create_task(self._ingestion_loop())
        self.processing_task = asyncio.create_task(self._processing_loop())
        
        logger.info(f"Live Data Service started - monitoring {len(self.parent_route_ids)} routes")
        
    async def stop(self):
        """Stop the live data service"""
        logger.info("Stopping Live Data Service...")
        
        self.is_running = False
        
        # Cancel background tasks
        if self.ingestion_task and not self.ingestion_task.done():
            self.ingestion_task.cancel()
            try:
                await self.ingestion_task
            except asyncio.CancelledError:
                pass
                
        if self.processing_task and not self.processing_task.done():
            self.processing_task.cancel()
            try:
                await self.processing_task
            except asyncio.CancelledError:
                pass
                
        logger.info("Live Data Service stopped")
        
    async def _load_route_mappings(self):
        """Load route parent/children mappings from input files"""
        try:
            # Load parent route IDs (these are the ones we query the API with)
            parent_file = self.config.in_dir / "routes_parent_ids.json"
            if parent_file.exists():
                with open(parent_file, 'r', encoding='utf-8') as f:
                    parent_data = json.load(f)
                    self.parent_route_ids = [str(v) for v in parent_data.values()]
            
            # Load children mapping (for GTFS trip mapping)
            children_file = self.config.in_dir / "routes_children_ids.json"
            if children_file.exists():
                with open(children_file, 'r', encoding='utf-8') as f:
                    children_data = json.load(f)
                    self.children_route_mapping = {k: str(v) for k, v in children_data.items()}
                    
            logger.info(f"Loaded {len(self.parent_route_ids)} parent routes and {len(self.children_route_mapping)} children routes")
            
        except Exception as e:
            logger.error(f"Failed to load route mappings: {e}")
            # Fallback to empty lists
            self.parent_route_ids = []
            self.children_route_mapping = {}
    
    async def _ingestion_loop(self):
        """Main ingestion loop that fetches live data from BMTC API"""
        logger.info("Starting live data ingestion loop")
        
        while self.is_running:
            try:
                async with BMTCAPISource(self.resource_manager) as api:
                    # Process routes in small batches to prevent memory issues
                    batch_size = 3
                    
                    for i in range(0, len(self.parent_route_ids), batch_size):
                        if not self.is_running:
                            break
                            
                        batch_routes = self.parent_route_ids[i:i + batch_size]
                        
                        # Process batch concurrently
                        batch_tasks = []
                        for route_id in batch_routes:
                            task = self._process_route_data(api, route_id)
                            batch_tasks.append(task)
                        
                        # Wait for batch completion
                        try:
                            await asyncio.gather(*batch_tasks, return_exceptions=True)
                        except Exception as e:
                            logger.error(f"Batch processing error: {e}")
                        
                        # Brief pause between batches
                        if self.is_running:
                            await asyncio.sleep(1)
                
                # Wait before next full cycle - 25 seconds to ensure RT feed updates every 30s max
                if self.is_running:
                    await asyncio.sleep(25)
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Ingestion loop error: {e}")
                if self.is_running:
                    await asyncio.sleep(30)  # Wait longer on error
                    
        logger.info("Live data ingestion loop stopped")
    
    async def _process_route_data(self, api: BMTCAPISource, route_id: str):
        """Process live data for a single route"""
        try:
            vehicle_count = 0

            # Fetch vehicles for this parent route ID
            async for vehicle in api.fetch_route_vehicles(route_id):
                # print(f"VEHICLE!!!! RUNNING {vehicle.route_id} {vehicle.id}")
                if not self.is_running:
                    break
                    
                # Store vehicle in repository
                await self.live_repo.store_vehicle(vehicle)
                vehicle_count += 1
                
                # Try to match vehicle to a trip
                trip = await self.live_repo.match_vehicle_to_trip(vehicle)
                if trip:
                    # Update trip with vehicle data
                    updated_trip = await self.live_repo.update_trip_with_vehicle_data(trip, vehicle)
                    
                    # Generate predictions for remaining stops using lightweight method
                    current_time = datetime.now(local_tz)
                    predicted_trip = await self.prediction_service.predict_arrival_times_lightweight(updated_trip, current_time)
                    # Always store the predicted trip to ensure we have prediction data
                    await self.live_repo.store_trip(predicted_trip)
                
            if vehicle_count > 0:
                logger.debug(f"Processed {vehicle_count} vehicles for route {route_id}")
                
        except Exception as e:
            logger.error(f"Error processing route {route_id}: {e}")
    
    async def _processing_loop(self):
        """Background processing loop for trip management and cleanup"""
        logger.info("Starting live data processing loop")
        
        while self.is_running:
            try:
                await self._process_active_trips()
                await self._cleanup_stale_data()
                
                # Run every 60 seconds
                if self.is_running:
                    await asyncio.sleep(60)
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Processing loop error: {e}")
                if self.is_running:
                    await asyncio.sleep(60)
                    
        logger.info("Live data processing loop stopped")
    
    async def _process_active_trips(self):
        """Process and update active trips"""
        try:
            active_trips = await self.live_repo.get_active_trips()
            
            for trip in active_trips:
                # Check if trip is still valid (not too old)
                try:
                    if not trip.is_active:
                        trip.mark_completed()
                        await self.live_repo.store_trip(trip)
                except Exception as e:
                    logger.error(f"Error processing active trip {trip.id}: {e}")
                    
        except Exception as e:
            logger.error(f"Error processing active trips: {e}")
    
    async def _cleanup_stale_data(self):
        """Clean up stale data from the repository"""
        try:
            # This will trigger the repository's own cleanup mechanisms
            stats = await self.live_repo.get_repository_stats()
            
            # Log stats for monitoring
            if stats['health']['memory_pressure'] > 0.8:
                logger.warning(f"High memory pressure: {stats['health']['memory_pressure']:.2%}")
                
        except Exception as e:
            logger.error(f"Error during cleanup: {e}")
    
    def get_service_stats(self) -> Dict[str, any]:
        """Get service statistics"""
        return {
            "is_running": self.is_running,
            "parent_routes_count": len(self.parent_route_ids),
            "children_routes_count": len(self.children_route_mapping),
            "ingestion_task_running": self.ingestion_task and not self.ingestion_task.done(),
            "processing_task_running": self.processing_task and not self.processing_task.done(),
            "transformation_task_running": self.transformation_task and not self.transformation_task.done(),
            "transformation_stats": self.transformation_service.get_transformation_stats()
        }
