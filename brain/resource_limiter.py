"""Resource limiter for daemon mode.
Limits CPU and memory usage to prevent system overload.
"""
from __future__ import annotations

import os
import psutil
import time
from contextlib import contextmanager
from typing import Generator


class ResourceLimiter:
    """Limits CPU and memory usage for background tasks."""
    
    def __init__(
        self,
        cpu_percent: float = 50.0,
        memory_mb: int = 512,
        check_interval: float = 1.0
    ):
        self.cpu_percent = cpu_percent
        self.memory_mb = memory_mb
        self.check_interval = check_interval
        self.process = psutil.Process(os.getpid())
        self._last_check = 0.0
        self._sleep_time = 0.1
    
    def should_throttle(self) -> bool:
        """Check if we should throttle execution."""
        now = time.time()
        if now - self._last_check < self.check_interval:
            return False
        
        self._last_check = now
        
        # Check CPU usage
        cpu = self.process.cpu_percent(interval=0.1)
        if cpu > self.cpu_percent:
            return True
        
        # Check memory usage
        mem = self.process.memory_info().rss / (1024 * 1024)  # MB
        if mem > self.memory_mb:
            return True
        
        return False
    
    def throttle(self) -> None:
        """Sleep to reduce resource usage."""
        time.sleep(self._sleep_time)
    
    @contextmanager
    def limit(self) -> Generator[None, None, None]:
        """Context manager for resource-limited execution."""
        while True:
            if not self.should_throttle():
                break
            self.throttle()
        
        try:
            yield
        finally:
            pass
    
    def set_cpu_limit(self, percent: float) -> None:
        """Update CPU limit."""
        self.cpu_percent = percent
    
    def set_memory_limit(self, mb: int) -> None:
        """Update memory limit."""
        self.memory_mb = mb
    
    def get_usage(self) -> dict:
        """Get current resource usage."""
        return {
            'cpu_percent': self.process.cpu_percent(interval=0.1),
            'memory_mb': self.process.memory_info().rss / (1024 * 1024),
            'threads': self.process.num_threads()
        }


def create_limiter(
    cpu_percent: float = 50.0,
    memory_mb: int = 512
) -> ResourceLimiter:
    """Factory function to create a resource limiter."""
    return ResourceLimiter(cpu_percent, memory_mb)


if __name__ == '__main__':
    # Test the limiter
    limiter = ResourceLimiter(cpu_percent=50.0, memory_mb=512)
    
    print("Starting resource limiter test...")
    print(f"CPU limit: {limiter.cpu_percent}%")
    print(f"Memory limit: {limiter.memory_mb} MB")
    
    for i in range(10):
        with limiter.limit():
            usage = limiter.get_usage()
            print(f"Iteration {i+1}: CPU={usage['cpu_percent']:.1f}%, "
                  f"Memory={usage['memory_mb']:.1f} MB")
            time.sleep(1)