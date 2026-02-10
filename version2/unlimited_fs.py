#!/usr/bin/env python3
"""
Virtual Filesystem for Unlimited Distributed Storage.

Creates a FUSE filesystem that:
- Mounts on SD card
- Appears as normal filesystem to applications
- Actually stores data across distributed network
- Small local cache on SD card
- Transparent to user - looks like infinite storage
"""
import os
import sys
import time
import errno
import logging
from pathlib import Path
from typing import Dict, Optional
import threading

# Try to import FUSE
try:
    from fuse import FUSE, FuseOSError, Operations, LoggingMixIn
    FUSE_AVAILABLE = True
except ImportError:
    FUSE_AVAILABLE = False
    logging.warning("FUSE not available - install python-fuse")
    # Create dummy classes for type hints
    class Operations:
        pass
    class LoggingMixIn:
        pass

from distributed_store import DistributedStore

class UnlimitedFS(LoggingMixIn, Operations if FUSE_AVAILABLE else object):
    """
    FUSE filesystem providing unlimited storage via distribution.
    
    Architecture:
    - Local cache on SD card (actual card size)
    - Distributed storage across network (unlimited)
    - Transparent caching and eviction
    - Applications see normal filesystem
    - All I/O intercepted and distributed
    """
    
    def __init__(
        self,
        distributed_store: DistributedStore,
        cache_dir: Path,
        cache_size_mb: int = 1024
    ):
        self.dist_store = distributed_store
        self.cache_dir = cache_dir
        self.cache_size_mb = cache_size_mb
        
        # Create cache directory
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        
        # Virtual filesystem metadata
        self.files: Dict[str, dict] = {}  # path -> {size, address, mode, atime, mtime, ctime}
        self.directories: Dict[str, dict] = {}  # path -> {mode, atime, mtime, ctime}
        self._fs_lock = threading.RLock()
        
        # Initialize root directory
        self.directories['/'] = {
            'mode': 0o755 | 0o040000,  # drwxr-xr-x
            'atime': time.time(),
            'mtime': time.time(),
            'ctime': time.time(),
            'uid': os.getuid(),
            'gid': os.getgid()
        }
        
        # File handle counter
        self._fh_counter = 0
        self._fh_lock = threading.Lock()
        
        # Open file handles
        self._open_files: Dict[int, dict] = {}  # fh -> {path, address, cache_file, mode}
        
        # Cache management
        self._cache_size = 0
        self._cache_lock = threading.Lock()
        
        # Statistics
        self.stats = {
            'read_hits': 0,
            'read_misses': 0,
            'writes': 0,
            'cache_evictions': 0,
            'network_fetches': 0
        }
        self._stats_lock = threading.Lock()
    
    def _get_fh(self) -> int:
        """Get unique file handle"""
        with self._fh_lock:
            self._fh_counter += 1
            return self._fh_counter
    
    def _split_path(self, path: str) -> tuple:
        """Split path into directory and filename"""
        if path == '/':
            return ('/', '')
        
        parts = path.rstrip('/').split('/')
        filename = parts[-1]
        directory = '/'.join(parts[:-1]) or '/'
        
        return (directory, filename)
    
    # FUSE Operations
    
    def getattr(self, path: str, fh=None):
        """Get file attributes"""
        with self._fs_lock:
            # Check if it's a directory
            if path in self.directories:
                dir_info = self.directories[path]
                return {
                    'st_mode': dir_info['mode'],
                    'st_nlink': 2,
                    'st_size': 4096,
                    'st_atime': dir_info['atime'],
                    'st_mtime': dir_info['mtime'],
                    'st_ctime': dir_info['ctime'],
                    'st_uid': dir_info.get('uid', os.getuid()),
                    'st_gid': dir_info.get('gid', os.getgid())
                }
            
            # Check if it's a file
            if path in self.files:
                file_info = self.files[path]
                return {
                    'st_mode': file_info['mode'],
                    'st_nlink': 1,
                    'st_size': file_info['size'],
                    'st_atime': file_info['atime'],
                    'st_mtime': file_info['mtime'],
                    'st_ctime': file_info['ctime'],
                    'st_uid': file_info.get('uid', os.getuid()),
                    'st_gid': file_info.get('gid', os.getgid())
                }
            
            raise FuseOSError(errno.ENOENT)
    
    def readdir(self, path: str, fh):
        """Read directory contents"""
        with self._fs_lock:
            if path not in self.directories:
                raise FuseOSError(errno.ENOENT)
            
            entries = ['.', '..']
            
            # Add subdirectories
            for dir_path in self.directories:
                if dir_path == path:
                    continue
                
                dir_parent, dir_name = self._split_path(dir_path)
                if dir_parent == path:
                    entries.append(dir_name)
            
            # Add files
            for file_path in self.files:
                file_parent, file_name = self._split_path(file_path)
                if file_parent == path:
                    entries.append(file_name)
            
            return entries
    
    def mkdir(self, path: str, mode):
        """Create directory"""
        with self._fs_lock:
            if path in self.directories or path in self.files:
                raise FuseOSError(errno.EEXIST)
            
            # Check parent exists
            parent, _ = self._split_path(path)
            if parent not in self.directories:
                raise FuseOSError(errno.ENOENT)
            
            self.directories[path] = {
                'mode': mode | 0o040000,  # Directory bit
                'atime': time.time(),
                'mtime': time.time(),
                'ctime': time.time(),
                'uid': os.getuid(),
                'gid': os.getgid()
            }
    
    def rmdir(self, path: str):
        """Remove directory"""
        with self._fs_lock:
            if path not in self.directories:
                raise FuseOSError(errno.ENOENT)
            
            # Check directory is empty
            for dir_path in self.directories:
                parent, _ = self._split_path(dir_path)
                if parent == path:
                    raise FuseOSError(errno.ENOTEMPTY)
            
            for file_path in self.files:
                parent, _ = self._split_path(file_path)
                if parent == path:
                    raise FuseOSError(errno.ENOTEMPTY)
            
            del self.directories[path]
    
    def create(self, path: str, mode):
        """Create file"""
        with self._fs_lock:
            if path in self.files or path in self.directories:
                raise FuseOSError(errno.EEXIST)
            
            # Check parent exists
            parent, _ = self._split_path(path)
            if parent not in self.directories:
                raise FuseOSError(errno.ENOENT)
            
            # Create file metadata
            self.files[path] = {
                'mode': mode | 0o100000,  # Regular file bit
                'size': 0,
                'address': None,
                'atime': time.time(),
                'mtime': time.time(),
                'ctime': time.time(),
                'uid': os.getuid(),
                'gid': os.getgid()
            }
            
            # Return file handle
            fh = self._get_fh()
            self._open_files[fh] = {
                'path': path,
                'address': None,
                'cache_file': None,
                'mode': 'w'
            }
            
            return fh
    
    def open(self, path: str, flags):
        """Open file"""
        with self._fs_lock:
            if path not in self.files:
                raise FuseOSError(errno.ENOENT)
            
            file_info = self.files[path]
            
            fh = self._get_fh()
            self._open_files[fh] = {
                'path': path,
                'address': file_info.get('address'),
                'cache_file': None,
                'mode': 'r' if flags & os.O_WRONLY == 0 else 'w'
            }
            
            return fh
    
    def read(self, path: str, size: int, offset: int, fh):
        """Read from file"""
        if fh not in self._open_files:
            raise FuseOSError(errno.EBADF)
        
        file_handle = self._open_files[fh]
        address = file_handle['address']
        
        if address is None:
            return b''
        
        # Try to read from cache first
        cache_file = self.cache_dir / f"{address}.cache"
        
        if cache_file.exists():
            with self._stats_lock:
                self.stats['read_hits'] += 1
            
            with open(cache_file, 'rb') as f:
                f.seek(offset)
                return f.read(size)
        
        # Cache miss - fetch from distributed storage
        with self._stats_lock:
            self.stats['read_misses'] += 1
            self.stats['network_fetches'] += 1
        
        try:
            data = self.dist_store.retrieve(address, try_remote=True)
            
            # Cache the data
            with open(cache_file, 'wb') as f:
                f.write(data)
            
            # Update cache size
            with self._cache_lock:
                self._cache_size += len(data)
                self._evict_if_needed()
            
            # Return requested slice
            return data[offset:offset+size]
        
        except Exception as e:
            logging.error(f"Failed to read from distributed storage: {e}")
            raise FuseOSError(errno.EIO)
    
    def write(self, path: str, data: bytes, offset: int, fh):
        """Write to file"""
        if fh not in self._open_files:
            raise FuseOSError(errno.EBADF)
        
        file_handle = self._open_files[fh]
        
        # Write to temporary cache file
        if not file_handle['cache_file']:
            cache_file = self.cache_dir / f"temp_{fh}.cache"
            file_handle['cache_file'] = cache_file
            cache_file.touch()
        
        cache_file = file_handle['cache_file']
        
        # Write data
        with open(cache_file, 'r+b') as f:
            f.seek(offset)
            f.write(data)
        
        with self._stats_lock:
            self.stats['writes'] += 1
        
        return len(data)
    
    def release(self, path: str, fh):
        """Close file"""
        if fh not in self._open_files:
            return
        
        file_handle = self._open_files[fh]
        
        # If file was written, store to distributed storage
        if file_handle['mode'] == 'w' and file_handle['cache_file']:
            cache_file = file_handle['cache_file']
            
            if cache_file.exists():
                # Read data
                data = cache_file.read_bytes()
                
                # Store to distributed storage
                address = self.dist_store.store(data, replicate=True)
                
                # Update file metadata
                with self._fs_lock:
                    if path in self.files:
                        self.files[path]['address'] = address
                        self.files[path]['size'] = len(data)
                        self.files[path]['mtime'] = time.time()
                
                # Move to permanent cache
                perm_cache = self.cache_dir / f"{address}.cache"
                cache_file.rename(perm_cache)
                
                # Update cache size
                with self._cache_lock:
                    self._cache_size += len(data)
                    self._evict_if_needed()
        
        del self._open_files[fh]
    
    def unlink(self, path: str):
        """Delete file"""
        with self._fs_lock:
            if path not in self.files:
                raise FuseOSError(errno.ENOENT)
            
            file_info = self.files[path]
            address = file_info.get('address')
            
            # Remove from distributed storage
            if address is not None:
                try:
                    self.dist_store.local_store.delete(address)
                except:
                    pass
                
                # Remove from cache
                cache_file = self.cache_dir / f"{address}.cache"
                if cache_file.exists():
                    size = cache_file.stat().st_size
                    cache_file.unlink()
                    
                    with self._cache_lock:
                        self._cache_size -= size
            
            del self.files[path]
    
    def _evict_if_needed(self):
        """Evict cache entries if over limit"""
        cache_limit = self.cache_size_mb * 1024 * 1024
        
        while self._cache_size > cache_limit:
            # Find oldest cached file
            oldest_file = None
            oldest_time = float('inf')
            
            for cache_file in self.cache_dir.glob("*.cache"):
                mtime = cache_file.stat().st_mtime
                if mtime < oldest_time:
                    oldest_time = mtime
                    oldest_file = cache_file
            
            if oldest_file:
                size = oldest_file.stat().st_size
                oldest_file.unlink()
                self._cache_size -= size
                
                with self._stats_lock:
                    self.stats['cache_evictions'] += 1
            else:
                break
    
    def statfs(self, path):
        """Get filesystem statistics - report as unlimited"""
        # Report virtually unlimited storage
        return {
            'f_bsize': 4096,
            'f_frsize': 4096,
            'f_blocks': 2**40,  # 4 PB
            'f_bfree': 2**40,   # All free
            'f_bavail': 2**40,  # All available
            'f_files': 2**32,   # Lots of files
            'f_ffree': 2**32,   # Lots of free inodes
            'f_favail': 2**32,
            'f_namemax': 255
        }

def mount_unlimited_fs(
    mount_point: Path,
    distributed_store: DistributedStore,
    cache_dir: Path,
    cache_size_mb: int = 1024,
    foreground: bool = False
):
    """
    Mount the unlimited filesystem.
    
    Args:
        mount_point: Where to mount the filesystem
        distributed_store: Distributed storage backend
        cache_dir: Local cache directory on SD card
        cache_size_mb: Maximum cache size in MB
        foreground: Run in foreground (for debugging)
    """
    if not FUSE_AVAILABLE:
        logging.error("FUSE not available - cannot mount filesystem")
        return False
    
    # Create mount point
    mount_point.mkdir(parents=True, exist_ok=True)
    
    # Create filesystem
    fs = UnlimitedFS(distributed_store, cache_dir, cache_size_mb)
    
    logging.info(f"Mounting unlimited filesystem at {mount_point}")
    logging.info(f"  Cache directory: {cache_dir}")
    logging.info(f"  Cache size: {cache_size_mb} MB")
    
    # Mount
    FUSE(
        fs,
        str(mount_point),
        foreground=foreground,
        allow_other=True,
        default_permissions=True
    )
    
    return True

# Example usage
if __name__ == "__main__":
    import argparse
    import tempfile
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    
    parser = argparse.ArgumentParser(description="Unlimited Filesystem")
    parser.add_argument('mount_point', help='Mount point')
    parser.add_argument('--cache-dir', default='/tmp/unlimitedfs_cache', help='Cache directory')
    parser.add_argument('--cache-size', type=int, default=1024, help='Cache size in MB')
    parser.add_argument('--foreground', action='store_true', help='Run in foreground')
    parser.add_argument('--store-path', default='/tmp/unlimited_store', help='Distributed store path')
    parser.add_argument('--port', type=int, default=6000, help='Network port')
    
    args = parser.parse_args()
    
    if not FUSE_AVAILABLE:
        print("Error: FUSE not available. Install with: pip install fusepy")
        sys.exit(1)
    
    # Create distributed store
    from distributed_store import DistributedStore
    
    store = DistributedStore(
        Path(args.store_path),
        "unlimited_fs_node",
        "127.0.0.1",
        args.port,
        auto_replicate=True
    )
    
    store.start()
    
    try:
        mount_unlimited_fs(
            Path(args.mount_point),
            store,
            Path(args.cache_dir),
            args.cache_size,
            args.foreground
        )
    except KeyboardInterrupt:
        print("\nUnmounting...")
    finally:
        store.stop()
