#!/usr/bin/env python3
"""
ARQ-FEC Server (Receiver) - Stream Encoding Geometry
Implements the receiver side of ARQ-FEC with stream encoding geometry.
Receives fragmented encoded SCHC packets via loopback UDP and attempts reconstruction.
"""

import socket
import math

from config import *

class ReceptionCStream:
    """C-Stream data structure (shared state for Reassembler and Decoder)."""

    def __init__(self):
        self.buffer: bytearray = bytearray()
        self.bitmap: list[bool] = []

    def put_data(self, index: int, data: bytes) -> None:
        """Put data in the C-Stream buffer at the given tile index."""
        end = index + len(data)
        if end > len(self.buffer):
            self.buffer.extend(b'\x00' * (end - len(self.buffer)))
            while len(self.bitmap) < end:
                self.bitmap.append(False)
        self.buffer[index:end] = data
        for i in range(index, end):
            self.bitmap[i] = True

class StreamDecoder:
    """Stream encoding geometry decoder for ARQ-FEC."""

    def __init__(self, cstream: ReceptionCStream,
                 symbol_size_bits: int, k: int, n: int):
        self.cstream = cstream
        self.symbol_size_bits = symbol_size_bits
        self.symbol_size_bytes = symbol_size_bits // 8
        self.k = k  # source symbols per block
        self.n = n  # encoded symbols per block

class StreamReassembler:
    """Stream encoding geometry reassembler for ARQ-FEC."""

    def __init__(self, cstream: ReceptionCStream,
                 window_size: int, tile_size_bits: int, w_field_size: int,
                 interleaving_depth: int = 0):
        self.cstream = cstream
        self.window_size = window_size
        self.tile_size_bits = tile_size_bits
        self.tile_size_bytes = tile_size_bits // 8
        self.w_field_size = w_field_size
        self.fcn_field_size = max(1, math.ceil(math.log2(self.window_size)))
        self.interleaving_depth = interleaving_depth

    def parse_fragment(self, data: bytes) -> tuple[int, int, bytes]:
        """Extract w, fcn, and payload from fragment bytes."""

        w_bits = self.w_field_size
        fcn_bits = self.fcn_field_size
        total_bytes = (w_bits + fcn_bits + 7) // 8

        header_int = int.from_bytes(data[:total_bytes], byteorder="big")

        total_bits = w_bits + fcn_bits
        header_int &= (1 << total_bits) - 1

        w = header_int >> fcn_bits
        fcn = header_int & ((1 << fcn_bits) - 1)

        return w, fcn, data[total_bytes:]

    def place_tiles(self, w: int, fcn: int, tiles: bytes) -> None:
        """Place tiles starting at w:fcn into the C-Stream."""

        step = self.tile_size_bytes
        # read the tiles in step.
        # extract that tile from the tiles
        # calculate the position in the cstream (use window size)
        #   the first tile is w:fcn, but then we reduce fcn by interleaving depth
        # put it in the cstream in the calculated position (1 tile contains x bits/bytes)
        #pos = index * self.tile_size_bytes # 1 tile contains x bits (bytes)


def main():
    """Main function for the ARQ-FEC application server (receiver)."""

    print("ARQ-FEC App Server (Receiver) - Stream Encoding Geometry")
    print("=" * 52)

    # Initialize reception C-Stream
    cstream = ReceptionCStream()

    # Initialize Reassembler and Decoder
    reassembler = StreamReassembler(
        cstream=cstream,
        window_size=WINDOW_SIZE,
        tile_size_bits=TILE_SIZE,
        w_field_size=W_FIELD_SIZE,
        interleaving_depth=INTERLEAVING_DEPTH
    )

    decoder = StreamDecoder(
        cstream=cstream,
        symbol_size_bits=SYMBOL_SIZE_BITS,
        k=SOURCE_BLOCK_SIZE,
        n=ENCODED_BLOCK_SIZE
    )

    print(f"Reassembler and Decoder sub-processes initialized.")

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
                # -- Reception -------------------------------------------------

                packet, addr = sock.recvfrom(RX_BUFFER_SIZE)

                print("\nRX:", packet)

                # -- Reassembly ------------------------------------------------

                w, fcn, payload = reassembler.parse_fragment(packet)
                print(f"   w={w}, fcn={fcn}, payload={payload.hex()}")

                if fcn == WINDOW_SIZE:
                    print("   Received ALL-1 fragment!")

                reassembler.place_tiles(w, fcn, payload)

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
