#!/usr/bin/env python3
"""
Main entry point for the GTFS Live Data System (New Architecture)
Bridges together all components of the new src/ system.
"""

import asyncio
import logging
import signal
import sys
import json
import traceback
import argparse
from pathlib import Path
from typing import Dict, Any, Optional

# Core components
from src.core.application import Application
from src.core.config import ApplicationConfig
from src.core.resource_manager import ResourceManager

# Services and server
from src.services.gtfs_service import GTFSService
from src.services.realtime_service import RealtimeService
from src.services.live_data_service import LiveDataService
from src.services.trip_scheduler import TripSchedulerService
from src.services.database_service import DatabaseService
from src.services.prediction_service import PredictionService
from src.api.web_server import WebServer

# Data layer
from src.data.repositories.live_data_repo import LiveDataRepository
from src.data.sources.bmtc_api import BMTCAPISource

# Middleware
from src.middleware import PredictionMiddleware

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class GTFSLiveDataSystem:
    """Main system coordinator that integrates all components"""

    def __init__(self, predict_times: bool = False):
        self.config = ApplicationConfig()
        self.app = Application()
        self.resource_manager: Optional[ResourceManager] = None
        self.gtfs_service: Optional[GTFSService] = None
        self.live_repo: Optional[LiveDataRepository] = None
        self.live_data_service: Optional[LiveDataService] = None
        self.trip_scheduler: Optional[TripSchedulerService] = None
        self.realtime_service: Optional[RealtimeService] = None
        self.web_server: Optional[WebServer] = None
        self.shutdown_event = asyncio.Event()
        self.predict_times = predict_times
        
    async def _bootstrap_static_gtfs(self):
        """Load in/ files, build GTFS, and write to out/ for endpoints."""
        print('Attempting to build static GTFS...')
        try:
            in_dir = self.config.in_dir
            out_dir = self.config.out_dir
            out_dir.mkdir(parents=True, exist_ok=True)

            def _read_json(path: Path) -> Any:
                with open(path, 'r', encoding='utf-8') as f:
                    return json.load(f)

            input_data = {
                "client_stops": _read_json(in_dir / "client_stops.json"),
                "routes_children": _read_json(in_dir / "routes_children_ids.json"),
                "routes_parent": _read_json(in_dir / "routes_parent_ids.json"),
                "start_times": _read_json(in_dir / "start_times.json"),
                "routelines": _read_json(in_dir / "routelines.json"),
                "times": _read_json(in_dir / "times.json"),
            }

            gtfs_zip_bytes = await self.gtfs_service.generate_gtfs_zip(input_data)
            with open(out_dir / "gtfs.zip", "wb") as f:
                f.write(gtfs_zip_bytes)

            # Write a simple version string for /gtfs-version
            version_rows = await self.gtfs_service.generate_gtfs_dataset(input_data)
            version = version_rows.get("feed_info.txt", [{}])[0].get("feed_version", "unknown")
            with open(out_dir / "feed_info.txt", "w", encoding='utf-8') as f:
                f.write(str(version))

            logger.info("Static GTFS built and written to out/gtfs.zip")
        except Exception as e:
            traceback.print_exc()
            logger.error(f"Static GTFS bootstrap failed: {e}")

    async def send_updates_to_clients(self):
        # await asyncio.sleep(5)
        async for u in self.realtime_service.stream_updates():
            for c in self.web_server.ws_clients:
                await c.send_bytes(u)

    async def setup(self):
        """Initialize and register all services"""
        logger.info("Setting up GTFS Live Data System...")

        # Run prediction middleware if --predict-times flag is set
        if self.predict_times:
            logger.info("--predict-times flag detected, running fresh predictions...")
            prediction_middleware = PredictionMiddleware(self.config)
            success = await prediction_middleware.generate_fresh_predictions()
            if not success:
                logger.warning("Failed to generate fresh predictions, falling back to existing times.json")
            else:
                logger.info("Fresh predictions generated successfully")

        # Initialize core
        self.resource_manager = ResourceManager()

        # Initialize database service
        self.database_service = DatabaseService(self.config)
        await self.database_service.initialize()

        # Data repositories
        self.live_repo = LiveDataRepository(
            resource_manager=self.resource_manager,
            database_service=self.database_service,
            max_memory_mb=self.config.max_memory_mb // 3
        )
        await self.live_repo.start()

        # Services
        self.gtfs_service = GTFSService(self.config)
        self.realtime_service = RealtimeService(self.config, self.live_repo)

        # Initialize and start the new live data service
        self.live_data_service = LiveDataService(self.config, self.resource_manager, self.live_repo)

        # Initialize trip scheduler service (CRITICAL: Fixes Issue 2 - Empty GTFS-RT)
        self.trip_scheduler = TripSchedulerService(self.config, self.live_repo)

        # Web server
        self.web_server = WebServer(self.config, self.gtfs_service, self.realtime_service)

        # Register services for lifecycle management
        self.app.register_service("database_service", self.database_service)
        self.app.register_service("live_repo", self.live_repo)
        self.app.register_service("live_data_service", self.live_data_service)
        self.app.register_service("trip_scheduler", self.trip_scheduler)

        # Bootstrap static GTFS (non-blocking)
        self.app.event_loop.add_task(self._bootstrap_static_gtfs(), name="bootstrap_static_gtfs")

        # Start web server (non-blocking)
        self.app.event_loop.add_task(self.web_server.start(host='0.0.0.0', port=59966), name="web_server_start")

        # Start streaming GTFS-RT updates to web_server.ws_clients (non-blocking)
        self.app.event_loop.add_task(self.send_updates_to_clients(), name="ws_streaming")
        logger.info("All services initialized and registered")
        
    def _setup_signal_handlers(self):
        """Setup signal handlers for graceful shutdown"""
        def shutdown_handler(signum, frame):
            logger.info(f"Received signal {signum}, shutting down...")
            self.shutdown_event.set()
            self.app.event_loop.shutdown_event.set()
        
        signal.signal(signal.SIGINT, shutdown_handler)
        signal.signal(signal.SIGTERM, shutdown_handler)
        
    async def start(self):
        """Start the system"""
        logger.info("Starting GTFS Live Data System...")
        
        # Setup signal handlers
        self._setup_signal_handlers()
        
        # Setup services
        await self.setup()
        
        # Start application
        await self.app.start()
        
        # logger.info("System started successfully")
        # logger.info("Web server running on http://0.0.0.0:59966")
        # logger.info("Available endpoints:")
        # logger.info("  - GET /gtfs.zip")
        # logger.info("  - GET /gtfs-version")
        # logger.info("  - GET /gtfs-rt.proto")
        # logger.info("  - GET /ws/gtfs-rt")
        
        # Wait for shutdown signal
        await self.shutdown_event.wait()
        
    async def stop(self):
        """Stop the system gracefully"""
        logger.info("Stopping GTFS Live Data System...")
        
        # Signal shutdown
        self.shutdown_event.set()
        self.app.event_loop.shutdown_event.set()
        
        # Stop application and all services (services have their own cleanup)
        await self.app.stop()
        
        logger.info("System stopped gracefully")


async def main(predict_times: bool = False):
    """Main entry point"""
    system = GTFSLiveDataSystem(predict_times=predict_times)

    try:
        await system.start()
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt")
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        sys.exit(1)
    finally:
        await system.stop()


if __name__ == "__main__":
    # Parse command-line arguments
    parser = argparse.ArgumentParser(
        description="GTFS Live Data System - Real-time transit feed generator"
    )
    parser.add_argument(
        '--predict-times',
        action='store_true',
        help='Generate fresh time predictions using the universal model based on current date (instead of static Wednesday predictions)'
    )
    args = parser.parse_args()

    try:
        asyncio.run(main(predict_times=args.predict_times))
    except KeyboardInterrupt:
        print("\nShutdown complete")
        sys.exit(0)
