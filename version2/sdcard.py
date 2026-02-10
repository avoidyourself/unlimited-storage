#!/usr/bin/env python3
"""
SD Card Detection and Management System.
Handles automatic detection, mounting, formatting, and monitoring of SD cards.
"""
import os
import sys
import time
import subprocess
import threading
import json
from pathlib import Path
from typing import Optional, List, Dict, Callable
from dataclasses import dataclass, asdict
import logging

# Try to import pyudev for device monitoring
try:
    import pyudev
    PYUDEV_AVAILABLE = True
except ImportError:
    PYUDEV_AVAILABLE = False
    logging.warning("pyudev not available - using fallback polling")

@dataclass
class SDCardInfo:
    """Information about an SD card"""
    device_path: str  # /dev/sdb1
    device_name: str  # sdb1
    mount_point: Optional[str]
    size_bytes: int
    filesystem: str
    label: str
    uuid: str
    vendor: str
    model: str
    is_mounted: bool
    is_writable: bool
    
    def to_dict(self) -> dict:
        return asdict(self)
    
    def size_human(self) -> str:
        """Human readable size"""
        size = self.size_bytes
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if size < 1024.0:
                return f"{size:.2f} {unit}"
            size /= 1024.0
        return f"{size:.2f} PB"

class SDCardManager:
    """
    Manages SD card detection, mounting, and monitoring.
    
    Features:
    - Auto-detection of card insertion/removal
    - Automatic mounting with custom filesystem
    - Format cards with our distributed filesystem
    - Real-time event monitoring
    - Safe unmounting
    """
    
    def __init__(
        self,
        mount_base: Path = Path("/mnt/cloudstore"),
        auto_mount: bool = True,
        auto_format: bool = False,
        monitor_interval: float = 2.0
    ):
        self.mount_base = mount_base
        self.auto_mount = auto_mount
        self.auto_format = auto_format
        self.monitor_interval = monitor_interval
        
        # Create mount base directory
        self.mount_base.mkdir(parents=True, exist_ok=True)
        
        # Detected cards
        self.cards: Dict[str, SDCardInfo] = {}
        self._cards_lock = threading.Lock()
        
        # Monitoring
        self._monitor_thread: Optional[threading.Thread] = None
        self._running = False
        
        # Event callbacks
        self.on_card_inserted: Optional[Callable[[SDCardInfo], None]] = None
        self.on_card_removed: Optional[Callable[[str], None]] = None
        self.on_card_mounted: Optional[Callable[[SDCardInfo], None]] = None
        self.on_card_error: Optional[Callable[[str, str], None]] = None
        
        # Statistics
        self.stats = {
            'cards_detected': 0,
            'cards_mounted': 0,
            'cards_formatted': 0,
            'mount_errors': 0,
            'total_capacity': 0
        }
        self._stats_lock = threading.Lock()
    
    def start(self):
        """Start SD card monitoring"""
        if self._running:
            return
        
        self._running = True
        
        # Initial scan
        self._scan_cards()
        
        # Start monitoring thread
        if PYUDEV_AVAILABLE:
            self._monitor_thread = threading.Thread(
                target=self._udev_monitor_loop,
                daemon=True
            )
        else:
            self._monitor_thread = threading.Thread(
                target=self._poll_monitor_loop,
                daemon=True
            )
        
        self._monitor_thread.start()
        
        logging.info("SD card monitoring started")
    
    def stop(self):
        """Stop monitoring and unmount all cards"""
        self._running = False
        
        if self._monitor_thread:
            self._monitor_thread.join(timeout=5.0)
        
        # Unmount all cards
        with self._cards_lock:
            for device_name in list(self.cards.keys()):
                self.unmount_card(device_name)
        
        logging.info("SD card monitoring stopped")
    
    def _scan_cards(self):
        """Scan for currently connected SD cards"""
        try:
            # Look for removable block devices
            block_devs = Path("/sys/block").glob("sd*")
            
            for dev_path in block_devs:
                # Check if removable
                removable_file = dev_path / "removable"
                if not removable_file.exists():
                    continue
                
                try:
                    is_removable = int(removable_file.read_text().strip())
                    if not is_removable:
                        continue
                except:
                    continue
                
                device_name = dev_path.name
                
                # Look for partitions
                for part_path in dev_path.glob(f"{device_name}*"):
                    if part_path.name == device_name:
                        continue
                    
                    partition_name = part_path.name
                    device_path = f"/dev/{partition_name}"
                    
                    # Get card info
                    card_info = self._get_card_info(device_path)
                    if card_info:
                        self._handle_card_insertion(card_info)
        
        except Exception as e:
            logging.error(f"Error scanning cards: {e}")
    
    def _get_card_info(self, device_path: str) -> Optional[SDCardInfo]:
        """Get information about an SD card"""
        try:
            # Get device name
            device_name = Path(device_path).name
            
            # Get size
            size_bytes = 0
            size_file = Path(f"/sys/block/{device_name[:3]}/{device_name}/size")
            if size_file.exists():
                sectors = int(size_file.read_text().strip())
                size_bytes = sectors * 512  # 512 bytes per sector
            
            # Get filesystem info using blkid
            blkid_output = subprocess.run(
                ['blkid', '-o', 'export', device_path],
                capture_output=True,
                text=True
            )
            
            blkid_info = {}
            if blkid_output.returncode == 0:
                for line in blkid_output.stdout.strip().split('\n'):
                    if '=' in line:
                        key, value = line.split('=', 1)
                        blkid_info[key] = value
            
            # Check if mounted
            mount_point = None
            is_mounted = False
            
            with open('/proc/mounts', 'r') as f:
                for line in f:
                    parts = line.split()
                    if parts[0] == device_path:
                        mount_point = parts[1]
                        is_mounted = True
                        break
            
            # Check if writable
            is_writable = False
            if is_mounted and mount_point:
                test_file = Path(mount_point) / ".write_test"
                try:
                    test_file.touch()
                    test_file.unlink()
                    is_writable = True
                except:
                    pass
            
            return SDCardInfo(
                device_path=device_path,
                device_name=device_name,
                mount_point=mount_point,
                size_bytes=size_bytes,
                filesystem=blkid_info.get('TYPE', 'unknown'),
                label=blkid_info.get('LABEL', ''),
                uuid=blkid_info.get('UUID', ''),
                vendor=blkid_info.get('ID_VENDOR', 'Unknown'),
                model=blkid_info.get('ID_MODEL', 'SD Card'),
                is_mounted=is_mounted,
                is_writable=is_writable
            )
        
        except Exception as e:
            logging.error(f"Error getting card info for {device_path}: {e}")
            return None
    
    def _handle_card_insertion(self, card_info: SDCardInfo):
        """Handle SD card insertion"""
        with self._cards_lock:
            # Check if already detected
            if card_info.device_name in self.cards:
                return
            
            self.cards[card_info.device_name] = card_info
            
            with self._stats_lock:
                self.stats['cards_detected'] += 1
                self.stats['total_capacity'] += card_info.size_bytes
        
        logging.info(f"SD card detected: {card_info.device_name} ({card_info.size_human()})")
        
        # Callback
        if self.on_card_inserted:
            try:
                self.on_card_inserted(card_info)
            except Exception as e:
                logging.error(f"Error in card insertion callback: {e}")
        
        # Auto-mount if enabled
        if self.auto_mount and not card_info.is_mounted:
            self.mount_card(card_info.device_name)
    
    def _handle_card_removal(self, device_name: str):
        """Handle SD card removal"""
        with self._cards_lock:
            if device_name not in self.cards:
                return
            
            card_info = self.cards[device_name]
            del self.cards[device_name]
            
            with self._stats_lock:
                self.stats['total_capacity'] -= card_info.size_bytes
        
        logging.info(f"SD card removed: {device_name}")
        
        # Callback
        if self.on_card_removed:
            try:
                self.on_card_removed(device_name)
            except Exception as e:
                logging.error(f"Error in card removal callback: {e}")
    
    def mount_card(self, device_name: str) -> bool:
        """Mount an SD card"""
        with self._cards_lock:
            if device_name not in self.cards:
                return False
            
            card_info = self.cards[device_name]
            
            if card_info.is_mounted:
                logging.info(f"Card {device_name} already mounted at {card_info.mount_point}")
                return True
            
            # Create mount point
            mount_point = self.mount_base / device_name
            mount_point.mkdir(parents=True, exist_ok=True)
            
            try:
                # Mount the card
                result = subprocess.run(
                    ['mount', card_info.device_path, str(mount_point)],
                    capture_output=True,
                    text=True,
                    check=True
                )
                
                # Update card info
                card_info.mount_point = str(mount_point)
                card_info.is_mounted = True
                
                # Check if writable
                test_file = mount_point / ".write_test"
                try:
                    test_file.touch()
                    test_file.unlink()
                    card_info.is_writable = True
                except:
                    card_info.is_writable = False
                
                with self._stats_lock:
                    self.stats['cards_mounted'] += 1
                
                logging.info(f"Mounted {device_name} at {mount_point}")
                
                # Callback
                if self.on_card_mounted:
                    try:
                        self.on_card_mounted(card_info)
                    except Exception as e:
                        logging.error(f"Error in mount callback: {e}")
                
                return True
            
            except subprocess.CalledProcessError as e:
                error_msg = f"Failed to mount {device_name}: {e.stderr}"
                logging.error(error_msg)
                
                with self._stats_lock:
                    self.stats['mount_errors'] += 1
                
                if self.on_card_error:
                    self.on_card_error(device_name, error_msg)
                
                return False
    
    def unmount_card(self, device_name: str) -> bool:
        """Safely unmount an SD card"""
        with self._cards_lock:
            if device_name not in self.cards:
                return False
            
            card_info = self.cards[device_name]
            
            if not card_info.is_mounted:
                return True
            
            try:
                # Sync first
                subprocess.run(['sync'], check=True)
                
                # Unmount
                subprocess.run(
                    ['umount', card_info.device_path],
                    capture_output=True,
                    text=True,
                    check=True
                )
                
                card_info.is_mounted = False
                card_info.mount_point = None
                
                logging.info(f"Unmounted {device_name}")
                
                return True
            
            except subprocess.CalledProcessError as e:
                logging.error(f"Failed to unmount {device_name}: {e.stderr}")
                return False
    
    def format_card(self, device_name: str, filesystem: str = "ext4", label: str = "CLOUDSTORE") -> bool:
        """Format SD card with specified filesystem"""
        with self._cards_lock:
            if device_name not in self.cards:
                return False
            
            card_info = self.cards[device_name]
            
            # Unmount if mounted
            if card_info.is_mounted:
                if not self.unmount_card(device_name):
                    return False
            
            try:
                logging.info(f"Formatting {device_name} as {filesystem}...")
                
                # Format based on filesystem type
                if filesystem == "ext4":
                    subprocess.run(
                        ['mkfs.ext4', '-F', '-L', label, card_info.device_path],
                        capture_output=True,
                        text=True,
                        check=True
                    )
                elif filesystem == "fat32" or filesystem == "vfat":
                    subprocess.run(
                        ['mkfs.vfat', '-F', '32', '-n', label, card_info.device_path],
                        capture_output=True,
                        text=True,
                        check=True
                    )
                else:
                    logging.error(f"Unsupported filesystem: {filesystem}")
                    return False
                
                # Update card info
                card_info.filesystem = filesystem
                card_info.label = label
                
                with self._stats_lock:
                    self.stats['cards_formatted'] += 1
                
                logging.info(f"Formatted {device_name} successfully")
                
                # Auto-mount after format
                if self.auto_mount:
                    self.mount_card(device_name)
                
                return True
            
            except subprocess.CalledProcessError as e:
                logging.error(f"Failed to format {device_name}: {e.stderr}")
                return False
    
    def _udev_monitor_loop(self):
        """Monitor using pyudev (better, event-driven)"""
        context = pyudev.Context()
        monitor = pyudev.Monitor.from_netlink(context)
        monitor.filter_by(subsystem='block', device_type='partition')
        
        for device in iter(monitor.poll, None):
            if not self._running:
                break
            
            if device.action == 'add':
                # Check if removable
                try:
                    parent = device.parent
                    if parent and hasattr(parent, 'attributes'):
                        removable = parent.attributes.get('removable')
                        if removable and int(removable) == 1:
                            device_path = device.device_node
                            card_info = self._get_card_info(device_path)
                            if card_info:
                                self._handle_card_insertion(card_info)
                except:
                    pass
            
            elif device.action == 'remove':
                device_name = device.sys_name
                self._handle_card_removal(device_name)
    
    def _poll_monitor_loop(self):
        """Monitor using polling (fallback)"""
        previous_cards = set()
        
        while self._running:
            try:
                # Get current cards
                current_cards = set()
                
                block_devs = Path("/sys/block").glob("sd*")
                for dev_path in block_devs:
                    removable_file = dev_path / "removable"
                    if not removable_file.exists():
                        continue
                    
                    try:
                        is_removable = int(removable_file.read_text().strip())
                        if not is_removable:
                            continue
                    except:
                        continue
                    
                    device_name = dev_path.name
                    
                    for part_path in dev_path.glob(f"{device_name}*"):
                        if part_path.name == device_name:
                            continue
                        current_cards.add(part_path.name)
                
                # Detect insertions
                inserted = current_cards - previous_cards
                for device_name in inserted:
                    device_path = f"/dev/{device_name}"
                    card_info = self._get_card_info(device_path)
                    if card_info:
                        self._handle_card_insertion(card_info)
                
                # Detect removals
                removed = previous_cards - current_cards
                for device_name in removed:
                    self._handle_card_removal(device_name)
                
                previous_cards = current_cards
                
                time.sleep(self.monitor_interval)
            
            except Exception as e:
                logging.error(f"Error in poll monitor: {e}")
                time.sleep(self.monitor_interval)
    
    def get_cards(self) -> Dict[str, SDCardInfo]:
        """Get all detected cards"""
        with self._cards_lock:
            return self.cards.copy()
    
    def get_stats(self) -> dict:
        """Get statistics"""
        with self._stats_lock:
            stats = self.stats.copy()
            stats['cards_current'] = len(self.cards)
            return stats
    
    def list_cards(self):
        """Print list of detected cards"""
        with self._cards_lock:
            if not self.cards:
                print("No SD cards detected")
                return
            
            print(f"\nDetected SD Cards ({len(self.cards)}):")
            print("=" * 80)
            
            for device_name, card_info in self.cards.items():
                print(f"\nDevice: {card_info.device_path}")
                print(f"  Size: {card_info.size_human()}")
                print(f"  Filesystem: {card_info.filesystem}")
                print(f"  Label: {card_info.label or 'None'}")
                print(f"  UUID: {card_info.uuid or 'None'}")
                print(f"  Mounted: {'Yes' if card_info.is_mounted else 'No'}")
                if card_info.is_mounted:
                    print(f"  Mount Point: {card_info.mount_point}")
                    print(f"  Writable: {'Yes' if card_info.is_writable else 'No'}")

# Example usage / CLI
if __name__ == "__main__":
    import argparse
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    
    parser = argparse.ArgumentParser(description="SD Card Manager")
    parser.add_argument('--auto-mount', action='store_true', help='Auto-mount cards')
    parser.add_argument('--monitor', action='store_true', help='Start monitoring')
    parser.add_argument('--list', action='store_true', help='List detected cards')
    parser.add_argument('--format', metavar='DEVICE', help='Format a card')
    parser.add_argument('--filesystem', default='ext4', help='Filesystem type (default: ext4)')
    
    args = parser.parse_args()
    
    manager = SDCardManager(auto_mount=args.auto_mount)
    
    def on_inserted(card: SDCardInfo):
        print(f"\n[EVENT] Card inserted: {card.device_name} ({card.size_human()})")
    
    def on_removed(device_name: str):
        print(f"\n[EVENT] Card removed: {device_name}")
    
    def on_mounted(card: SDCardInfo):
        print(f"\n[EVENT] Card mounted: {card.device_name} at {card.mount_point}")
    
    manager.on_card_inserted = on_inserted
    manager.on_card_removed = on_removed
    manager.on_card_mounted = on_mounted
    
    manager.start()
    
    if args.list:
        time.sleep(1.0)  # Give time for initial scan
        manager.list_cards()
    
    if args.format:
        print(f"Formatting {args.format} as {args.filesystem}...")
        if manager.format_card(args.format, args.filesystem):
            print("Format successful!")
        else:
            print("Format failed!")
    
    if args.monitor:
        print("\nMonitoring for SD card events... (Ctrl+C to stop)")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\nStopping...")
    
    manager.stop()
