#!/usr/bin/env python3
"""
ARQ-FEC Device (Sender) - Stream Encoding Geometry
Implements the sender side of ARQ-FEC with stream encoding geometry.
Sends fragmented encoded SCHC packets via loopback UDP.
"""

import socket
import math

from config import *

# =============================================================================
# E N C O D E R
# =============================================================================

class StreamEncoder:
    """Stream encoding geometry encoder for ARQ-FEC."""

    def __init__(self, symbol_size_bits: int, k: int, n: int):
        self.symbol_size_bits = symbol_size_bits # m bits
        self.symbol_size_bytes = symbol_size_bits // 8
        self.k = k  # source symbols per block
        self.n = n  # encoded symbols per block
    
    def divide_packet_into_symbols(self, schc_packet: bytes):
        """"List positions of each source symbol in the SCHC packet."""
        step = self.symbol_size_bytes
        return [i for i in range(0, len(schc_packet), step)]
    
    def get_source_symbol(self, schc_packet: bytes, index: int):
        """Get source symbol from (virtual) D-stream."""
        return schc_packet[index:index+self.symbol_size_bytes]

    def get_c_stream_size(self, source_symbols_number: int):
        """Calculate the size in symbols of the C-stream."""
        # every 2 +1
        full_blocks = source_symbols_number // self.k
        trailing = source_symbols_number % self.k

        encoded_from_full = full_blocks * self.n
        # Trailing source symbols are carried directly (no parity block created)
        # so they add one encoded symbol each.
        total_encoded = encoded_from_full + trailing

        return total_encoded, trailing

    def get_encoded_symbol(self, schc_packet: bytes, index: int):
        """Get encoded symbol from (virtual) C-stream."""
        # 0 1 [2] 3 4 [5] 6 7 [8] ...
        symbol_size = self.symbol_size_bytes

        block = index // 3
        pos = index % 3

        if pos in (0, 1):
            source_idx = block * 2 + pos
            start = source_idx * symbol_size
            return self.get_source_symbol(schc_packet, start)

        # pos == 2 -> XOR of previous two source symbols
        s0_idx = block * 2
        s1_idx = s0_idx + 1
        s0 = self.get_source_symbol(schc_packet, s0_idx * symbol_size)
        s1 = self.get_source_symbol(schc_packet, s1_idx * symbol_size)

        # Ensure both are length symbol_size (pad with zeros if truncated)
        if len(s0) < symbol_size:
            s0 = s0 + b'\x00' * (symbol_size - len(s0))
        if len(s1) < symbol_size:
            s1 = s1 + b'\x00' * (symbol_size - len(s1))

        # Byte-wise XOR
        return bytes(a ^ b for a, b in zip(s0, s1))

# =============================================================================
# F R A G M E N T E R
# =============================================================================
    
class StreamFragmenter:
    """Stream encoding geometry assembler and fragmenter for ARQ-FEC."""

    def __init__(self, window_size: int, tile_size_bits: int,
                 interleaving_depth: int = 0):
        self.window_size = window_size
        self.tile_size_bits = tile_size_bits
        self.tile_size_bytes = tile_size_bits // 8
        self.interleaving_depth = interleaving_depth

    def iter_interleaved_indices(self, num_tiles: int):
        """Yield tile indices in stride-interleaved order."""

        d = self.interleaving_depth

        if d <= 1:
            yield from range(num_tiles)
            return

        # stride-based permutation over flat stream
        for offset in range(d):
            for i in range(offset, num_tiles, d):
                yield i

    def get_header(self, tile_idx: int):
        """Compute SCHC fragment header values from a tile index."""

        w = tile_idx // self.window_size
        pos = tile_idx % self.window_size
        fcn = self.window_size - 1 - pos

        return w, fcn

    def iter_fragments(self, cstream_size: int, mtu: int):
        """Yield MTU-sized fragments of interleaved tiles with headers."""

        step = self.tile_size_bytes
        num_tiles = cstream_size // step

        fragment = []
        size = 0

        current_w = None

        for tile_idx in self.iter_interleaved_indices(num_tiles):

            w, fcn = self.get_header(tile_idx)
            tile_size = step

            if fragment:
                mtuwouldoverflow = (size + tile_size > mtu)
                windowbreak = (current_w is not None and w < current_w)

                if mtuwouldoverflow or windowbreak:
                    yield fragment
                    fragment = []
                    size = 0
                    current_w = None

            fragment.append((tile_idx, w, fcn))
            size += tile_size

            current_w = w if current_w is None else max(current_w, w)

        if fragment:
            yield fragment

    def _divide_cstream_into_windows_tiles(self, cstream_size: int):
        """Return list of windows, each containing tile start offsets."""
        
        step = self.tile_size_bytes

        # All tile start positions
        tile_indices = list(range(0, cstream_size, step))

        # Group into windows
        windows = [
            tile_indices[i:i + self.window_size]
            for i in range(0, len(tile_indices), self.window_size)
        ]

        return windows

    def _interleave(self, tiles: list[int]):
        """Interleave list of tile indices by set depth."""
        d = self.interleaving_depth
        n = len(tiles)

        if d <= 1:
            return tiles

        result = []

        # first pass: stride walks
        for offset in range(d):
            for i in range(offset, n, d):
                result.append(tiles[i])

        return result

    def _generate_fragments(self, tiles: list[int], mtu: int):
        """Yield fragments that fit the MTU without wrapping W."""
        # Requirements to calculate W:FCN
        total_tiles = len(tiles)
        total_windows = (total_tiles + self.window_size - 1) // self.window_size
        last_window_index = total_windows - 1
        last_window_len = total_tiles - self.window_size * (total_windows - 1)

        max_tiles_per_frag = mtu // self.tile_size_bytes

        prev_w = -1
        current_fragment = []
        current_frag_header = (-1, -1)

        for tile in tiles:
            w = tile // self.window_size
            pos = tile % self.window_size
            L = last_window_len if w == last_window_index else self.window_size
            fcn = (L - 1) - pos

            if len(current_fragment) == 0:
                current_frag_header = (w, fcn)

            if w >= prev_w and len(current_fragment) < max_tiles_per_frag:
                current_fragment.append(tile)
                prev_w = w
            else:
                # flush existing fragment
                yield current_fragment, current_frag_header
                # start new fragment and include current tile
                current_fragment = [tile]
                current_frag_header = (w, fcn)
                prev_w = w

        # final flush if non-empty
        if current_fragment:
            yield current_fragment, current_frag_header

# =============================================================================
# M A I N
# =============================================================================
    
def main():
    """Main function for the ARQ-FEC device (sender)."""

    print("ARQ-FEC Device (Sender) - Stream Encoding Geometry")
    print("=" * 52)

    # Create UDP socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((IP_ADDR, DEV_PORT))

    print(f"SCHC Packet: {SCHC_PACKET.hex()} ({len(SCHC_PACKET)} bytes)")

    # 1. Encoding
    # =========================================================================

    encoder = StreamEncoder(
        symbol_size_bits=SYMBOL_SIZE_BITS,
        k=SOURCE_BLOCK_SIZE,
        n=ENCODED_BLOCK_SIZE
    )

    print("\n[1] Divide the SCHC Packet into symbols (D-Stream):")

    source_symbols = encoder.divide_packet_into_symbols(SCHC_PACKET)

    assert len(source_symbols) == len(SCHC_PACKET) / encoder.symbol_size_bytes

    print(f"   Produced {len(source_symbols)} source symbols:")
    print("   " + " ".join(
        encoder.get_source_symbol(SCHC_PACKET, idx).hex()
        for idx in source_symbols
    ))

    assert encoder.get_source_symbol(SCHC_PACKET, 0).hex() == "8f"
    assert encoder.get_source_symbol(SCHC_PACKET, 19).hex() == "90"
    assert encoder.get_source_symbol(SCHC_PACKET, 38).hex() == "cc"

    # Calculate size of encoded stream
    cstream_size, trailing_syms = encoder.get_c_stream_size(len(source_symbols))

    assert cstream_size == len(SCHC_PACKET) * 3 // 2
    assert encoder.get_c_stream_size(2) == (3, 0) # SB (2) -> EB (3)
    assert encoder.get_c_stream_size(3) == (4, 1)

    print("\n[2] Produce encoded blocks (C-Stream):")
    print("   " + " ".join(
        encoder.get_encoded_symbol(SCHC_PACKET, idx).hex()
        for idx in range(0, cstream_size, encoder.symbol_size_bytes)
    ))
    print(f"   C-Stream contains {cstream_size} encoded symbols.")
    print(f"   {trailing_syms} trailing source symbol(s).")

    assert encoder.get_encoded_symbol(SCHC_PACKET, 0).hex() == "8f"
    assert encoder.get_encoded_symbol(SCHC_PACKET, 1).hex() == "f8"
    assert encoder.get_encoded_symbol(SCHC_PACKET, 2).hex() == "77" # xor
    assert encoder.get_encoded_symbol(SCHC_PACKET, 3).hex() == "5f"
    assert encoder.get_encoded_symbol(SCHC_PACKET, 32).hex() == "e9" # xor

    # 2. Fragmentation
    # =========================================================================

    fragmenter = StreamFragmenter(
        window_size=WINDOW_SIZE,
        tile_size_bits=TILE_SIZE,
        interleaving_depth=INTERLEAVING_DEPTH
    )

    # A) Assembler
    # -------------------------------------------------------------------------

    print(f"\n[3] Divide the C-Stream into tiles - {WINDOW_SIZE} per window:")

    windows_tiles = fragmenter._divide_cstream_into_windows_tiles(cstream_size)

    for w in range(len(windows_tiles)):
        print(f"   W{w}: {windows_tiles[w]}")
    # print(f"   {len(windows_tiles)} windows (window size is {WINDOW_SIZE}).")

    assert len(windows_tiles) == math.ceil(
        (len(SCHC_PACKET) * 3 // 2) / WINDOW_SIZE)
    # assert len(windows_tiles) == 9 # 58/7
    assert len(windows_tiles[0]) == WINDOW_SIZE

    print("\n[4] Flatten & apply interleaving:")

    tiles = [idx for window in windows_tiles for idx in window] # flatten
    interleaved_tiles = fragmenter._interleave(tiles)

    print(f"   {interleaved_tiles}.")

    # B) Transporter
    # -------------------------------------------------------------------------

    print(f"\n[5] Divide into fragments (MTU={MTU_SIZE_BYTES}); "
          "non-decreasing W:")

    visual_procedure_fragments = []

    for frag_tiles, frag_hdr in fragmenter._generate_fragments(
        interleaved_tiles, MTU_SIZE_BYTES
    ):
        w, fcn = frag_hdr
        print(f"   {w}:{fcn} {frag_tiles}")

        buf = bytearray()
        for tile_idx in frag_tiles:
            sym = encoder.get_encoded_symbol(
                SCHC_PACKET,
                tile_idx * fragmenter.tile_size_bytes
            )
            buf.extend(sym)

        visual_procedure_fragments.append(bytes(buf))
        # print("       |", " ".join(f"{b:02x}" for b in buf))

    # C-like procedure
    # ----------------

    print(f"\n[6] Fragments payload from C-Stream (MTU={MTU_SIZE_BYTES}):")

    fragments = []
    
    for fragment in fragmenter.iter_fragments(
        cstream_size=cstream_size,
        mtu=MTU_SIZE_BYTES
    ):
        # header comes from first tile in fragment (all consistent by design)
        _, w, fcn = fragment[0]
        buf = bytearray()
        for idx, _, _ in fragment:
            sym = encoder.get_encoded_symbol(
                SCHC_PACKET,
                idx * encoder.symbol_size_bytes
            )
            buf.extend(sym)

        fragments.append(bytes(buf))
        print(f"   {w}:{fcn} |", " ".join(f"{b:02x}" for b in buf))

    for i in range(len(fragments)):
        assert fragments[i] == visual_procedure_fragments[i]


if __name__ == "__main__":
    main()
