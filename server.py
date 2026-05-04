#!/usr/bin/env python3
"""
ARQ-FEC Server (Receiver) - Stream Encoding Geometry
Implements the receiver side of ARQ-FEC with stream encoding geometry.
Receives fragmented encoded SCHC packets via loopback UDP and attempts reconstruction.
"""

import socket

from config import *

class StreamDecoder:
    """Stream encoding geometry decoder for ARQ-FEC."""

    def __init__(self, symbol_size_bits: int, k: int, n: int):
        self.symbol_size_bits = symbol_size_bits
        self.symbol_size_bytes = symbol_size_bits // 8
        self.k = k  # source symbols per block
        self.n = n  # encoded symbols per block

def main():
    """Main function for the ARQ-FEC application server (receiver)."""

    print("ARQ-FEC App Server (Receiver) - Stream Encoding Geometry")
    print("=" * 52)

    # Create UDP socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    # Create UDP socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((IP_ADDR, APP_PORT))
    sock.settimeout(RX_TIMEOUT)  # Set timeout
    
    print(f"Listening on {IP_ADDR}:{APP_PORT}")
    print(f"Waiting for fragmented SCHC packets...")

    try:

        while True:
            try:
                data, addr = sock.recvfrom(RX_BUFFER_SIZE)

                print("GOT.", data)

            except socket.timeout:
                print("   Timeout reached - no more packets expected")
                break
            except Exception as e:
                print(f"   Error receiving: {e}")
                break

        # Process received windows
        print(f"\nProcessing received windows...")
        # ...
        print("  All good!")

    except Exception as e:
        print(f"Error: {e}")
    finally:
        sock.close()
        print("\nServer shutting down...")

if __name__ == "__main__":
    main()
