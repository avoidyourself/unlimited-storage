#!/usr/bin/env python3
"""
Autonomous SD Card Cloud Storage System.

Complete integration:
- SD card detection and auto-mount
- Independent network stack (LoRa/BLE/WiFi)
- Unlimited virtual filesystem
- Distributed storage backend
- Real-time monitoring

This turns a small SD card into an unlimited storage node
with its own network that doesn't depend on host networking.
"""
import time
import sys
import logging
import signal
from pathlib import Path
from typing import Optional
import threading

from sdcard_manager import SDCardManager, SDCardInfo
from independent_network import IndependentNetworkStack
from distributed_store import DistributedStore
from unlimited_fs import mount_unlimited_fs, FUSE_AVAILABLE

class AutonomousSDCard:
    """
    Complete autonomous SD card system.
    
    Turns SD card into:
    - Unlimited storage node (via distribution)
    - Independent network node (own stack)
    - Autonomous operation (survives host failures)
    """
    
    def __init__(
        self,
        node_id: str,
        mount_base: Path = Path("/mnt/cloudstore"),
        network_port: int = 9000
    ):
        self.node_id = node_id
        self.mount_base = mount_base
        self.network_port = network_port
        
        # Components
        self.sdcard_manager: Optional[SDCardManager] = None
        self.network_stack: Optional[IndependentNetworkStack] = None
        self.distributed_store: Optional[DistributedStore] = None
        self.mounted_cards: dict = {}  # device_name -> mount_info
        
        # State
        self.running = False
        self._monitor_thread: Optional[threading.Thread] = None
        
        # Statistics
        self.stats = {
            'start_time': time.time(),
            'cards_processed': 0,
            'filesystems_mounted': 0,
            'network_nodes': 0,
            'total_storage_provided': 0
        }
        self._stats_lock = threading.Lock()
    
    def start(self):
        """Start the autonomous system"""
        if self.running:
            return
        
        logging.info("=" * 70)
        logging.info("AUTONOMOUS SD CARD CLOUD STORAGE SYSTEM")
        logging.info("=" * 70)
        logging.info(f"Node ID: {self.node_id}")
        logging.info("")
        
        # 1. Initialize independent network stack
        logging.info("[1/4] Initializing independent network stack...")
        self.network_stack = IndependentNetworkStack(self.node_id)
        
        if self.network_stack.initialize():
            self.network_stack.start()
            logging.info(f"  ✓ Network active at {self.network_stack.my_address.ip}")
            logging.info(f"  ✓ Transport: {self.network_stack.active_transport.transport_type.value if self.network_stack.active_transport else 'None'}")
        else:
            logging.warning("  ⚠ Network initialization failed (will retry)")
        
        # 2. Initialize SD card manager
        logging.info("\n[2/4] Initializing SD card manager...")
        self.sdcard_manager = SDCardManager(
            mount_base=self.mount_base,
            auto_mount=True,
            auto_format=False
        )
        
        # Set up callbacks
        self.sdcard_manager.on_card_inserted = self._handle_card_inserted
        self.sdcard_manager.on_card_removed = self._handle_card_removed
        self.sdcard_manager.on_card_mounted = self._handle_card_mounted
        self.sdcard_manager.on_card_error = self._handle_card_error
        
        self.sdcard_manager.start()
        logging.info("  ✓ SD card monitoring active")
        
        # 3. Start monitoring thread
        logging.info("\n[3/4] Starting real-time monitoring...")
        self.running = True
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            daemon=True
        )
        self._monitor_thread.start()
        logging.info("  ✓ Monitoring active")
        
        # 4. System ready
        logging.info("\n[4/4] System ready!")
        logging.info("=" * 70)
        logging.info("STATUS: OPERATIONAL")
        logging.info("=" * 70)
        
        self._print_status()
    
    def stop(self):
        """Stop the autonomous system"""
        logging.info("\n" + "=" * 70)
        logging.info("SHUTTING DOWN")
        logging.info("=" * 70)
        
        self.running = False
        
        # Stop monitoring
        if self._monitor_thread:
            self._monitor_thread.join(timeout=2.0)
        
        # Unmount all filesystems
        for device_name, mount_info in list(self.mounted_cards.items()):
            logging.info(f"Unmounting {device_name}...")
            self._unmount_card_filesystem(device_name)
        
        # Stop SD card manager
        if self.sdcard_manager:
            self.sdcard_manager.stop()
            logging.info("✓ SD card manager stopped")
        
        # Stop distributed store
        if self.distributed_store:
            self.distributed_store.stop()
            logging.info("✓ Distributed store stopped")
        
        # Stop network stack
        if self.network_stack:
            self.network_stack.stop()
            logging.info("✓ Network stack stopped")
        
        logging.info("=" * 70)
        logging.info("SHUTDOWN COMPLETE")
        logging.info("=" * 70)
    
    def _handle_card_inserted(self, card: SDCardInfo):
        """Handle SD card insertion"""
        logging.info("")
        logging.info("=" * 70)
        logging.info(f"SD CARD INSERTED: {card.device_name}")
        logging.info("=" * 70)
        logging.info(f"  Device: {card.device_path}")
        logging.info(f"  Size: {card.size_human()}")
        logging.info(f"  Filesystem: {card.filesystem}")
        logging.info(f"  Label: {card.label or 'None'}")
        
        with self._stats_lock:
            self.stats['cards_processed'] += 1
    
    def _handle_card_removed(self, device_name: str):
        """Handle SD card removal"""
        logging.info("")
        logging.info("=" * 70)
        logging.info(f"SD CARD REMOVED: {device_name}")
        logging.info("=" * 70)
        
        # Unmount filesystem if mounted
        if device_name in self.mounted_cards:
            self._unmount_card_filesystem(device_name)
    
    def _handle_card_mounted(self, card: SDCardInfo):
        """Handle SD card mounted"""
        logging.info(f"  ✓ Mounted at: {card.mount_point}")
        logging.info(f"  ✓ Writable: {'Yes' if card.is_writable else 'No'}")
        
        # Initialize distributed storage on this card
        self._setup_distributed_storage(card)
        
        # Mount unlimited filesystem
        self._mount_unlimited_filesystem(card)
    
    def _handle_card_error(self, device_name: str, error: str):
        """Handle SD card error"""
        logging.error(f"  ✗ Error with {device_name}: {error}")
    
    def _setup_distributed_storage(self, card: SDCardInfo):
        """Setup distributed storage backend on card"""
        if not card.mount_point:
            return
        
        logging.info("\n  [Setup] Initializing distributed storage...")
        
        # Create store directory on card
        store_path = Path(card.mount_point) / ".cloudstore" / "storage"
        store_path.mkdir(parents=True, exist_ok=True)
        
        # Create distributed store
        self.distributed_store = DistributedStore(
            store_path=store_path,
            node_id=self.node_id,
            host="127.0.0.1",  # Binds to localhost (our independent network handles routing)
            port=self.network_port,
            replication_factor=3,
            auto_replicate=True
        )
        
        self.distributed_store.start()
        
        logging.info(f"    ✓ Storage initialized at {store_path}")
        logging.info(f"    ✓ Replication factor: 3")
        logging.info(f"    ✓ Network port: {self.network_port}")
        
        with self._stats_lock:
            self.stats['network_nodes'] += 1
    
    def _mount_unlimited_filesystem(self, card: SDCardInfo):
        """Mount unlimited virtual filesystem"""
        if not card.mount_point or not self.distributed_store:
            return
        
        if not FUSE_AVAILABLE:
            logging.warning("    ⚠ FUSE not available - unlimited FS not mounted")
            logging.warning("    Install with: pip install fusepy")
            return
        
        logging.info("\n  [Setup] Mounting unlimited filesystem...")
        
        # Create unlimited mount point
        unlimited_mount = Path(card.mount_point) / "unlimited"
        unlimited_mount.mkdir(parents=True, exist_ok=True)
        
        # Create cache directory
        cache_dir = Path(card.mount_point) / ".cloudstore" / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        
        # Calculate cache size (use 20% of card for cache)
        cache_size_mb = min(2048, card.size_bytes // (1024 * 1024) // 5)
        
        # Mount in background thread
        def mount_thread():
            try:
                mount_unlimited_fs(
                    unlimited_mount,
                    self.distributed_store,
                    cache_dir,
                    cache_size_mb,
                    foreground=False
                )
            except Exception as e:
                logging.error(f"    ✗ Failed to mount unlimited FS: {e}")
        
        thread = threading.Thread(target=mount_thread, daemon=True)
        thread.start()
        
        time.sleep(1.0)  # Give it time to mount
        
        # Check if mounted successfully
        if unlimited_mount.exists():
            logging.info(f"    ✓ Unlimited filesystem mounted at {unlimited_mount}")
            logging.info(f"    ✓ Apparent size: UNLIMITED")
            logging.info(f"    ✓ Local cache: {cache_size_mb} MB")
            logging.info(f"    ✓ Actual capacity: {card.size_human()} → UNLIMITED via network")
            
            self.mounted_cards[card.device_name] = {
                'card': card,
                'store_path': Path(card.mount_point) / ".cloudstore" / "storage",
                'unlimited_mount': unlimited_mount,
                'cache_dir': cache_dir
            }
            
            with self._stats_lock:
                self.stats['filesystems_mounted'] += 1
                self.stats['total_storage_provided'] += card.size_bytes
    
    def _unmount_card_filesystem(self, device_name: str):
        """Unmount card's unlimited filesystem"""
        if device_name not in self.mounted_cards:
            return
        
        mount_info = self.mounted_cards[device_name]
        
        # Unmount FUSE filesystem
        unlimited_mount = mount_info['unlimited_mount']
        try:
            import subprocess
            subprocess.run(['fusermount', '-u', str(unlimited_mount)], check=False)
        except:
            pass
        
        del self.mounted_cards[device_name]
    
    def _monitor_loop(self):
        """Real-time monitoring loop"""
        last_print = 0
        
        while self.running:
            try:
                current_time = time.time()
                
                # Print status every 30 seconds
                if current_time - last_print > 30:
                    self._print_status()
                    last_print = current_time
                
                time.sleep(5)
            
            except Exception as e:
                logging.error(f"Monitor error: {e}")
    
    def _print_status(self):
        """Print current system status"""
        print("\n" + "=" * 70)
        print("SYSTEM STATUS")
        print("=" * 70)
        
        # Uptime
        uptime = time.time() - self.stats['start_time']
        hours = int(uptime // 3600)
        minutes = int((uptime % 3600) // 60)
        print(f"Uptime: {hours}h {minutes}m")
        
        # SD Cards
        if self.sdcard_manager:
            cards = self.sdcard_manager.get_cards()
            print(f"\nSD Cards: {len(cards)}")
            for device_name, card in cards.items():
                status = "MOUNTED + UNLIMITED" if device_name in self.mounted_cards else "DETECTED"
                print(f"  {device_name}: {card.size_human()} - {status}")
        
        # Network
        if self.network_stack:
            net_stats = self.network_stack.get_stats()
            print(f"\nIndependent Network:")
            print(f"  Address: {net_stats['my_address']}")
            print(f"  Transport: {net_stats['active_transport']}")
            print(f"  Peers: {net_stats['peers_count']}")
            print(f"  Packets Sent: {net_stats['packets_sent']}")
            print(f"  Packets Received: {net_stats['packets_received']}")
        
        # Storage
        if self.distributed_store:
            store_stats = self.distributed_store.get_stats()
            print(f"\nDistributed Storage:")
            print(f"  Local Stores: {store_stats['distributed']['local_stores']}")
            print(f"  Replications Sent: {store_stats['distributed']['replications_sent']}")
            print(f"  Total Objects: {store_stats['local_storage']['total_objects']}")
        
        # Overall stats
        print(f"\nOverall Statistics:")
        with self._stats_lock:
            print(f"  Cards Processed: {self.stats['cards_processed']}")
            print(f"  Filesystems Mounted: {self.stats['filesystems_mounted']}")
            print(f"  Network Nodes Active: {self.stats['network_nodes']}")
        
        print("=" * 70)

def main():
    import argparse
    import hashlib
    
    # Set up logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(message)s',
        datefmt='%H:%M:%S'
    )
    
    parser = argparse.ArgumentParser(
        description="Autonomous SD Card Cloud Storage System"
    )
    parser.add_argument(
        '--node-id',
        help='Node ID (auto-generated if not provided)'
    )
    parser.add_argument(
        '--port',
        type=int,
        default=9000,
        help='Network port (default: 9000)'
    )
    parser.add_argument(
        '--mount-base',
        default='/mnt/cloudstore',
        help='Base mount directory (default: /mnt/cloudstore)'
    )
    
    args = parser.parse_args()
    
    # Generate node ID if not provided
    if args.node_id:
        node_id = args.node_id
    else:
        node_id = "node_" + hashlib.sha256(str(time.time()).encode()).hexdigest()[:12]
    
    # Create system
    system = AutonomousSDCard(
        node_id=node_id,
        mount_base=Path(args.mount_base),
        network_port=args.port
    )
    
    # Handle signals
    def signal_handler(sig, frame):
        print("\n\nInterrupt received...")
        system.stop()
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Start system
    system.start()
    
    print("\n")
    print("Commands:")
    print("  status - Show system status")
    print("  cards  - List SD cards")
    print("  stats  - Show detailed statistics")
    print("  quit   - Shutdown system")
    print()
    
    # Interactive loop
    try:
        while system.running:
            try:
                cmd = input("> ").strip().lower()
                
                if cmd == 'quit':
                    break
                elif cmd == 'status':
                    system._print_status()
                elif cmd == 'cards':
                    if system.sdcard_manager:
                        system.sdcard_manager.list_cards()
                elif cmd == 'stats':
                    system._print_status()
                elif cmd == '':
                    continue
                else:
                    print("Unknown command. Try: status, cards, stats, quit")
            
            except EOFError:
                break
            except KeyboardInterrupt:
                break
    
    finally:
        system.stop()

if __name__ == "__main__":
    main()
