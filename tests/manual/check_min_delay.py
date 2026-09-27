#!/usr/bin/env python3
"""
Test script to find minimum delay needed for auto-detection.

This script repeatedly tests auto-detection at 9600 baud with decreasing delays
to find the minimum reliable delay time.
"""

import sys
import time

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    print("ERROR: pyserial not installed")
    print("Install with: pip install pyserial")
    sys.exit(1)


def bytes_to_printable(data):
    """Convert bytes to printable ASCII, replacing non-printable with '.'"""
    return ''.join(chr(b) if 32 <= b < 127 else '.' for b in data)


def test_detection_with_delay(port_path, baud_rate, reset_delay, num_attempts=5):
    """
    Test auto-detection with a specific delay multiple times.
    
    Args:
        port_path: Serial port path
        baud_rate: Baud rate to test
        reset_delay: Delay after DTR reset in seconds
        num_attempts: Number of times to test (default: 5)
        
    Returns:
        (success_count, total_attempts) tuple
    """
    success_count = 0
    
    for attempt in range(num_attempts):
        try:
            ser = serial.Serial(
                port=port_path,
                baudrate=baud_rate,
                bytesize=8,
                stopbits=1,
                parity='N',
                timeout=1.0,
                write_timeout=1.0,
            )
            
            # Clear any existing data in buffer first
            ser.reset_input_buffer()
            
            # Reset Arduino by toggling DTR
            ser.dtr = False
            time.sleep(0.1)
            ser.dtr = True
            
            # Wait for Arduino to reset and start transmitting
            time.sleep(reset_delay)
            
            # Check if there's data available
            has_data = ser.in_waiting > 0
            
            if has_data:
                # Read a sample to verify it's not all nulls
                sample = ser.read(min(ser.in_waiting, 64))
                non_null_bytes = sum(1 for b in sample if b != 0)
                
                if non_null_bytes > 0:
                    success_count += 1
                    print(f"  Attempt {attempt + 1}: ✓ ({len(sample)} bytes, {non_null_bytes} non-null)")
                else:
                    print(f"  Attempt {attempt + 1}: ✗ (all null bytes)")
            else:
                print(f"  Attempt {attempt + 1}: ✗ (no data)")
            
            ser.close()
            
            # Small delay between attempts
            time.sleep(0.2)
                
        except serial.SerialException as e:
            print(f"  Attempt {attempt + 1}: ✗ Error: {e}")
        except Exception as e:
            print(f"  Attempt {attempt + 1}: ✗ Unexpected: {e}")
    
    return success_count, num_attempts


def main():
    """Main function."""
    # List available ports
    print("\n" + "="*60)
    print("Available Serial Ports")
    print("="*60)
    
    ports = list(list_ports.comports())
    
    if not ports:
        print("No serial ports found!")
        return
    
    for i, port in enumerate(ports):
        print(f"\n{i+1}. {port.device}")
        print(f"   Description: {port.description}")
    
    print()
    
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
    
    # Configuration
    baud_rate = 9600
    num_attempts = 5
    
    print("\n" + "="*60)
    print("Minimum Delay Detection Test")
    print("="*60)
    print(f"Port: {selected_port}")
    print(f"Baud Rate: {baud_rate}")
    print(f"Attempts per delay: {num_attempts}")
    print(f"Success threshold: {num_attempts}/{num_attempts} (100%)")
    print("="*60 + "\n")
    
    # Starting delay and decrement
    print("Starting delay (seconds, default 2.0): ", end='')
    try:
        start_delay_str = input().strip()
        current_delay = float(start_delay_str) if start_delay_str else 2.0
    except (ValueError, KeyboardInterrupt):
        print("\nUsing default: 2.0")
        current_delay = 2.0
    
    print("Delay decrement per step (seconds, default 0.1): ", end='')
    try:
        decrement_str = input().strip()
        delay_decrement = float(decrement_str) if decrement_str else 0.1
    except (ValueError, KeyboardInterrupt):
        print("\nUsing default: 0.1")
        delay_decrement = 0.1
    
    print("Minimum delay to test (seconds, default 0.1): ", end='')
    try:
        min_delay_str = input().strip()
        min_delay = float(min_delay_str) if min_delay_str else 0.1
    except (ValueError, KeyboardInterrupt):
        print("\nUsing default: 0.1")
        min_delay = 0.1
    
    print("\n" + "="*60)
    print("Starting Tests...")
    print("="*60 + "\n")
    
    last_success_delay = None
    first_failure_delay = None
    
    while current_delay >= min_delay:
        print(f"\nTesting delay: {current_delay:.3f}s")
        success, total = test_detection_with_delay(
            selected_port, 
            baud_rate, 
            current_delay, 
            num_attempts
        )
        
        success_rate = (success / total) * 100
        print(f"Result: {success}/{total} successful ({success_rate:.0f}%)")
        
        if success == total:
            print("  ✓ All attempts successful!")
            last_success_delay = current_delay
        else:
            print(f"  ⚠️  Only {success}/{total} successful - delay too short")
            if first_failure_delay is None:
                first_failure_delay = current_delay
            
            # If we've found a failure after a success, we can stop
            if last_success_delay is not None:
                print("\n" + "="*60)
                print("Test Complete - Found Threshold!")
                print("="*60)
                print(f"Last successful delay: {last_success_delay:.3f}s")
                print(f"First failure delay: {first_failure_delay:.3f}s")
                print(f"\nRecommended minimum delay: {last_success_delay:.3f}s")
                print(f"Safe delay (with margin): {last_success_delay + 0.1:.3f}s")
                return
        
        current_delay -= delay_decrement
        
        # Prevent going below minimum
        if current_delay < min_delay:
            break
    
    # Summary
    print("\n" + "="*60)
    print("Test Complete")
    print("="*60)
    
    if last_success_delay is not None:
        print(f"Minimum successful delay found: {last_success_delay:.3f}s")
        if first_failure_delay is not None:
            print(f"First failure at: {first_failure_delay:.3f}s")
        print(f"\nRecommended minimum delay: {last_success_delay:.3f}s")
        print(f"Safe delay (with margin): {last_success_delay + 0.1:.3f}s")
    else:
        print("No successful delay found in tested range.")
        print(f"Try starting with a higher delay (current: {current_delay + delay_decrement:.3f}s)")


if __name__ == "__main__":
    main()
