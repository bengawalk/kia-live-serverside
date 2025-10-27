"""
Web server implementation for Phase 5 of architectural rewrite.
"""

import asyncio
import logging
import weakref
import gc
from typing import Optional

from aiohttp import web, WSMsgType

from ..core.config import ApplicationConfig
from ..services.gtfs_service import GTFSService
from ..services.realtime_service import RealtimeService

logger = logging.getLogger(__name__)

# CORS settings
CORS_SETTINGS = {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type, X-Requested-With, Authorization',
    'Access-Control-Max-Age': '3600',
}

class WebServer:
    """Web server maintaining exact same endpoints as old implementation"""

    CORS_OPTIONS = {
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
        'Access-Control-Allow-Headers': '*',
        'Access-Control-Max-Age': '3600',
    }
    def __init__(self, config: ApplicationConfig, gtfs_service: GTFSService, 
                 realtime_service: RealtimeService):
        self.config = config
        self.gtfs_service = gtfs_service
        self.realtime_service = realtime_service
        
        # WebSocket connections (using WeakSet like original)
        self.ws_clients = weakref.WeakSet()
        
        # Web app and server
        self.app: Optional[web.Application] = None
        self.runner: Optional[web.AppRunner] = None
        self.site: Optional[web.TCPSite] = None

    async def create_app(self) -> web.Application:
        """Create and configure the web application"""
        app = web.Application()
        
        # Setup routes - exact same paths as original
        app.router.add_get('/gtfs.zip', self._handle_gtfs_zip)
        app.router.add_get('/gtfs-version', self._handle_gtfs_version)
        app.router.add_get('/gtfs-rt.proto', self._handle_gtfs_realtime)
        app.router.add_get('/ws/gtfs-rt', self._handle_websocket)
        app.router.add_route('OPTIONS', '/{tail:.*}', self._handle_options)
        
        return app

    async def _handle_gtfs_zip(self, request: web.Request):
        """GET /gtfs.zip - Serve the GTFS zip file"""
        zip_path = self.config.out_dir / "gtfs.zip"
        if not zip_path.exists():
            return web.Response(status=404, text="GTFS ZIP not found.")
        headers = CORS_SETTINGS.copy()
        headers['Content-Disposition'] = 'attachment; filename=gtfs.zip'
        return web.FileResponse(
            zip_path,
            headers=headers
        )

    async def _handle_gtfs_version(self, request: web.Request) -> web.Response:
        """GET /gtfs-version - Return version info"""
        version_file = self.config.out_dir / "feed_info.txt"
        if not version_file.exists():
            return web.json_response({"error": "version file not found"}, status=404)

        with open(version_file, "r") as f:
            version = f.read().strip()
        response = web.Response(text=version, headers=CORS_SETTINGS)
        response.headers["Cache-Control"] = "no-store"
        return response

    async def _handle_gtfs_realtime(self, request: web.Request) -> web.Response:
        """GET /gtfs-rt.proto - Serve GTFS-RT protobuf feed"""
        feed_data = await self.realtime_service.get_current_feed()
        headers = CORS_SETTINGS
        headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
        headers['Pragma'] = 'no-cache'
        headers['Expires'] = '0'
        if not feed_data:
            return web.Response(status=503, text="GTFS-RT feed not available", headers=headers)
        return web.Response(
            body=feed_data,
            content_type='application/x-protobuf',
            headers=headers
        )

    async def _handle_websocket(self, request: web.Request) -> web.WebSocketResponse:
        """GET /ws/gtfs-rt - WebSocket endpoint"""
        ws = web.WebSocketResponse(compress=True)
        await ws.prepare(request)
        self.ws_clients.add(ws)

        try:
            # Send current snapshot immediately
            snapshot = await self.realtime_service.get_current_feed()
            if snapshot:
                await ws.send_bytes(snapshot)

            async for msg in ws:
                if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.ERROR):
                    break

        except Exception as e:
            logger.error(f"WebSocket error: {e}")
        finally:
            self.ws_clients.discard(ws)
            await ws.close()
            gc.collect()

        return ws

    async def _handle_options(self, request: web.Request) -> web.Response:
        """Handle OPTIONS preflight requests"""
        return web.Response(headers=CORS_SETTINGS)

    async def start(self, host: str = '0.0.0.0', port: int = 59965) -> None:
        """Start the web server"""
        logger.info(f"Starting web server on {host}:{port}")
        
        self.app = await self.create_app()
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        
        self.site = web.TCPSite(self.runner, host, port)
        await self.site.start()
        
        logger.info(f"Web server started on {host}:{port}")


    async def stop(self) -> None:
        """Stop the web server gracefully"""
        logger.info("Stopping web server...")
        
        for ws in list(self.ws_clients):
            try:
                await ws.close()
            except:
                pass
            self.ws_clients.discard(ws)
        
        if self.site:
            await self.site.stop()
        if self.runner:
            await self.runner.cleanup()
        
        logger.info("Web server stopped")
