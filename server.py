#!/usr/bin/env python3
"""
ARQ-FEC Server (Receiver) - Stream Encoding Geometry
Implements the receiver side of ARQ-FEC with stream encoding geometry.
Receives fragmented encoded SCHC packets via loopback UDP and attempts reconstruction.
"""

import socket
import math
import zlib
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
                 symbol_size_bits: int, k: int, n: int, tile_size_bits: int):
        self.cstream = cstream
        self.symbol_size_bits = symbol_size_bits
        self.symbol_size_bytes = symbol_size_bits // 8
        self.k = k  # source symbols per block
        self.n = n  # encoded symbols per block
        self.symbols_per_tile = (tile_size_bits // 8) // self.symbol_size_bytes

    def symbol_received(self, symbol_index: int) -> bool:
        """Read the C-Stream bitmap to see if a symbol has been received."""
        tile_index = symbol_index // self.symbols_per_tile
        return (
            self.cstream.bitmap[tile_index]
            if tile_index < len(self.cstream.bitmap)
            else False
        )

    def is_decodeable(self):
        """Check k out of n in every encoded block of the C-Stream."""

        symbols_in_cstream = len(self.cstream.bitmap) * self.symbols_per_tile
        trailing_symbols = symbols_in_cstream % self.n

        # Defensive check: legitimate trailing symbols are strictly < k
        if trailing_symbols >= self.k:
            return False

        full_blocks_end = symbols_in_cstream - trailing_symbols

        # Check complete/full blocks
        for block_start in range(0, full_blocks_end, self.n):
            symbols_received = sum(
                1 for s in range(block_start, block_start + self.n)
                if self.symbol_received(s)
            )
            if symbols_received < self.k:
                return False

        # Trailing symbols have no parity; all must be present
        if trailing_symbols > 0:
            symbols_received = sum(
                1 for s in range(full_blocks_end, symbols_in_cstream)
                if self.symbol_received(s)
            )
            if (symbols_received < trailing_symbols):
                return False
        return True

    def decode(self) -> bytes:
        """Decode the C-Stream assuming XOR-based parity (n = k + 1).
        Does not mutate the C-Stream. Returns reconstructed SCHC packet."""

        symbols_in_cstream = len(self.cstream.bitmap) * self.symbols_per_tile
        full_blocks_end = symbols_in_cstream - (symbols_in_cstream % self.n)

        result = bytearray()

        # Process complete blocks
        for block_start in range(0, full_blocks_end, self.n):
            present = [
                s for s in range(block_start, block_start + self.n)
                if self.symbol_received(s)
            ]

            # Append the k source symbols (positions block_start .. block_start+k-1)
            for src_pos in range(block_start, block_start + self.k):
                if src_pos in present:
                    start = src_pos * self.symbol_size_bytes
                    result.extend(
                        self.cstream.buffer[start:start + self.symbol_size_bytes]
                    )
                else:
                    # Recover via XOR of all present symbols
                    recovered = bytes(self.symbol_size_bytes)
                    for p in present:
                        s = p * self.symbol_size_bytes
                        d = self.cstream.buffer[s:s + self.symbol_size_bytes]
                        recovered = bytes(a ^ b for a, b in zip(recovered, d))
                    result.extend(recovered)

        # Append trailing source symbols as-is (no parity exists)
        for src_pos in range(full_blocks_end, symbols_in_cstream):
            start = src_pos * self.symbol_size_bytes
            result.extend(
                self.cstream.buffer[start:start + self.symbol_size_bytes]
            )

        return bytes(result)

    def rcs_check(self, payload: bytes, rcs: bytes) -> bool:
        """Verify CRC-32 of payload against the provided RCS."""
        computed = zlib.crc32(payload) & 0xFFFFFFFF
        expected = int.from_bytes(rcs, byteorder='big')
        return computed == expected

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

    def get_tile_index(self, w: int, fcn: int) -> int:
        """Convert W:FCN to flat tile index in the C-Stream."""
        return (w + 1) * self.window_size - fcn - 1

    def place_tiles(self, w: int, fcn: int, tiles: bytes) -> None:
        """Place tiles from a received fragment into the C-Stream.

        The first tile is located at the position derived from W:FCN.
        Subsequent tiles are placed at interleaved stride intervals.
        """
        step = self.tile_size_bytes
        num_tiles = len(tiles) // step
        base_index = self.get_tile_index(w, fcn)
        stride = self.interleaving_depth if self.interleaving_depth > 1 else 1

        # Extract and place each tile at its corresponding position
        for tile_offset in range(num_tiles):
            start = tile_offset * step
            tile_bytes = tiles[start:start+step]
            tile_index = base_index + tile_offset * stride
            self.cstream.put_data(index=tile_index, data=tile_bytes)

def main(ready_event=None):
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
        n=ENCODED_BLOCK_SIZE,
        tile_size_bits=TILE_SIZE
    )

    print(f"Reassembler and Decoder sub-processes initialized.")

    # Create UDP socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    # Create UDP socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((IP_ADDR, APP_PORT))
    sock.settimeout(RX_TIMEOUT)  # Set timeout

    if ready_event is not None:
        ready_event.set()

    print(f"Listening on {IP_ADDR}:{APP_PORT}")
    print(f"Waiting for fragmented SCHC packets...")

    # Initialize statistics
    stats_regular_received = 0
    stats_all1_received = False
    stats_decodeable = False
    stats_rcs_ok = False

    try:

        while True:
            try:
                # -- Reception -------------------------------------------------

                packet, addr = sock.recvfrom(RX_BUFFER_SIZE)

                print("RX:", packet)

                # -- Reassembly ------------------------------------------------

                w, fcn, payload = reassembler.parse_fragment(packet)
                print(f"   w={w}, fcn={fcn}, payload={payload.hex()}")

                is_all1 = (fcn == reassembler.window_size)

                if is_all1:
                    print("   Received ALL-1 fragment.")
                    stats_all1_received = True

                else:
                    # Process regular fragments
                    reassembler.place_tiles(w, fcn, payload)
                    print(f"   C-Stream> {cstream.buffer.hex()}")
                    stats_regular_received += 1

                # -- Decoding --------------------------------------------------

                if (decoder.is_decodeable()):
                    print("   ! C-Stream is decodeable !")
                    decoded_schc_packet = decoder.decode()
                    stats_decodeable = True
                    print(f"   Decoded SCHC Packet = {decoded_schc_packet.hex()}.")

                    # Check against what we expect (assert?)
                    print(f"   > {('OK' if decoded_schc_packet == SCHC_PACKET else 'ERROR')}")

                    if is_all1:
                        if decoder.rcs_check(
                            payload=decoded_schc_packet,
                            rcs=payload
                        ):
                            print(f"   >>> RCS Check OK !!!")
                            stats_rcs_ok = True
                            break

                # -- End of main loop ------------------------------------------

                print()

            except socket.timeout:
                print("   Timeout reached - no more packets expected")
                break
            except Exception as e:
                print(f"   Error receiving: {e}")
                break

        # Post-processing goes here

    except Exception as e:
        print(f"Error: {e}")
    finally:
        sock.close()
        print("\nServer shutting down...")

    # Return statistics
    return dict(
        regular_received=stats_regular_received,
        all1_received=stats_all1_received,
        decodeable=stats_decodeable,
        rcs_ok=stats_rcs_ok,
    )

if __name__ == "__main__":
    main()
