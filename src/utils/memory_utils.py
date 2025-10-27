"""
Memory management utilities for the new architecture.
Provides bounded data structures with TTL and memory limits to prevent memory accumulation.
"""

import gc
import sys
import time
import weakref
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List, Iterator
from collections import OrderedDict
import threading


class BoundedDict:
    """Dictionary with automatic memory management, TTL, and size limits"""
    
    def __init__(self, max_memory_mb: int = 10, ttl_seconds: int = 3600, max_items: int = 1000, name: str = "BoundedDict"):
        self.max_memory_mb = max_memory_mb
        self.ttl_seconds = ttl_seconds
        self.max_items = max_items
        self.name = name
        
        # Use OrderedDict for LRU functionality
        self._data: OrderedDict[str, Any] = OrderedDict()
        self._timestamps: Dict[str, datetime] = {}
        self._lock = threading.RLock()  # Allow recursive locks
        
        # Memory tracking
        self._estimated_size_bytes = 0
        self._last_cleanup = time.time()
        self._cleanup_interval = 60  # Cleanup every minute
        
    def __setitem__(self, key: str, value: Any):
        """Set item with automatic cleanup if needed"""
        with self._lock:
            now = datetime.now()
            
            # Remove old value if exists
            if key in self._data:
                self._remove_item_internal(key)
            
            # Add new value
            self._data[key] = value
            self._timestamps[key] = now
            
            # Estimate memory usage (rough approximation)
            item_size = self._estimate_size(key, value)
            self._estimated_size_bytes += item_size
            
            # Trigger cleanup if needed
            self._cleanup_if_needed()
    
    def __getitem__(self, key: str) -> Any:
        """Get item and move to end (LRU)"""
        with self._lock:
            if key not in self._data:
                raise KeyError(key)
            
            # Check if expired
            if self._is_expired(key):
                self._remove_item_internal(key)
                raise KeyError(key)
            
            # Move to end for LRU
            value = self._data[key]
            self._data.move_to_end(key)
            return value
    
    def get(self, key: str, default: Any = None) -> Any:
        """Get item with default value"""
        try:
            return self[key]
        except KeyError:
            return default
    
    def __contains__(self, key: str) -> bool:
        """Check if key exists and is not expired"""
        with self._lock:
            if key not in self._data:
                return False
            
            if self._is_expired(key):
                self._remove_item_internal(key)
                return False
            
            return True
    
    def __delitem__(self, key: str):
        """Delete item"""
        with self._lock:
            if key not in self._data:
                raise KeyError(key)
            self._remove_item_internal(key)
    
    def __len__(self) -> int:
        """Get number of items (after cleanup)"""
        with self._lock:
            self._cleanup_expired()
            return len(self._data)
    
    def __iter__(self) -> Iterator[str]:
        """Iterate over keys"""
        with self._lock:
            self._cleanup_expired()
            return iter(list(self._data.keys()))  # Copy to avoid modification during iteration
    
    def items(self) -> Iterator[tuple[str, Any]]:
        """Iterate over items"""
        with self._lock:
            self._cleanup_expired()
            return iter(list(self._data.items()))  # Copy to avoid modification during iteration
    
    def keys(self) -> Iterator[str]:
        """Iterate over keys"""
        return self.__iter__()
    
    def values(self) -> Iterator[Any]:
        """Iterate over values"""
        with self._lock:
            self._cleanup_expired()
            return iter(list(self._data.values()))  # Copy to avoid modification during iteration
    
    def clear(self):
        """Clear all items"""
        with self._lock:
            self._data.clear()
            self._timestamps.clear()
            self._estimated_size_bytes = 0
    
    def cleanup_expired(self):
        """Manually trigger cleanup of expired items"""
        with self._lock:
            self._cleanup_expired()
    
    def _is_expired(self, key: str) -> bool:
        """Check if item is expired"""
        if key not in self._timestamps:
            return True
        
        age = (datetime.now() - self._timestamps[key]).total_seconds()
        return age > self.ttl_seconds
    
    def _remove_item_internal(self, key: str):
        """Remove item and update size tracking"""
        if key in self._data:
            # Estimate size reduction
            value = self._data[key]
            item_size = self._estimate_size(key, value)
            self._estimated_size_bytes = max(0, self._estimated_size_bytes - item_size)
            
            # Remove from data structures
            del self._data[key]
            self._timestamps.pop(key, None)
    
    def _cleanup_expired(self):
        """Remove expired items"""
        now = datetime.now()
        expired_keys = []
        
        for key, timestamp in self._timestamps.items():
            if (now - timestamp).total_seconds() > self.ttl_seconds:
                expired_keys.append(key)
        
        for key in expired_keys:
            self._remove_item_internal(key)
    
    def _cleanup_if_needed(self):
        """Cleanup if memory or time limits exceeded"""
        now = time.time()
        
        # Time-based cleanup
        if now - self._last_cleanup > self._cleanup_interval:
            self._cleanup_expired()
            self._last_cleanup = now
        
        # Memory-based cleanup
        memory_mb = self._estimated_size_bytes / (1024 * 1024)
        if memory_mb > self.max_memory_mb:
            self._cleanup_by_memory()
        
        # Item count-based cleanup
        if len(self._data) > self.max_items:
            self._cleanup_by_count()
    
    def _cleanup_by_memory(self):
        """Remove oldest items until memory limit is met"""
        target_bytes = self.max_memory_mb * 1024 * 1024 * 0.8  # Target 80% of limit
        
        while self._estimated_size_bytes > target_bytes and self._data:
            # Remove oldest item (first in OrderedDict)
            oldest_key = next(iter(self._data))
            self._remove_item_internal(oldest_key)
    
    def _cleanup_by_count(self):
        """Remove oldest items until count limit is met"""
        target_count = int(self.max_items * 0.8)  # Target 80% of limit
        
        while len(self._data) > target_count:
            # Remove oldest item (first in OrderedDict)
            oldest_key = next(iter(self._data))
            self._remove_item_internal(oldest_key)
    
    def _estimate_size(self, key: str, value: Any) -> int:
        """Rough estimate of memory usage for key-value pair"""
        try:
            # Basic size estimation
            key_size = sys.getsizeof(key)
            value_size = sys.getsizeof(value)
            
            # Add some overhead for data structures
            overhead = 64  # Rough estimate for dict overhead
            
            return key_size + value_size + overhead
        except:
            # Fallback to conservative estimate
            return 1024  # 1KB default
    
    def get_stats(self) -> Dict[str, Any]:
        """Get statistics about the bounded dict"""
        with self._lock:
            memory_mb = self._estimated_size_bytes / (1024 * 1024)
            
            return {
                "name": self.name,
                "item_count": len(self._data),
                "max_items": self.max_items,
                "memory_usage_mb": round(memory_mb, 2),
                "max_memory_mb": self.max_memory_mb,
                "memory_utilization": round((memory_mb / self.max_memory_mb) * 100, 1),
                "ttl_seconds": self.ttl_seconds,
                "oldest_item_age_seconds": self._get_oldest_item_age(),
                "estimated_size_bytes": self._estimated_size_bytes
            }
    
    def _get_oldest_item_age(self) -> Optional[float]:
        """Get age of oldest item in seconds"""
        if not self._timestamps:
            return None
        
        oldest_time = min(self._timestamps.values())
        return (datetime.now() - oldest_time).total_seconds()


class BoundedCache:
    """Simple cache with TTL and memory limits"""
    
    def __init__(self, max_size_mb: int = 5, ttl_seconds: int = 300):
        self._storage = BoundedDict(
            max_memory_mb=max_size_mb,
            ttl_seconds=ttl_seconds,
            name="BoundedCache"
        )
    
    def get(self, key: str) -> Optional[Any]:
        """Get cached value"""
        return self._storage.get(key)
    
    def set(self, key: str, value: Any, ttl_seconds: Optional[int] = None):
        """Set cached value with optional TTL override"""
        if ttl_seconds is not None:
            # For custom TTL, we store the item with timestamp
            expiry_time = datetime.now() + timedelta(seconds=ttl_seconds)
            wrapped_value = {"value": value, "expires_at": expiry_time}
            self._storage[key] = wrapped_value
        else:
            self._storage[key] = value
    
    def delete(self, key: str):
        """Delete cached value"""
        if key in self._storage:
            del self._storage[key]
    
    def clear(self):
        """Clear all cached values"""
        self._storage.clear()
    
    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics"""
        return self._storage.get_stats()
    #
    # def __setitem__(self, key, value):
    #     return self._storage.__setitem__(key, value)
    #
    # def __iter__(self):
    #     return self._storage.__iter__()
    #
    # def __getitem__(self, key):
    #     return self._storage.__getitem__(key)
    #
    # def __delitem__(self, key):
    #     return self._storage.__delitem__(key)


class MemoryMonitor:
    """Monitor memory usage across the application"""
    
    def __init__(self):
        self._bounded_dicts: List[weakref.ref] = []
        self._monitoring_enabled = True
    
    def register_bounded_dict(self, bounded_dict: BoundedDict):
        """Register a BoundedDict for monitoring"""
        self._bounded_dicts.append(weakref.ref(bounded_dict))
    
    def get_memory_summary(self) -> Dict[str, Any]:
        """Get memory summary across all registered BoundedDicts"""
        total_memory_mb = 0
        total_items = 0
        active_dicts = []
        
        # Clean up dead references
        self._bounded_dicts = [ref for ref in self._bounded_dicts if ref() is not None]
        
        for dict_ref in self._bounded_dicts:
            bounded_dict = dict_ref()
            if bounded_dict:
                stats = bounded_dict.get_stats()
                active_dicts.append(stats)
                total_memory_mb += stats.get("memory_usage_mb", 0)
                total_items += stats.get("item_count", 0)
        
        return {
            "total_memory_mb": round(total_memory_mb, 2),
            "total_items": total_items,
            "active_bounded_dicts": len(active_dicts),
            "details": active_dicts
        }
    
    def trigger_cleanup(self):
        """Trigger cleanup on all registered BoundedDicts"""
        for dict_ref in self._bounded_dicts:
            bounded_dict = dict_ref()
            if bounded_dict:
                bounded_dict.cleanup_expired()
        
        # Force garbage collection
        gc.collect()


# Global memory monitor instance
memory_monitor = MemoryMonitor()


class CircularBuffer:
    """Memory-efficient circular buffer for streaming data"""
    
    def __init__(self, max_size: int = 1000):
        self.max_size = max_size
        self._buffer: List[Any] = []
        self._index = 0
        self._full = False
        self._lock = threading.Lock()
    
    def append(self, item: Any):
        """Add item to buffer"""
        with self._lock:
            if len(self._buffer) < self.max_size:
                self._buffer.append(item)
            else:
                self._buffer[self._index] = item
                self._index = (self._index + 1) % self.max_size
                self._full = True
    
    def get_recent(self, count: int = None) -> List[Any]:
        """Get recent items (most recent first)"""
        with self._lock:
            if not self._buffer:
                return []
            
            if not self._full:
                # Buffer not full, return in reverse order
                result = list(reversed(self._buffer))
            else:
                # Buffer is full, need to handle wraparound
                result = []
                for i in range(len(self._buffer)):
                    idx = (self._index - 1 - i) % self.max_size
                    result.append(self._buffer[idx])
            
            if count is not None:
                result = result[:count]
            
            return result
    
    def clear(self):
        """Clear the buffer"""
        with self._lock:
            self._buffer.clear()
            self._index = 0
            self._full = False
    
    def __len__(self) -> int:
        """Get number of items in buffer"""
        with self._lock:
            return len(self._buffer)
    
    def get_stats(self) -> Dict[str, Any]:
        """Get buffer statistics"""
        with self._lock:
            return {
                "current_size": len(self._buffer),
                "max_size": self.max_size,
                "is_full": self._full,
                "utilization": round((len(self._buffer) / self.max_size) * 100, 1)
            }


def get_memory_usage_mb() -> float:
    """Get current process memory usage in MB"""
    try:
        import psutil
        process = psutil.Process()
        return process.memory_info().rss / (1024 * 1024)
    except ImportError:
        # Fallback to basic estimation
        return 0.0


def force_garbage_collection():
    """Force garbage collection and return collected objects count"""
    return gc.collect()


def optimize_memory_settings(target_memory_mb: int = 200) -> Dict[str, int]:
    """Calculate optimal memory settings for components"""
    # Reserve memory for system and other components
    available_memory = target_memory_mb * 0.8  # Use 80% of target
    
    return {
        "live_data_repo_mb": int(available_memory * 0.4),  # 40% for live data
        "api_cache_mb": int(available_memory * 0.2),       # 20% for API cache
        "static_cache_mb": int(available_memory * 0.2),    # 20% for static data cache
        "processing_buffer_mb": int(available_memory * 0.1), # 10% for processing
        "general_cache_mb": int(available_memory * 0.1)    # 10% for general use
    }
