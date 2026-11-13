#!/usr/bin/env python3
"""
SCHC Server (Receiver) - No FEC (control case)
Implements the receiver side of plain SCHC Ack-on-Error fragmentation (RFC 8724),
with the SCHC Compound ACK (RFC 9441), and without any FEC decoding.
Receives fragmented SCHC packets via loopback UDP and attempts reassembly.
"""

import socket
import math
import zlib
import time

from config import *
from colors import Color

class NoFecReassembler:
    """Plain SCHC reassembler (no decoding): tiles are put back in order."""

    def __init__(self, window_size: int, tile_size_bits: int, w_field_size: int):
        self.window_size = window_size
        self.tile_size_bytes = tile_size_bits // 8
        self.w_field_size = w_field_size
        self.fcn_field_size = max(1, math.ceil(math.log2(self.window_size)))
        self.header_size_bytes = (self.w_field_size + self.fcn_field_size + 7) // 8

        self.tiles = {}          # tile index -> tile bytes
        self.last_window = None  # W of the All-1 (window of the last tile)
        self.rcs = None          # RCS carried in the All-1

    def parse_fragment(self, data: bytes) -> tuple[int, int, bytes]:
        """Extract w, fcn, and payload from fragment bytes."""

        header_int = int.from_bytes(data[:self.header_size_bytes], byteorder="big")

        w = header_int >> self.fcn_field_size
        fcn = header_int & ((1 << self.fcn_field_size) - 1)

        return w, fcn, data[self.header_size_bytes:]

    def get_tile_index(self, w: int, fcn: int) -> int:
        """Convert W:FCN to flat tile index in the SCHC packet."""
        return (w + 1) * self.window_size - fcn - 1

    def place_tiles(self, w: int, fcn: int, payload: bytes) -> None:
        """Place the consecutive tiles of a Regular Fragment, starting at W:FCN."""

        step = self.tile_size_bytes
        base_index = self.get_tile_index(w, fcn)

        for offset in range(len(payload) // step):
            self.tiles[base_index + offset] = payload[offset * step:(offset + 1) * step]

    def get_schc_packet(self):
        """Concatenate tiles 0..highest received; None if there is a gap."""

        if not self.tiles:
            return None

        packet = bytearray()
        for idx in range(max(self.tiles) + 1):
            if idx not in self.tiles:
                return None
            packet.extend(self.tiles[idx])

        return bytes(packet)

    def is_complete(self) -> bool:
        """The receiver doesn't know the packet length: the RCS tells if the tail is there."""

        packet = self.get_schc_packet()
        if packet is None or self.rcs is None:
            return False

        computed = zlib.crc32(packet) & 0xFFFFFFFF
        return computed == int.from_bytes(self.rcs, byteorder="big")

    def build_compound_ack(self) -> bytes:
        """SCHC Compound ACK (C=0), RFC 9441: W1 | C=0 | Bitmap1 | W2 | Bitmap2 | ... | zero padding.

        Lists every window up to the All-1 W that has missing tiles. Bitmap bit i is
        the tile at position i of the window (FCN = WINDOW_SIZE - 1 - i).
        """

        m = self.w_field_size
        ws = self.window_size

        # Windows with missing tiles; windows not listed have none
        entries = []
        for w in range(self.last_window + 1):
            bitmap = ""
            for i in range(ws):
                bitmap += "1" if (w * ws + i) in self.tiles else "0"
            if "0" in bitmap:
                entries.append((w, bitmap))

        # Nothing missing but RCS failed (cannot happen with loss only): report the last window
        if not entries:
            entries.append((self.last_window, "1" * ws))

        bits = ""
        for w, bitmap in entries:
            bits += format(w, f"0{m}b")
            if len(bits) == m:
                bits += "0" # C=0, only after the first W
            bits += bitmap

        # Zero padding to a byte boundary (also acts as the M zero bits terminator)
        bits += "0" * ((8 - len(bits) % 8) % 8)

        return int(bits, 2).to_bytes(len(bits) // 8, byteorder="big")

    def build_c1_ack(self) -> bytes:
        """SCHC ACK C=1: W (last window) | C=1 | zero padding."""

        bits = format(self.last_window, f"0{self.w_field_size}b") + "1"
        bits += "0" * ((8 - len(bits) % 8) % 8)

        return int(bits, 2).to_bytes(len(bits) // 8, byteorder="big")

def main(ready_event=None):
    """Main function for the SCHC application server (receiver), no FEC."""

    print(f"{Color.PURPLE}SCHC App Server (Receiver) - No FEC (Ack-on-Error){Color.ENDC}")
    print(Color.PURPLE, "=" * 52, Color.ENDC)

    reassembler = NoFecReassembler(
        window_size=WINDOW_SIZE,
        tile_size_bits=TILE_SIZE,
        w_field_size=W_FIELD_SIZE
    )

    # Create UDP socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((IP_ADDR, APP_PORT))
    # Inactivity timeout: must outlast all device retries (each waits RX_TIMEOUT - 1)
    sock.settimeout(RX_TIMEOUT * MAX_ATTEMPTS)

    if ready_event is not None:
        ready_event.set()

    print(f"{Color.PURPLE}Listening on {IP_ADDR}:{APP_PORT}...{Color.ENDC}")

    retx_called = False
    all1_received = False

    # Initialize statistics
    stats_regular_recvd = 0
    stats_c0_sent = 0
    stats_c1_sent = 0
    stats_ackreq_recvd = 0
    stats_decodeable_before_retx = False
    stats_decodeable = False
    stats_rcs_ok = False
    timed_out = False # Session ended by inactivity timeout (session failed)

    try:

        while True:
            try:
                # -- Reception -------------------------------------------------

                packet, dev = sock.recvfrom(RX_BUFFER_SIZE)

                print(f"\n{Color.PURPLE}RX:{Color.ENDC}", packet)

                w, fcn, payload = reassembler.parse_fragment(packet)
                fcn_all_ones = (1 << reassembler.fcn_field_size) - 1
                w_all_ones = (1 << reassembler.w_field_size) - 1

                # Header only (no payload): Sender-Abort or ACK-REQ
                is_sender_abort = (len(payload) == 0 and w == w_all_ones and fcn == fcn_all_ones)
                is_ack_req = (len(payload) == 0 and fcn == 0)
                is_all1 = (len(payload) > 0 and fcn == fcn_all_ones)

                if is_sender_abort:
                    print(f"   {Color.RED}SENDER-ABORT{Color.ENDC} - device gave up")
                    break

                if is_ack_req:
                    stats_ackreq_recvd += 1
                    print("   Received ACK-REQ.")

                elif is_all1:
                    print("   Received ALL-1 fragment.")
                    all1_received = True
                    reassembler.last_window = w
                    reassembler.rcs = payload[:4] # RCS Size is 4 bytes

                elif len(payload) > 0:
                    # Process regular fragments
                    print(f"   w={w}, fcn={fcn}, payload={payload.hex()}")
                    reassembler.place_tiles(w, fcn, payload)
                    stats_regular_recvd += 1

                else:
                    print("   Ignoring unknown message.")
                    continue

                # Nothing can be answered before the All-1 (W of last tile, RCS)
                if not all1_received:
                    continue

                # -- Reassembly check ------------------------------------------

                complete = reassembler.is_complete()
                if complete:
                    print("   ! All tiles received !")
                    stats_decodeable = True
                    if not retx_called:
                        stats_decodeable_before_retx = True

                # Regular fragments are not answered, except a retransmitted one
                # that completes the packet (C=1 sent right away, as in server_stream.py)
                is_retx_frag = retx_called and not (is_all1 or is_ack_req)

                if complete:
                    print(f"   SCHC Packet = {reassembler.get_schc_packet().hex()}.")
                    print(f"   {Color.BOLD}{Color.GREEN}>>> RCS Check OK !!!{Color.ENDC}")
                    stats_rcs_ok = True

                    ack = reassembler.build_c1_ack()
                    print("    Sending ACK C=1:", ack.hex())
                    sock.sendto(ack, (IP_ADDR, DEV_PORT))
                    stats_c1_sent += 1
                    break

                # Not complete: only the All-1 / ACK-REQ get an answer
                if not (is_all1 or is_ack_req):
                    continue

                ack = reassembler.build_compound_ack()
                print(f"   {Color.BOLD}{Color.YELLOW}>>> Sending ACK C=0:{Color.ENDC}", ack.hex())
                sock.sendto(ack, (IP_ADDR, DEV_PORT))
                time.sleep(0.01) # Small delay after Tx
                stats_c0_sent += 1
                retx_called = True

                # -- End of main loop ------------------------------------------

            except socket.timeout:
                print(f"   {Color.BOLD}{Color.RED}Timeout reached{Color.ENDC} - no more packets expected")
                timed_out = True
                break
            except Exception as e:
                print(f"   Error receiving: {e}")
                break

    except Exception as e:
        print(f"Error: {e}")
    finally:
        sock.close()
        print("\nServer shutting down...")

    # Return statistics
    return dict(
        regular_recvd=stats_regular_recvd,
        all1_recvd=all1_received,
        c1_sent=stats_c1_sent,
        c0_sent=stats_c0_sent,
        ackreq_recvd=stats_ackreq_recvd,
        retx_called=retx_called,
        decodeable_before_retx=stats_decodeable_before_retx, # All tiles received before any retransmission
        decodeable=stats_decodeable,                         # All tiles received
        rcs_ok=stats_rcs_ok,
        timed_out=timed_out,
        early_ack_sent=0, # No early ACK (W=1, C=1) without FEC
    )

if __name__ == "__main__":
    main()
