"""
Database service for the new architecture.
Handles all database logging operations with connection pooling and memory management.
"""

import sqlite3
import asyncio
import aiosqlite
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime, date, timedelta
import logging

from src.core.config import ApplicationConfig
from src.data.models.vehicle import Vehicle
from src.data.models.trip import Trip, TripStop

logger = logging.getLogger(__name__)


class DatabaseService:
    """Handle all database logging operations with SQLite"""

    def __init__(self, config: ApplicationConfig):
        self.config = config
        self.db_path = config.db_path
        self.connection_pool_size = 5  # Support concurrent reads (was 1, increased to handle 10 queries/sec)
        self._initialized = False

        # True connection pooling
        self._connection_pool: List[aiosqlite.Connection] = []
        self._available_connections = asyncio.Queue()
        self._pool_lock = asyncio.Lock()

        # Background write queues for fire-and-forget writes
        self._write_queue = asyncio.Queue(maxsize=10000)  # Vehicle positions
        self._stop_write_queue = asyncio.Queue(maxsize=5000)  # Completed stops (increased from 1000)
        self._write_worker_task: Optional[asyncio.Task] = None
        self._stop_write_worker_task: Optional[asyncio.Task] = None
        self._is_running = False
        self._dropped_writes = 0
        self._dropped_stop_writes = 0

    async def initialize(self):
        """Initialize database schema and connection pool"""
        if self._initialized:
            return

        # Ensure the directory exists
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # Create initial connection for schema setup
        async with aiosqlite.connect(str(self.db_path)) as conn:
            # Enable WAL mode for concurrent read/write access (CRITICAL FIX)
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA synchronous=NORMAL")  # Faster, still safe with WAL
            await conn.execute("PRAGMA cache_size=-32000")  # 128MB cache (increased from 16MB for better query performance)
            await conn.execute("PRAGMA busy_timeout=500")   # .5 second timeout - fail fast to prevent cascading delays
            await conn.execute("PRAGMA temp_store=MEMORY")   # Keep temp tables in memory

            await conn.execute('''
                CREATE TABLE IF NOT EXISTS completed_stop_times (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    stop_id TEXT,
                    trip_id TEXT,
                    route_id TEXT,
                    date TEXT,
                    actual_arrival TEXT,
                    actual_departure TEXT,
                    scheduled_arrival TEXT,
                    scheduled_departure TEXT,
                    UNIQUE(stop_id, trip_id, date)
                )
            ''')

            await conn.execute('''
                CREATE TABLE IF NOT EXISTS vehicle_positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    vehicle_id TEXT,
                    trip_id TEXT,
                    route_id TEXT,
                    lat REAL,
                    lon REAL,
                    bearing REAL,
                    timestamp INTEGER,
                    speed REAL,
                    status TEXT,
                    UNIQUE(vehicle_id, trip_id, route_id, lat, lon, timestamp)
                )
            ''')

            # Create index for efficient querying by trip_id and timestamp
            await conn.execute('''
                CREATE INDEX IF NOT EXISTS idx_vehicle_positions_trip_ts
                ON vehicle_positions(trip_id, timestamp)
            ''')

            # Create index for efficient querying by vehicle_id, route_id and timestamp
            await conn.execute('''
                CREATE INDEX IF NOT EXISTS idx_vehicle_positions_vehicle_route_ts
                ON vehicle_positions(vehicle_id, route_id, timestamp)
            ''')

            await conn.commit()

        # Create connection pool with persistent connections
        async with self._pool_lock:
            for i in range(self.connection_pool_size):
                conn = await aiosqlite.connect(str(self.db_path))
                # Configure each pooled connection
                await conn.execute("PRAGMA journal_mode=WAL")
                await conn.execute("PRAGMA synchronous=NORMAL")
                await conn.execute("PRAGMA busy_timeout=500")  # Match schema connection - fail fast
                await conn.execute("PRAGMA temp_store=MEMORY")
                self._connection_pool.append(conn)
                await self._available_connections.put(conn)

        # Start background write worker
        self._is_running = True
        self._write_worker_task = asyncio.create_task(self._background_writer())

        self._initialized = True
        logger.info(f"Database initialized at {self.db_path} with {self.connection_pool_size} pooled connections (WAL mode enabled, background writer active)")

    async def _get_connection(self, timeout: float = 0.5) -> aiosqlite.Connection:
        """
        Get a connection from the pool with timeout to prevent cascading delays.

        Args:
            timeout: Maximum time to wait for a connection (default: 3 seconds)

        Returns:
            A database connection from the pool

        Raises:
            RuntimeError: If connection pool is exhausted after timeout
        """
        try:
            return await asyncio.wait_for(
                self._available_connections.get(),
                timeout=timeout
            )
        except asyncio.TimeoutError:
            logger.error(f"Connection pool exhausted - timeout after {timeout}s waiting for connection")
            raise RuntimeError("Database connection pool exhausted - all connections busy")

    async def _return_connection(self, conn: aiosqlite.Connection):
        """Return a connection to the pool"""
        await self._available_connections.put(conn)

    async def _replace_broken_connection(self):
        """Replace a broken connection in the pool with a new one"""
        try:
            # Create new connection
            new_conn = await aiosqlite.connect(str(self.db_path))
            await new_conn.execute("PRAGMA journal_mode=WAL")
            await new_conn.execute("PRAGMA synchronous=NORMAL")
            await new_conn.execute("PRAGMA busy_timeout=500")
            await new_conn.execute("PRAGMA temp_store=MEMORY")

            # Add to pool
            await self._available_connections.put(new_conn)
            logger.info("Replaced broken connection in pool")

        except Exception as e:
            logger.error(f"Failed to create replacement connection: {e}")

    async def stop(self):
        """Stop method for application lifecycle (alias for close)"""
        await self.close()

    async def close(self):
        """Close all pooled connections and stop background writer"""
        # Stop background writer
        self._is_running = False
        if self._write_worker_task and not self._write_worker_task.done():
            self._write_worker_task.cancel()
            try:
                await self._write_worker_task
            except asyncio.CancelledError:
                pass

        # Close connections
        async with self._pool_lock:
            for conn in self._connection_pool:
                await conn.close()
            self._connection_pool.clear()
            logger.info(f"Database connection pool closed (dropped {self._dropped_writes} writes due to queue full)")

    async def _background_writer(self):
        """
        Background task that processes queued database writes.
        Runs in its own task, never blocking the main event loop.
        """
        logger.info("Background database writer started")

        # Dedicated connection for background writes
        write_conn = await aiosqlite.connect(str(self.db_path))
        await write_conn.execute("PRAGMA journal_mode=WAL")
        await write_conn.execute("PRAGMA synchronous=NORMAL")
        await write_conn.execute("PRAGMA busy_timeout=5000")  # Longer timeout for background
        await write_conn.execute("PRAGMA wal_autocheckpoint=0")  # Disable auto-checkpoint to prevent exclusive locks

        # Track batches for periodic checkpointing
        batch_count = 0
        checkpoint_interval = 50  # Checkpoint every 50 batches

        try:
            while self._is_running:
                try:
                    # Get batch of vehicle position writes from queue (with timeout)
                    batch = []
                    try:
                        # Get first item (blocking with timeout)
                        first_item = await asyncio.wait_for(
                            self._write_queue.get(),
                            timeout=0.5  # Reduced from 1.0 to process stops more frequently
                        )
                        batch.append(first_item)

                        # Get more items without blocking (drain queue)
                        while not self._write_queue.empty() and len(batch) < 500:
                            try:
                                item = self._write_queue.get_nowait()
                                batch.append(item)
                            except asyncio.QueueEmpty:
                                break

                    except asyncio.TimeoutError:
                        # No vehicle positions, but continue to process stop writes
                        pass

                    # Process vehicle positions batch
                    if batch:
                        await self._process_write_batch(write_conn, batch)
                        batch_count += 1

                    # CRITICAL: Always process stop writes, even if no vehicle positions
                    # This prevents stop queue from filling up when vehicle queue is empty
                    stop_writes = []
                    while not self._stop_write_queue.empty() and len(stop_writes) < 200:  # Increased from 50 to 200
                        try:
                            stop_writes.append(self._stop_write_queue.get_nowait())
                        except asyncio.QueueEmpty:
                            break

                    if stop_writes:
                        await self._process_stop_writes(write_conn, stop_writes)
                        batch_count += 1

                    # CRITICAL: Periodic WAL checkpoint to prevent WAL file from growing indefinitely
                    # Without this, all data stays in the WAL file and never moves to main database
                    if batch_count >= checkpoint_interval:
                        try:
                            # Use PASSIVE checkpoint (non-blocking, doesn't interfere with readers)
                            await write_conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                            logger.debug(f"WAL checkpoint completed after {batch_count} batches")
                            batch_count = 0
                        except Exception as e:
                            logger.warning(f"WAL checkpoint failed: {e}")
                            # Continue operation even if checkpoint fails

                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"Error in background writer: {e}")
                    await asyncio.sleep(1)  # Back off on error

        finally:
            # Final checkpoint before closing
            try:
                await write_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                logger.info("Final WAL checkpoint completed")
            except Exception as e:
                logger.warning(f"Final checkpoint failed: {e}")

            await write_conn.close()
            logger.info("Background database writer stopped")

    async def _process_write_batch(self, conn: aiosqlite.Connection, batch: List[Dict[str, Any]]):
        """Process a batch of queued writes"""
        try:
            # Prepare batch data
            values = [(
                item.get("vehicle_id"),
                item.get("trip_id"),
                item.get("route_id"),
                item.get("lat"),
                item.get("lon"),
                item.get("bearing"),
                item.get("timestamp"),
                item.get("speed"),
                item.get("status"),
            ) for item in batch]

            # Single executemany + commit
            await conn.executemany('''
                INSERT OR IGNORE INTO vehicle_positions (
                    vehicle_id, trip_id, route_id, lat, lon, bearing, timestamp, speed, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', values)

            await conn.commit()
            logger.debug(f"Background writer: inserted {len(batch)} positions")

        except Exception as e:
            logger.error(f"Error processing write batch: {e}")

    async def _process_stop_writes(self, conn: aiosqlite.Connection, stops: List[Dict[str, Any]]):
        """Process a batch of completed stop writes"""
        try:
            # Prepare batch data
            values = [(
                stop.get("stop_id"),
                stop.get("trip_id"),
                stop.get("route_id"),
                stop.get("date"),
                stop.get("actual_arrival"),
                stop.get("actual_departure"),
                stop.get("scheduled_arrival"),
                stop.get("scheduled_departure"),
            ) for stop in stops]

            # Single executemany + commit
            await conn.executemany('''
                INSERT OR IGNORE INTO completed_stop_times (
                    stop_id, trip_id, route_id, date,
                    actual_arrival, actual_departure,
                    scheduled_arrival, scheduled_departure
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', values)

            await conn.commit()
            logger.debug(f"Background writer: inserted {len(stops)} completed stops")

        except Exception as e:
            logger.error(f"Error processing stop writes: {e}")

    async def checkpoint_wal(self):
        """Checkpoint WAL file to main database (periodic maintenance)"""
        conn = await self._get_connection()
        try:
            await conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            logger.debug("WAL checkpoint completed")
        except Exception as e:
            logger.error(f"Error during WAL checkpoint: {e}")
        finally:
            await self._return_connection(conn)

    async def log_completed_trip_stop(self, stop_data: Dict[str, Any]):
        """
        Queue completed stop data for background writing (fire-and-forget).

        This method returns immediately without blocking.
        """
        try:
            self._stop_write_queue.put_nowait(stop_data)
            logger.debug(f"Queued stop {stop_data.get('stop_id')} for background write")
        except asyncio.QueueFull:
            self._dropped_stop_writes += 1
            logger.warning(f"Stop write queue full, dropped stop {stop_data.get('stop_id')} (total dropped: {self._dropped_stop_writes})")

    async def log_vehicle_position(self, position_data: Dict[str, Any]):
        """Log vehicle position for ongoing trips"""
        conn = await self._get_connection()
        try:
            await conn.execute('''
                INSERT OR IGNORE INTO vehicle_positions (
                    vehicle_id, trip_id, route_id, lat, lon, bearing, timestamp, speed, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                position_data.get("vehicle_id"),
                position_data.get("trip_id"),
                position_data.get("route_id"),
                position_data.get("lat"),
                position_data.get("lon"),
                position_data.get("bearing"),
                position_data.get("timestamp"),
                position_data.get("speed"),
                position_data.get("status"),
            ))
            await conn.commit()
        except Exception as e:
            logger.error(f"Error inserting vehicle position for vehicle_id={position_data.get('vehicle_id')}, trip_id={position_data.get('trip_id')}: {e}")
        finally:
            await self._return_connection(conn)

    async def batch_insert_positions(self, positions: List[Dict[str, Any]]):
        """
        Queue vehicle positions for background writing (fire-and-forget).

        This method returns immediately without blocking, preventing database
        operations from slowing down the main event loop.
        """
        if not positions:
            return

        # Queue writes for background processing (non-blocking)
        queued = 0
        for pos in positions:
            try:
                self._write_queue.put_nowait(pos)
                queued += 1
            except asyncio.QueueFull:
                # Queue is full, drop write to prevent backpressure
                self._dropped_writes += 1

        if queued > 0:
            logger.debug(f"Queued {queued} positions for background write (dropped: {self._dropped_writes} total)")

        # Fire-and-forget: returns immediately

    async def get_recent_vehicle_positions(self, vehicle_id: str, route_id: str, limit: int = 500, max_age_hours: int = 6) -> List[Dict[str, Any]]:
        """Retrieve recent vehicle positions from SQLite (read-only operation)

        Args:
            vehicle_id: The vehicle ID to search for
            route_id: The route_id to filter by
            limit: Maximum number of positions to return
            max_age_hours: Only return positions within this many hours

        Returns:
            List of position dictionaries, ordered by timestamp descending (most recent first)
            Returns empty list on timeout/error to prevent blocking
        """
        # Use dedicated read-only connection (WAL mode allows unlimited readers!)
        conn = None
        try:
            # Open read-only connection (doesn't use pool, never blocks writers)
            conn = await aiosqlite.connect(f"file:{self.db_path}?mode=ro", uri=True)

            cutoff_timestamp = int((datetime.now() - timedelta(hours=max_age_hours)).timestamp())

            conn.row_factory = aiosqlite.Row

            # Execute with realistic timeout (queries can take 150-250ms in production)
            cursor = await asyncio.wait_for(
                conn.execute('''
                    SELECT vehicle_id, trip_id, route_id, lat, lon, bearing, timestamp, speed, status
                    FROM vehicle_positions
                    WHERE vehicle_id = ? AND route_id = ? AND timestamp >= ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                ''', (vehicle_id, route_id, cutoff_timestamp, limit)),
                timeout=1.0
            )

            rows = await asyncio.wait_for(cursor.fetchall(), timeout=1.0)

            # Optimized: Convert rows to dictionaries using list comprehension
            positions = [dict(row) for row in rows]

            logger.debug(f"Retrieved {len(positions)} positions from SQLite for vehicle {vehicle_id} on route {route_id}")
            return positions

        except asyncio.TimeoutError:
            logger.warning(f"Query timeout for vehicle {vehicle_id}")
            return []
        except Exception as e:
            logger.error(f"Error retrieving vehicle positions for vehicle_id={vehicle_id}, route_id={route_id}: {e}")
            return []
        finally:
            # Close read-only connection (NOT pooled)
            if conn is not None:
                await conn.close()

    async def get_positions_by_vehicle(self, vehicle_id: str, route_id: str, limit: int = 1000, max_age_hours: int = 8) -> List[Dict[str, Any]]:
        """Retrieve vehicle positions for a specific vehicle from SQLite (read-only operation)

        This is the preferred method for querying positions as it filters by vehicle_id rather than trip_id.
        Trip IDs are often not updated promptly when vehicles change trips.

        Args:
            vehicle_id: The vehicle ID to search for
            route_id: The route ID to filter by
            limit: Maximum number of positions to return
            max_age_hours: Only return positions within this many hours (default: 8, matching in-memory max_age)

        Returns:
            List of position dictionaries, ordered by timestamp ascending (oldest first)
            Returns empty list on timeout/error to prevent blocking
        """
        conn = None  # Initialize to None to prevent leak
        try:
            # CRITICAL: Use read-only connection (unlimited concurrent readers with WAL mode!)
            # Read-only connections are NEVER blocked by writers or checkpoints
            conn = await aiosqlite.connect(f"file:{self.db_path}?mode=ro", uri=True)

            cutoff_timestamp = int((datetime.now() - timedelta(hours=max_age_hours)).timestamp())

            conn.row_factory = aiosqlite.Row

            # Execute with realistic timeout (queries can take 150-250ms in production)
            cursor = await asyncio.wait_for(
                conn.execute('''
                    SELECT vehicle_id, trip_id, route_id, lat, lon, bearing, timestamp, speed, status
                    FROM vehicle_positions
                    WHERE vehicle_id = ? AND route_id = ? AND timestamp >= ?
                    ORDER BY timestamp ASC
                    LIMIT ?
                ''', (vehicle_id, route_id, cutoff_timestamp, limit)),
                timeout=1.0
            )

            rows = await asyncio.wait_for(cursor.fetchall(), timeout=1.0)

            # Optimized: Convert rows to dictionaries using list comprehension
            positions = [dict(row) for row in rows]

            logger.debug(f"Retrieved {len(positions)} positions from SQLite for vehicle {vehicle_id} on route {route_id}")
            return positions

        except asyncio.TimeoutError:
            logger.debug(f"Query timeout for vehicle {vehicle_id} (returning empty list)")
            return []
        except Exception as e:
            logger.error(f"Error retrieving vehicle positions for vehicle_id={vehicle_id}, route_id={route_id}: {e}")
            return []
        finally:
            # Close read-only connection (NOT returned to pool - read-only connections are unlimited!)
            if conn is not None:
                await conn.close()

    async def get_positions_by_trip(self, trip_id: str, route_id: str, limit: int = 1000, max_age_hours: int = 8) -> List[Dict[str, Any]]:
        """Retrieve vehicle positions for a specific trip from SQLite (read-only operation)

        DEPRECATED: Prefer get_positions_by_vehicle() as trip IDs are not updated promptly.
        This method is kept for backward compatibility.

        Args:
            trip_id: The trip ID to search for
            route_id: The route ID to filter by
            limit: Maximum number of positions to return
            max_age_hours: Only return positions within this many hours (default: 8, matching in-memory max_age)

        Returns:
            List of position dictionaries, ordered by timestamp ascending (oldest first)
            Returns empty list on timeout/error to prevent blocking
        """
        conn = None  # Initialize to None to prevent leak
        try:
            # CRITICAL: Use read-only connection (unlimited concurrent readers with WAL mode!)
            # Read-only connections are NEVER blocked by writers or checkpoints
            conn = await aiosqlite.connect(f"file:{self.db_path}?mode=ro", uri=True)

            cutoff_timestamp = int((datetime.now() - timedelta(hours=max_age_hours)).timestamp())

            conn.row_factory = aiosqlite.Row

            # Execute with realistic timeout (queries can take 150-250ms in production)
            cursor = await asyncio.wait_for(
                conn.execute('''
                    SELECT vehicle_id, trip_id, route_id, lat, lon, bearing, timestamp, speed, status
                    FROM vehicle_positions
                    WHERE trip_id = ? AND route_id = ? AND timestamp >= ?
                    ORDER BY timestamp ASC
                    LIMIT ?
                ''', (trip_id, route_id, cutoff_timestamp, limit)),
                timeout=1.0
            )

            rows = await asyncio.wait_for(cursor.fetchall(), timeout=1.0)

            # Optimized: Convert rows to dictionaries using list comprehension
            positions = [dict(row) for row in rows]

            logger.debug(f"Retrieved {len(positions)} positions from SQLite for trip {trip_id} on route {route_id}")
            return positions

        except asyncio.TimeoutError:
            logger.debug(f"Query timeout for trip {trip_id} (returning empty list)")
            return []
        except Exception as e:
            logger.error(f"Error retrieving vehicle positions for trip_id={trip_id}, route_id={route_id}: {e}")
            return []
        finally:
            # Close read-only connection (NOT returned to pool - read-only connections are unlimited!)
            if conn is not None:
                await conn.close()

    async def cleanup_old_data(self, retention_days: int = 365):
        """Cleanup old vehicle positions and trip data"""
        cutoff_date = (datetime.now() - timedelta(days=retention_days)).strftime('%Y-%m-%d')
        cutoff_timestamp = int((datetime.now() - timedelta(days=retention_days)).timestamp())

        conn = await self._get_connection()
        try:
            # Clean old completed stop times
            result1 = await conn.execute('''
                DELETE FROM completed_stop_times
                WHERE date < ?
            ''', (cutoff_date,))

            # Clean old vehicle positions
            result2 = await conn.execute('''
                DELETE FROM vehicle_positions
                WHERE timestamp < ?
            ''', (cutoff_timestamp,))

            await conn.commit()

            logger.info(f"Cleaned up old data: {result1.rowcount} completed stops, {result2.rowcount} vehicle positions")

        except Exception as e:
            logger.error(f"Error cleaning up old data: {e}")
        finally:
            await self._return_connection(conn)
