# src/core/application.py
from src.core.event_loop import ManagedEventLoop
from src.core.resource_manager import ResourceManager


class Application:
    """Main application coordinator"""

    def __init__(self):
        self.event_loop = ManagedEventLoop()
        self.resource_manager = ResourceManager()
        self.services = {}

    async def start(self):
        """Start all application services"""
        # Start event loop housekeeping
        await self.event_loop.start()
        # Start all registered services (non-blocking if they are async-compatible)
        for service in self.services.values():
            start_method = getattr(service, 'start', None)
            if start_method is not None:
                result = start_method()
                if hasattr(result, '__await__'):
                    await result

    async def stop(self):
        """Gracefully stop application"""
        # Stop services in reverse registration order
        for name, service in list(self.services.items())[::-1]:
            stop_method = getattr(service, 'stop', None)
            if stop_method is not None:
                result = stop_method()
                if hasattr(result, '__await__'):
                    await result
        # Stop event loop housekeeping
        await self.event_loop.stop()

    def register_service(self, name: str, service):
        self.services[name] = service

    def get_service(self, name: str):
        return self.services.get(name)