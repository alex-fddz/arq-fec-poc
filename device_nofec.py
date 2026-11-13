#!/usr/bin/env python3
"""
SCHC Device (Sender) - No FEC (control case)
Implements the sender side of plain SCHC Ack-on-Error fragmentation (RFC 8724),
with the SCHC Compound ACK (RFC 9441), and without any FEC encoding.
Sends fragmented SCHC packets via loopback UDP.
"""

import socket
import math
import time
import zlib
import os

from config import *
from colors import Color

# =============================================================================
# F R A G M E N T E R
# =============================================================================

class NoFecFragmenter:
    """Plain SCHC fragmenter (no encoding): the SCHC packet is cut into tiles."""

    def __init__(self, window_size: int, tile_size_bits: int, w_field_size: int):
        self.window_size = window_size
        self.tile_size_bytes = tile_size_bits // 8
        self.w_field_size = w_field_size
        self.fcn_field_size = max(1, math.ceil(math.log2(self.window_size)))
        self.header_size_bytes = (self.w_field_size + self.fcn_field_size + 7) // 8

    def get_header(self, tile_idx: int):
        """Compute SCHC fragment header values (W, FCN) from a tile index."""

        w = tile_idx // self.window_size
        pos = tile_idx % self.window_size
        fcn = self.window_size - 1 - pos

        return w, fcn

    def iter_fragments(self, num_tiles: int, mtu: int):
        """Yield lists of consecutive tile indices, as many as fit in the MTU."""

        tiles_per_fragment = (mtu - self.header_size_bytes) // self.tile_size_bytes

        for start in range(0, num_tiles, tiles_per_fragment):
            end = min(start + tiles_per_fragment, num_tiles)
            yield list(range(start, end))

    def new_fragment(self, w: int, fcn: int) -> bytearray:
        """Make a fragment with header bytes (W, then FCN)."""

        header_int = (w << self.fcn_field_size) | (fcn & ((1 << self.fcn_field_size) - 1))

        packet = bytearray()
        packet.extend(header_int.to_bytes(self.header_size_bytes, byteorder="big"))
        return packet

    def parse_compound_ack(self, ack: bytes) -> list[int]:
        """Return the tile indices reported missing in a SCHC Compound ACK (C=0).

        Format (RFC 9441): W1 | C=0 | Bitmap1 | W2 | Bitmap2 | ... | zero padding.
        """

        bits = "".join(format(byte, "08b") for byte in ack)
        m = self.w_field_size
        ws = self.window_size

        missing = []
        pos = 0
        first = True

        while True:
            entry_size = m + (1 if first else 0) + ws
            if len(bits) - pos < entry_size:
                break # Only padding left

            w = int(bits[pos:pos + m], 2)
            if not first and w == 0:
                break # M zero bits: end of the Compound ACK

            pos += m
            if first:
                pos += 1 # Skip the C bit (only after the first W)

            bitmap = bits[pos:pos + ws]
            pos += ws

            for i, bit in enumerate(bitmap):
                if bit == "0":
                    missing.append(w * ws + i)

            first = False

        return missing

    def calculate_crc32(self, data: bytes) -> bytes:
        """Return CRC-32 (poly 0xEDB88320) for given data."""
        val = zlib.crc32(data) & 0xFFFFFFFF
        return val.to_bytes(4, "big")

# =============================================================================
# M A I N
# =============================================================================

def main():
    """Main function for the SCHC device (sender), no FEC."""

    print(f"{Color.BLUE}SCHC Device (Sender) - No FEC (Ack-on-Error){Color.ENDC}")
    print(Color.BLUE, "=" * 52, Color.ENDC)

    # Create UDP socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((IP_ADDR, DEV_PORT))
    sock.settimeout(RX_TIMEOUT - 1)  # Retransmission Timer (shorter than server's)

    print(f"SCHC Packet: {SCHC_PACKET.hex()} ({len(SCHC_PACKET)} bytes)")

    # 1. Fragmentation (no encoding: tiles are cut directly from the SCHC packet)
    # =========================================================================

    fragmenter = NoFecFragmenter(
        window_size=WINDOW_SIZE,
        tile_size_bits=TILE_SIZE,
        w_field_size=W_FIELD_SIZE
    )

    tile_size = fragmenter.tile_size_bytes
    num_tiles = math.ceil(len(SCHC_PACKET) / tile_size)
    last_window = (num_tiles - 1) // WINDOW_SIZE # W of last tile

    print(f"\n[1] Divide the SCHC Packet into {num_tiles} tiles - {WINDOW_SIZE} per window:")
    for w in range(last_window + 1):
        first_tile = w * WINDOW_SIZE
        print(f"   W{w}: {list(range(first_tile, min(first_tile + WINDOW_SIZE, num_tiles)))}")

    fragments = list(fragmenter.iter_fragments(num_tiles, MTU_SIZE_BYTES))

    print(f"\n[2] Divide into fragments (MTU={MTU_SIZE_BYTES}):")
    for frag_tiles in fragments:
        w, fcn = fragmenter.get_header(frag_tiles[0])
        print(f"   {w}:{fcn} {frag_tiles}")

    print(f"\n[3] Transmitting fragments via loopback UDP...")
    time.sleep(0.05) # Small delay to let server initialize

    # Initialize statistics
    stats_regular_count = len(fragments)
    stats_regular_tx_attempts = 0
    stats_regular_lost = 0
    stats_all1_lost = 0
    stats_c0_recvd = 0
    stats_c1_recvd = 0
    stats_ackreq_sent = 0
    stats_attempts = 0      # All-1 + ACK-REQ sent (spec Attempts counter)
    stats_aborted = False   # True if the device gave up (Sender-Abort)

    def send_regular_fragment(frag_tiles: list):
        """Send one Regular Fragment (simulating channel loss)."""

        nonlocal stats_regular_tx_attempts
        nonlocal stats_regular_lost

        w, fcn = fragmenter.get_header(frag_tiles[0])
        stats_regular_tx_attempts += 1

        if is_packet_lost():
            print(f"   {Color.RED}LOST {w}:{fcn}.{Color.ENDC}")
            stats_regular_lost += 1
            return

        packet = fragmenter.new_fragment(w, fcn)
        start = frag_tiles[0] * tile_size
        end = (frag_tiles[-1] + 1) * tile_size
        packet.extend(SCHC_PACKET[start:end])

        print(f"   {Color.BLUE}TX:{Color.ENDC}", packet.hex())
        sock.sendto(packet, (IP_ADDR, APP_PORT))
        time.sleep(0.01) # Small delay between fragments

    def send_all1():
        """Send the All-1 Fragment, carrying the RCS (simulating channel loss if allowed)."""

        nonlocal stats_all1_lost
        nonlocal stats_attempts

        stats_attempts += 1

        if ALLOW_ALL1_LOSS and is_packet_lost():
            print(f"   {Color.RED}LOST ALL-1.{Color.ENDC}")
            stats_all1_lost += 1
            return

        all1_packet = fragmenter.new_fragment(w=last_window, fcn=WINDOW_SIZE)
        all1_packet.extend(fragmenter.calculate_crc32(data=SCHC_PACKET))

        print(f"   {Color.BLUE}TX:{Color.ENDC}", all1_packet.hex())
        sock.sendto(all1_packet, (IP_ADDR, APP_PORT))
        time.sleep(0.01) # Small delay between fragments

    def send_ack_req():
        """Send an ACK-REQ (W = last window, FCN = 0, no payload; never lost)."""

        nonlocal stats_ackreq_sent
        nonlocal stats_attempts

        stats_attempts += 1
        stats_ackreq_sent += 1
        packet = fragmenter.new_fragment(w=last_window, fcn=0)
        print(f"   Sending {Color.BLUE}ACK-REQ{Color.ENDC}:", packet.hex())
        sock.sendto(packet, (IP_ADDR, APP_PORT))
        time.sleep(0.01) # Small delay after Tx

    def send_sender_abort():
        """Give up: send a Sender-Abort (W and FCN all ones, no payload; never lost)."""

        nonlocal stats_aborted

        stats_aborted = True
        packet = fragmenter.new_fragment(w=(1 << W_FIELD_SIZE) - 1, fcn=WINDOW_SIZE)
        print(f"   {Color.RED}Sending SENDER-ABORT{Color.ENDC}:", packet.hex())
        sock.sendto(packet, (IP_ADDR, APP_PORT))

    # First pass: every Regular Fragment, then the All-1
    for frag_tiles in fragments:
        send_regular_fragment(frag_tiles)
    send_all1()

    # Now await ACK. Every All-1 or ACK-REQ sent is one attempt; the socket
    # timeout (RX_TIMEOUT - 1) acts as the Retransmission Timer.
    #   C=1     -> done
    #   C=0     -> retransmit the fragments carrying missing tiles, then send ACK-REQ
    #   timeout -> resend All-1
    # Once MAX_ATTEMPTS is reached, the device gives up (Sender-Abort).
    print(f"\n[4] Awaiting ACK...")

    try:
        while True:
            try:
                ack, addr = sock.recvfrom(1024)
            except socket.timeout:
                print("   Timeout reached - no ack")
                if stats_attempts < MAX_ATTEMPTS:
                    send_all1()
                else:
                    send_sender_abort()
                    break
                continue

            c_bit = format(ack[0], "08b")[W_FIELD_SIZE] # C comes right after W

            if c_bit == "1":
                print(f"   {Color.GREEN}Got C=1{Color.ENDC}; All good!")
                stats_c1_recvd += 1
                break

            print(f"   {Color.BLUE}Got C=0{Color.ENDC}:", ack.hex())
            stats_c0_recvd += 1

            if stats_attempts >= MAX_ATTEMPTS:
                send_sender_abort()
                break

            # Ignore bitmap positions past the last tile (last window may be partial)
            missing_tiles = [t for t in fragmenter.parse_compound_ack(ack) if t < num_tiles]
            print(f"   Missing tiles: {missing_tiles}")
            print("   Retransmitting...")

            # Tiles are lost together with their fragment: resend those fragments
            for frag_tiles in fragments:
                if any(t in missing_tiles for t in frag_tiles):
                    send_regular_fragment(frag_tiles)
            send_ack_req()

    except Exception as e:
        print(f"   Error: {e}")
    finally:
        sock.close()
        print("\nDevice shutting down...")

    # Return statistics
    return dict(
        loss_probability=LOSS_PROBABILITY,
        allow_all1_lost=ALLOW_ALL1_LOSS,
        regular_count=stats_regular_count,
        regular_tx_attempts=stats_regular_tx_attempts,
        regular_lost=stats_regular_lost,
        all1_lost=stats_all1_lost,
        c1_recvd=stats_c1_recvd,
        c0_recvd=stats_c0_recvd,
        ackreq_sent=stats_ackreq_sent,
        attempts=stats_attempts,
        aborted=stats_aborted,
    )

def is_packet_lost():
    return int.from_bytes(os.urandom(4),'big')/0x100000000 < LOSS_PROBABILITY


if __name__ == "__main__":
    main()
