# src/core/event_loop.py
import asyncio


class ManagedEventLoop:
    """Single event loop with proper resource management"""

    def __init__(self):
        self.loop = None
        self.shutdown_event = asyncio.Event()
        self.running_tasks = set()

    async def start(self):
        """Start the event loop with all services"""
        if self.loop is None:
            self.loop = asyncio.get_running_loop()
        # Start periodic cleanup task
        self.add_task(self.cleanup_completed_tasks(), name="cleanup_completed_tasks")

    async def stop(self):
        """Gracefully stop all services and tasks"""
        # Signal shutdown
        self.shutdown_event.set()
        # Cancel all running tasks except the current one
        current_task = asyncio.current_task()
        tasks_to_cancel = [t for t in list(self.running_tasks) if t is not current_task]
        for task in tasks_to_cancel:
            if not task.done() and not task.cancelled():
                task.cancel()
        # Wait for tasks to finish
        if tasks_to_cancel:
            await asyncio.gather(*tasks_to_cancel, return_exceptions=True)
        # Final cleanup
        await self.cleanup_completed_tasks()

    def add_task(self, coro, name: str):
        """Add task with automatic cleanup"""
        if self.loop is None:
            self.loop = asyncio.get_running_loop()
        task = self.loop.create_task(coro, name=name)
        self.running_tasks.add(task)
        # Auto-remove from set when done
        def _discard(_):
            self.running_tasks.discard(task)
        task.add_done_callback(_discard)
        return task

    async def cleanup_completed_tasks(self):
        """Periodic cleanup of completed tasks"""
        try:
            while not self.shutdown_event.is_set():
                # Remove completed tasks
                completed = [t for t in list(self.running_tasks) if t.done()]
                for task in completed:
                    self.running_tasks.discard(task)
                # Sleep briefly to avoid busy loop
                await asyncio.sleep(0.05)
        finally:
            # One last pass
            completed = [t for t in list(self.running_tasks) if t.done()]
            for task in completed:
                self.running_tasks.discard(task)