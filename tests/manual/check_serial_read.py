#!/usr/bin/env python3
"""
Simple serial port read test to diagnose what data is actually being received.

This script opens the serial port and displays raw bytes, hex, and ASCII.
"""

import sys
import time
from datetime import datetime

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    print("ERROR: pyserial not installed")
    print("Install with: pip install pyserial")
    sys.exit(1)


def list_available_ports():
    """List all available serial ports."""
    print("\n" + "="*60)
    print("Available Serial Ports")
    print("="*60)
    
    ports = list(list_ports.comports())
    
    if not ports:
        print("No serial ports found!")
        return None
    
    for i, port in enumerate(ports):
        print(f"\n{i+1}. {port.device}")
        print(f"   Description: {port.description}")
        print(f"   Manufacturer: {port.manufacturer}")
        if port.serial_number:
            print(f"   Serial Number: {port.serial_number}")
        if port.vid and port.pid:
            print(f"   VID:PID: {port.vid:04X}:{port.pid:04X}")
    
    print()
    return ports


def bytes_to_printable(data):
    """Convert bytes to printable ASCII, replacing non-printable with '.'"""
    return ''.join(chr(b) if 32 <= b < 127 else '.' for b in data)


def score_serial_data(data):
    """Score serial data based on ASCII-like content.
    
    Args:
        data: Raw bytes received from serial port
        
    Returns:
        Score (higher is better) - count of ASCII printable + control chars
    """
    if not data:
        return 0
    # Count printable ASCII characters and common control chars (tab, newline, carriage return)
    return sum(32 <= b <= 126 or b in (9, 10, 13) for b in data)


def auto_detect_baud_rate(port_path):
    """
    Auto-detect baud rate using manual scoring approach (same as usb_port_mapper.py).
    
    Tests each baud rate candidate and scores the received data based on ASCII-like content.
    Returns the baud rate with the highest score.
    
    Args:
        port_path: Serial port path
        
    Returns:
        Detected baud rate or None
    """
    # Baud rate candidates (Arduino/ESP32 defaults first, then comprehensive list)
    # Must match usb_port_mapper.py for consistency
    baud_candidates = [
        4800,
        9600,     # Most common Arduino (Uno, Nano)
        19200,
        115200,   # Modern Arduino (Mega, Due, ESP32)
    ]
    
    print("\n" + "="*60)
    print("Auto-detecting Baud Rate (Manual Scoring Method)")
    print("="*60)
    print(f"Port: {port_path}")
    print(f"Testing rates: {baud_candidates}")
    print(f"Method: Score ASCII-printable content (32-126 + tab/newline/CR)")
    print("="*60 + "\n")
    
    best_baud = None
    best_score = -1
    results = []
    
    for baud_rate in baud_candidates:
        try:
            print(f"Testing {baud_rate} baud...", end='', flush=True)
            
            ser = serial.Serial(
                port=port_path,
                baudrate=baud_rate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.2,
            )
            
            # Clear any existing data in buffer
            ser.reset_input_buffer()
            
            # Reset Arduino by toggling DTR (triggers reset on most Arduino boards)
            ser.dtr = False
            time.sleep(0.1)
            ser.dtr = True
            
            # Wait for device to reset and start transmitting
            time.sleep(2.0)
            
            # Read up to 200 bytes (same as usb_port_mapper.py)
            data = ser.read(200)
            ser.close()
            
            # Score the data
            score = score_serial_data(data)
            results.append((baud_rate, score, data))
            
            print(f" score={score} ({len(data)} bytes)")
            
            if len(data) > 0:
                print(f"   Sample HEX: {data[:32].hex()}")
                print(f"   Sample ASCII: {bytes_to_printable(data[:32])}")
            
            if score > best_score:
                best_score = score
                best_baud = baud_rate
            
            # Small delay between attempts to let port settle
            time.sleep(0.2)
                
        except serial.SerialException as e:
            print(f" ✗ Error: {e}")
            results.append((baud_rate, 0, b''))
        except Exception as e:
            print(f" ✗ Unexpected error: {e}")
            results.append((baud_rate, 0, b''))
    
    # Display results summary
    print("\n" + "-"*60)
    print("Results Summary:")
    print("-"*60)
    for baud, score, data in results:
        marker = " ← BEST" if baud == best_baud else ""
        print(f"  {baud:>6} baud: score={score:>4} ({len(data):>3} bytes){marker}")
    print("-"*60)
    
    if best_baud and best_score > 0:
        print(f"\n✓ DETECTED: {best_baud} baud (score: {best_score})")
        return best_baud
    else:
        print("\n⚠️  Could not auto-detect baud rate (all scores were 0)")
        return None


def test_serial_read(port_path, baud_rate=115200, duration=10):
    """
    Read from serial port and display data in multiple formats.
    
    Args:
        port_path: Serial port path (e.g., /dev/ttyACM0 or COM3)
        baud_rate: Baud rate (default: 115200)
        duration: How long to read in seconds (default: 10)
    """
    print("\n" + "="*60)
    print("Serial Port Read Test")
    print("="*60)
    print(f"Port: {port_path}")
    print(f"Baud Rate: {baud_rate}")
    print(f"Duration: {duration} seconds")
    print(f"Press Ctrl+C to stop early")
    print("="*60 + "\n")
    
    try:
        # Open serial port
        ser = serial.Serial(
            port=port_path,
            baudrate=baud_rate,
            bytesize=8,
            stopbits=1,
            parity='N',
            timeout=1.0
        )
        
        print(f"✓ Opened {port_path} successfully")
        
        # Reset Arduino by toggling DTR (required for auto-start on RPi)
        print("Resetting Arduino...")
        ser.dtr = False
        time.sleep(0.1)  # Brief delay
        ser.dtr = True
        time.sleep(2)  # Wait for Arduino to reset and start sketch
        
        print(f"Waiting for data...\n")
        
        start_time = time.time()
        total_bytes = 0
        read_count = 0
        
        while (time.time() - start_time) < duration:
            try:
                # Read available data
                if ser.in_waiting > 0:
                    data = ser.read(ser.in_waiting)
                    if data:
                        read_count += 1
                        total_bytes += len(data)
                        timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                        
                        print(f"\n[{timestamp}] Read #{read_count} - {len(data)} bytes")
                        print(f"  HEX:   {data.hex()}")
                        print(f"  ASCII: {bytes_to_printable(data)}")
                        print(f"  RAW:   {data}")
                        
                        # Check if all zeros
                        if all(b == 0 for b in data):
                            print(f"  ⚠️  WARNING: All bytes are 0x00 (null)")
                        
                else:
                    time.sleep(0.1)
                    
            except KeyboardInterrupt:
                print("\n\n⏹️  Stopped by user")
                break
        
        ser.close()
        
        # Summary
        print("\n" + "="*60)
        print("Summary")
        print("="*60)
        print(f"Duration: {time.time() - start_time:.1f} seconds")
        print(f"Total Reads: {read_count}")
        print(f"Total Bytes: {total_bytes}")
        if read_count > 0:
            print(f"Average per Read: {total_bytes / read_count:.1f} bytes")
        print()
        
    except serial.SerialException as e:
        print(f"\n❌ Serial Error: {e}")
        print("\nPossible issues:")
        print("  - Port is already in use by another process")
        print("  - Permission denied (try: sudo usermod -a -G dialout $USER)")
        print("  - Wrong baud rate")
    except Exception as e:
        print(f"\n❌ Unexpected Error: {e}")


def main():
    """Main function."""
    # List available ports
    ports = list_available_ports()
    
    if not ports:
        return
    
    # Get user selection
    print("Select a port to test (or press Enter for port 1): ", end='')
    try:
        selection = input().strip()
        if not selection:
            selection = "1"
        port_index = int(selection) - 1
        
        if port_index < 0 or port_index >= len(ports):
            print(f"Invalid selection. Must be 1-{len(ports)}")
            return
        
        selected_port = ports[port_index].device
    except (ValueError, KeyboardInterrupt):
        print("\nCancelled")
        return
    
    # Get baud rate
    print("\nBaud rate options:")
    print("  1. 9600")
    print("  2. 115200")
    print("  3. 57600")
    print("  4. 38400")
    print("  5. Auto-detect (recommended)")
    print("  6. Custom")
    print("\nSelect baud rate (or press Enter for auto-detect): ", end='')
    
    baud_rate = None
    try:
        baud_choice = input().strip()
        if not baud_choice or baud_choice == "5":
            baud_rate = auto_detect_baud_rate(selected_port)
            if not baud_rate:
                print("Auto-detect failed. Enter baud rate manually: ", end='')
                baud_rate = int(input().strip())
        elif baud_choice == "1":
            baud_rate = 9600
        elif baud_choice == "2":
            baud_rate = 115200
        elif baud_choice == "3":
            baud_rate = 57600
        elif baud_choice == "4":
            baud_rate = 38400
        elif baud_choice == "6":
            print("Enter custom baud rate: ", end='')
            baud_rate = int(input().strip())
        else:
            print("Invalid choice, using auto-detect")
            baud_rate = auto_detect_baud_rate(selected_port)
            if not baud_rate:
                print("Auto-detect failed, using 115200")
                baud_rate = 115200
    except (ValueError, KeyboardInterrupt):
        print("\nUsing auto-detect")
        baud_rate = auto_detect_baud_rate(selected_port)
        if not baud_rate:
            print("Auto-detect failed, using 115200")
            baud_rate = 115200
    
    # Get duration
    print("\nHow long to read (seconds, default 10): ", end='')
    try:
        duration_str = input().strip()
        duration = int(duration_str) if duration_str else 10
    except (ValueError, KeyboardInterrupt):
        print("\nUsing default: 10 seconds")
        duration = 10
    
    # Run the test
    test_serial_read(selected_port, baud_rate, duration)


if __name__ == "__main__":
    main()
