#!/usr/bin/env python3
"""
ARQ-FEC Device (Sender) - Matrix Encoding Geometry
Implements the sender side of ARQ-FEC with matrix encoding geometry.
Sends fragmented encoded SCHC packets via loopback UDP.
"""

import socket
import math
import time
import zlib
import os
import pickle
from enum import Enum
import reedsolo
import select
from bitstring import BitArray
from itertools import groupby

from config_matrix import *
from colors import Color


class MsgType(Enum):
    SCHC_NotValid = -1
    SCHC_ACK = 1
    SCHC_ReceiverAbort = 2

class States(Enum):
    TX_INIT = 1
    TX_SEND = 2
    TX_WAIT_X_SESSION_ACK = 3
    TX_RESEND_MISSING_TILES = 4
    TX_END = 5

# =============================================================================
# E N C O D E R
# =============================================================================

class MatrixEncoder:
    """Matrix encoding geometry encoder for ARQ-FEC."""

    def __init__(self, symbol_size_bits: int, tile_size: int, overhead: float):
        self.symbol_size_bits           = symbol_size_bits # m bits
        self.symbol_size_bytes          = symbol_size_bits // 8
        self.s                          = tile_size                 # bytes
        self.overhead                   = overhead
        self.schc_packet_len            = len(SCHC_PACKET) * 8      # bits
        self.k                          = math.floor(self.schc_packet_len/(self.s * self.symbol_size_bits))
        self.residual_coding_bits_count = self.schc_packet_len % (self.s * self.symbol_size_bits) 
        self.r                          = math.ceil(self.k * self.overhead)
        self.n                          = self.k + self.r

    def divide_packet_into_dmatrix(self, schc_packet: bytes) ->tuple[bytes, bytes]:
        """"Divide SCHC packet into D-matrix."""
        step        = self.symbol_size_bytes
        positions   = [i for i in range(0, len(schc_packet), step)]
        symbols     = [schc_packet[idx : idx + step] for idx in positions] 

        print(f"   Produced {len(positions)} source symbols:")
        print("   " + " ".join(
            self.get_source_symbol(SCHC_PACKET, idx).hex()
            for idx in positions
        ))
        print("")
        print("   D-matrix")
        self.data_matrix = []

        for i in range(self.s):
            start = i * self.k
            end = start + self.k
            row = symbols[start:end]
            self.data_matrix.append(row)

        for row in self.data_matrix:
            # Si son bytes y quieres verlos en hexadecimal:
            print("   " +" ".join(b.hex() for b in row))

        self.residual_coding_bits = symbols[self.s * self.k : ]
        print("")
        print("   Residual Coding bits: " +" ".join(b.hex() for b in self.residual_coding_bits))

        rcs = self.calculate_crc32(schc_packet)

        return self.residual_coding_bits, rcs

    def get_source_symbol(self, schc_packet: bytes, index: int):
        """Get source symbol from (virtual) D-matrix."""
        if index < 0: # Enable negative indices
            index = len(schc_packet) - (index * -1)
        return schc_packet[index:index+self.symbol_size_bytes]

    def create_cmatrix(self):
        codec = reedsolo.RSCodec(self.r)
        self.encoded_matrix = []

        for row in self.data_matrix:
            # codec.encode() toma los K bytes y retorna un objeto bytes de longitud N (datos + paridad)
            row_bytes = b"".join(row)
            encoded_row = codec.encode(row_bytes)
            row_list = [bytes([b]) for b in encoded_row]
            self.encoded_matrix.append(row_list)

        print("")
        print("   C-matrix")
        for row in self.encoded_matrix:
            # Si son bytes y quieres verlos en hexadecimal:
            print("   " +" ".join(b.hex() for b in row))

        return self.k

    def create_encoded_schc_packet(self):    
        self.encoded_schc_packet = []
        for col_idx in range(self.n):
            for row_idx in range(self.s):
                symbol = self.encoded_matrix[row_idx][col_idx]
                self.encoded_schc_packet.append(symbol)

        print("")
        print("   Encoded SCHC Packet")
        print("   " +" ".join(b.hex() for b in self.encoded_schc_packet))   

        return self.encoded_schc_packet

    def calculate_crc32(self, data: bytes) -> bytes:
        """Return CRC-32 (poly 0xEDB88320) for given data."""
        #data = b"".join(data)
        val = zlib.crc32(data) & 0xFFFFFFFF
        return val.to_bytes(4, "big")

# =============================================================================
# F R A G M E N T E R
# =============================================================================
    
class MatrixFragmenter:
    """Matrix encoding geometry assembler and fragmenter for ARQ-FEC."""

    def __init__(self, window_size: int, tile_size: int, w_field_size: int, mtu: int , residual: list[bytes], k: int, rcs: bytes):
        self._running_machine       = True
        self.window_size            = window_size                                          # tiles
        self.tile_size_bytes        = tile_size                                        # bytes
        self.w_field_size           = w_field_size                                        # bits
        self.fcn_field_size         = max(1, math.ceil(math.log2(self.window_size)))    # bits
        self._mtu                   = mtu
        self.residual_coding_bits   = residual
        self._k                     = k
        self._rcs                   = rcs

        self.tiles = []
        self._currentWindow     = 0
        self._currentFcn        = (window_size)-1
        self._currentTile_ptr   = 0

        self._current_state     = States.TX_SEND

        # Create UDP socket
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((IP_ADDR, DEV_PORT))
        self.sock.settimeout(RX_TIMEOUT - 1)  # Set timeout (shorter than server's)

        unos = '1' * self.window_size
        self.bitmap_array = [BitArray(bin=unos) for _ in range(2**self.w_field_size)]

        self.stats_regular_count        = 0
        self.stats_regular_tx_attempts  = 0
        self.stats_regular_lost         = 0
        self.stats_all1_lost            = 0
        self.stats_c0_recvd             = 0
        self.stats_c1_recvd             = 0
        self.stats_ackreq_sent          = 0
        self.stats_attempts             = 0      # All-1 + ACK-REQ sent (spec Attempts counter)
        self.stats_aborted              = False  # True if the device gave up (Sender-Abort)

        # Retransmission Timer: shorter than the server's inactivity timeout (as in device_stream.py)
        self._retx_timeout              = RX_TIMEOUT - 1
        self._retx_timer_start          = time.monotonic()

    def divide_encoded_schc_packet_into_tiles(self, encoded_schc_packet: list[bytes]):

        n = len(encoded_schc_packet)//self.tile_size_bytes
        for i in range(n):
            start = i * self.tile_size_bytes
            end = start + self.tile_size_bytes
            row = encoded_schc_packet[start:end]
            self.tiles.append(row)
            print("   Tile " + str(i+1) + ": " + " ".join(b.hex() for b in row)) 

        self._nFullTiles = len(self.tiles)
        self._nWindows   = (self._nFullTiles + self.window_size - 1) // self.window_size
        self._lastWindow = (self._nFullTiles - 1) // self.window_size  # W of the last tile (used in All-1 and ACK REQ)

        # Number of Regular Fragments needed to send the whole encoded SCHC packet (as in device_stream.py)
        payload_available_in_bytes  = self._mtu - 1 - (self.w_field_size + self.fcn_field_size) // 8
        tiles_per_fragment          = payload_available_in_bytes // self.tile_size_bytes
        if tiles_per_fragment == 0:
            raise ValueError(f"TILE_SIZE={self.tile_size_bytes} does not fit in MTU={self._mtu} (needs MTU >= TILE_SIZE + 2)")
        self.stats_regular_count = math.ceil(self._nFullTiles / tiles_per_fragment)

        return self.tiles

    def send_regular_fragment(self, packet: bytes):
        """Send a Regular SCHC Fragment, simulating channel loss."""
        self.stats_regular_tx_attempts += 1

        if is_packet_lost():
            print(f"   {Color.RED}LOST FRAG:{Color.ENDC}", packet.hex())
            self.stats_regular_lost += 1
            return

        print(f"   {Color.BLUE}TX FRAG:{Color.ENDC}", packet.hex())
        self.sock.sendto(packet, (IP_ADDR, APP_PORT))
        time.sleep(0.01) # Small delay between fragments

    def send_all1(self):
        """Send the All-1 SCHC Fragment (simulating channel loss if allowed) and wait for an ACK."""
        fcn_mask            = (1 << self.fcn_field_size) - 1
        header_int          = (self._lastWindow << self.fcn_field_size) | (self.window_size & fcn_mask)
        schc_header_all_1   = bytes([header_int])
        residual            = b"".join(self.residual_coding_bits)
        packet_all_1        = schc_header_all_1 + self._rcs + residual

        self.stats_attempts     += 1
        self._retx_timer_start  = time.monotonic()
        self._current_state     = States.TX_WAIT_X_SESSION_ACK

        if ALLOW_ALL1_LOSS and is_packet_lost():
            print(f"   {Color.RED}LOST ALL1:{Color.ENDC}", packet_all_1.hex())
            self.stats_all1_lost += 1
            return

        print(f"   {Color.BLUE}TX ALL1:{Color.ENDC}", packet_all_1.hex())
        self.sock.sendto(packet_all_1, (IP_ADDR, APP_PORT))
        time.sleep(0.01) # Small delay between fragments

    def send_sender_abort(self):
        """Give up: send a SCHC Sender-Abort (never lost) and stop the state machine."""
        w_mask          = (1 << self.w_field_size) - 1
        fcn_mask        = (1 << self.fcn_field_size) - 1
        header_int      = (w_mask << self.fcn_field_size) | fcn_mask  # W all ones, FCN all ones (RFC 8724)
        packet          = bytes([header_int])

        print(f"   {Color.RED}TX SENDER-ABORT:{Color.ENDC}", packet.hex())
        self.sock.sendto(packet, (IP_ADDR, APP_PORT))

        self.stats_aborted      = True
        self._current_state     = States.TX_END
        self._running_machine   = False

    def run(self):
        """State Machine"""
        while self._running_machine:

            if self._current_state == States.TX_SEND:

                # Calculate how many tiles fit in a message
                payload_available_in_bytes  = self._mtu - 1 - (self.w_field_size + self.fcn_field_size) // 8 
                payload_available_in_tiles  = int(payload_available_in_bytes/self.tile_size_bytes)
                n_remaining_tiles           = self._nFullTiles - self._currentTile_ptr 

                # After sending the message, are there any tiles left to send?
                if n_remaining_tiles>payload_available_in_tiles: # If there are any tiles left to send
                    # Extract the tiles to be sent
                    temp_tiles = []
                    n_tiles_to_send = 0
                    for row in self.tiles[self._currentTile_ptr:(self._currentTile_ptr + payload_available_in_tiles)]:
                        for symbols in row:
                            temp_tiles.append(symbols)
                        n_tiles_to_send = n_tiles_to_send + 1

                    # Create the SCHC header and the tile payload 
                    schc_payload    = b"".join(temp_tiles)
                    fcn_mask        = (1 << self.fcn_field_size) - 1
                    header_int      = (self._currentWindow << self.fcn_field_size) | (self._currentFcn & fcn_mask)
                    schc_header     = bytes([header_int])
                    k_value         = bytes([self._k])
                    packet          = schc_header + k_value + schc_payload

                    # Send this fragment to app. server
                    self.send_regular_fragment(packet)

                    # Update tile, fcn, and window pointers
                    self._currentTile_ptr    = self._currentTile_ptr + n_tiles_to_send;
                    if self._currentFcn - n_tiles_to_send <= -1:
                        self._currentFcn = (self._currentFcn - n_tiles_to_send) + self.window_size
                        self._currentWindow = self._currentWindow + 1
                    else:
                        self._currentFcn = self._currentFcn - n_tiles_to_send       

                else: #There are no tiles left to send
                    # Extract the tiles to be sent
                    temp_tiles = []
                    n_tiles_to_send = 0
                    for row in self.tiles[self._currentTile_ptr:(self._currentTile_ptr + payload_available_in_tiles)]:
                        for symbols in row:
                            temp_tiles.append(symbols)
                        n_tiles_to_send = n_tiles_to_send + 1

                    # Create the SCHC header and the tile payload 
                    schc_payload    = b"".join(temp_tiles)
                    fcn_mask        = (1 << self.fcn_field_size) - 1
                    header_int      = (self._currentWindow << self.fcn_field_size) | (self._currentFcn & fcn_mask)
                    schc_header     = bytes([header_int])
                    k_value         = bytes([self._k])
                    packet          = schc_header + k_value + schc_payload

                    # Send this fragment to app. server
                    self.send_regular_fragment(packet)

                    # Update tile, fcn, and window pointers
                    self._currentTile_ptr    = self._currentTile_ptr + n_tiles_to_send;
                    self._currentFcn         = self._currentFcn - n_tiles_to_send;

                    # Sending All-1
                    self.send_all1()


            elif self._current_state == States.TX_WAIT_X_SESSION_ACK:
                time.sleep(0.1)

                # Retransmission Timer expired without any ACK: resend All-1, or give up
                if time.monotonic() - self._retx_timer_start > self._retx_timeout:
                    print("   Timeout reached - no ack")
                    if self.stats_attempts < MAX_ATTEMPTS:
                        self.send_all1()
                    else:
                        self.send_sender_abort()
                        break


            # Processes incoming messages
            self.process_receive_msg()



        return 

    def process_receive_msg(self):

        ready_to_read, _, _ = select.select([self.sock], [], [], 0.1)
        if self.sock in ready_to_read:
            # ¡Llegó un mensaje! Lo leemos de inmediato sin bloquear el hilo
            try:
                data, addr = self.sock.recvfrom(1024)
                #print(f"Mensaje recibido de {addr}: {data}")

                if self._current_state == States.TX_SEND:
                    msg_type = self.get_msg_type(data)
                    if msg_type == MsgType.SCHC_ACK:
                        w, c = self.decode_msg(data, msg_type)
                        
                        if w==1 and c==1:
                            # Sending All-1
                            self.send_all1()

                elif self._current_state == States.TX_WAIT_X_SESSION_ACK:
                    msg_type = self.get_msg_type(data)
                    if msg_type == MsgType.SCHC_ACK:
                        w, c = self.decode_msg(data, msg_type)
                    
                        if w==3 and c==1:
                            self._current_state = States.TX_END
                            self._running_machine = False
                            
                        elif c==0:
                            # Safeguard: no more retransmission rounds allowed
                            if self.stats_attempts >= MAX_ATTEMPTS:
                                self.send_sender_abort()
                                return

                            ### Sending missing tiles #######
                            # Calculate how many tiles fit in a message
                            payload_available_in_bytes  = self._mtu - 1 - (self.w_field_size + self.fcn_field_size) // 8 
                            payload_available_in_tiles  = int(payload_available_in_bytes/self.tile_size_bytes)

                            # Extract the tiles to be sent
                            missed_tiles = []
                            for w, bitmap in enumerate(self.bitmap_array):
                                for pos,tail in enumerate(bitmap):
                                    tile_idx = w*self.window_size + pos
                                    # Ignore positions past the last tile (a receiver that does not know k reports whole windows)
                                    if tail == 0 and tile_idx < self._nFullTiles:
                                        missed_tiles.append(tile_idx)

                            consecutivos, no_consecutivos = self.detect_consecutive(missed_tiles, payload_available_in_tiles)

                            # Isolated tiles are sent alone, one tile per fragment
                            groups_to_send = consecutivos + [[tile] for tile in no_consecutivos]

                            for tiles_to_send in groups_to_send:
                                schc_payload = []
                                temp_payload = []
                                for tile in tiles_to_send:
                                    temp_payload.append(self.tiles[tile])

                                # Prepare SCHC regular fragment    
                                schc_payload = b''.join(byte for sublista in temp_payload for byte in sublista)
                                w   = math.floor((tiles_to_send[0])/self.window_size)
                                fcn = (math.floor((tiles_to_send[0])/self.window_size) + 1) * self.window_size - (tiles_to_send[0] + 1)       
                                fcn_mask        = (1 << self.fcn_field_size) - 1
                                header_int      = (w << self.fcn_field_size) | (fcn & fcn_mask)
                                schc_header     = bytes([header_int])
                                k_value         = bytes([self._k])
                                packet          = schc_header + k_value + schc_payload

                                # Send this fragment to app. server
                                self.send_regular_fragment(packet)


                            # Sending SCHC ACK REQ
                            w   = self._lastWindow
                            fcn = 0       
                            fcn_mask        = (1 << self.fcn_field_size) - 1
                            header_int      = (w << self.fcn_field_size) | (fcn & fcn_mask)
                            schc_header     = bytes([header_int])
                            packet          = schc_header

                            # Send this fragment to app. server
                            print(f"   {Color.BLUE}TX ACK REQ:{Color.ENDC}", packet.hex())
                            self.stats_ackreq_sent += 1
                            self.stats_attempts += 1
                            self._retx_timer_start = time.monotonic()
                            self.sock.sendto(packet, (IP_ADDR, APP_PORT))
                            time.sleep(0.01) # Small delay between fragments

                            self._current_state = States.TX_WAIT_X_SESSION_ACK


            except OSError: # e.g. BlockingIOError, or ConnectionRefusedError if the server is gone
                pass

    def get_msg_type(self, data: bytes) -> int:
        """Get the message type based in the headers of the message"""
        schc_header = data[0]
        msg_len     = len(data)
        w           = schc_header >> self.fcn_field_size
        c           = (schc_header >> (self.fcn_field_size-1)) & 1
    
        if msg_len>1 and c==1:
            # SCHC Receiver-Abort
            print(f"   {Color.RED}RX RECEIVER-ABORT:{Color.ENDC}", data.hex())
            return MsgType.SCHC_ReceiverAbort
        elif msg_len==1 and c==1:
            # SCHC ACK (c=1)
            print(f"   {Color.RED}RX ACK:{Color.ENDC}", data.hex())
            return MsgType.SCHC_ACK
        elif c==0:
            # SCHC ACK (c=0)
            print(f"   {Color.RED}RX COMPOUND-ACK:{Color.ENDC}", data.hex())
            return MsgType.SCHC_ACK
        else:
            print("Message Type: Not valid")
            return MsgType.SCHC_NotValid

    def decode_msg(self, data: bytes, type_msg: int) ->tuple[int, int]:

        if type_msg==MsgType.SCHC_ACK:
            schc_header = data[0]
            w           = schc_header >> self.fcn_field_size
            c           = (schc_header >> (self.fcn_field_size-1)) & 1

            if c==1:
                # Only the end-of-session ACK (W=3) counts as C=1, as in device_stream.py
                if w==3:
                    self.stats_c1_recvd += 1
                return w, c
            else:
                self.stats_c0_recvd += 1

                # Windows not listed in this ACK have no missing tiles: forget previous bitmaps
                unos = '1' * self.window_size
                self.bitmap_array = [BitArray(bin=unos) for _ in range(2**self.w_field_size)]

                schc_header_bitArray = BitArray(data)  # works on bitstring 4.x and 5.x
                del schc_header_bitArray[self.w_field_size]
                for bitmap in schc_header_bitArray.cut(self.w_field_size + self.window_size):
                    if len(bitmap) < self.w_field_size + self.window_size:
                        break
                    w       = bitmap[0:self.w_field_size].uint
                    bits    = bitmap[self.w_field_size:]
                    self.bitmap_array[w] = bits


                return w, c       

    def detect_consecutive(self, missed_tiles, tiles_by_payload):
        lista = sorted(set(missed_tiles))
        secuencias = []
        aislados = []
        
        # x[1] es el valor, x[0] es el índice. 
        # Al restarlos, los números consecutivos generan la misma clave.
        for _, grupo in groupby(enumerate(lista), key=lambda x: x[1] - x[0]):
            elementos = [x[1] for x in grupo]
            
            # Segmentar la secuencia en trozos de tamaño máximo tiles_by_payload
            for i in range(0, len(elementos), tiles_by_payload):
                subgrupo = elementos[i:i+tiles_by_payload]
                
                if len(subgrupo) > 1:
                    secuencias.append(subgrupo)
                else:
                    aislados.append(subgrupo[0])
                    
        return secuencias, aislados

    def get_stats(self):
        return (
            self.stats_regular_count,
            self.stats_regular_tx_attempts,
            self.stats_regular_lost,
            self.stats_all1_lost,
            self.stats_c0_recvd,
            self.stats_c1_recvd,
            self.stats_ackreq_sent,
            self.stats_attempts,
            self.stats_aborted,
        )

    def stop_socket(self):
        self.sock.close()

# =============================================================================
# M A I N
# =============================================================================
    
def main():
    """Main function for the ARQ-FEC device (sender)."""

    print(f"{Color.BLUE}ARQ-FEC Device (Sender) - Matrix Encoding Geometry{Color.ENDC}")
    print(Color.BLUE, "=" * 52, Color.ENDC)

    print(f"SCHC Packet: {SCHC_PACKET.hex()} ({len(SCHC_PACKET)} bytes)")

    # 1. Encoding
    # =========================================================================

    encoder = MatrixEncoder(
        symbol_size_bits=SYMBOL_SIZE_BITS,
        tile_size=TILE_SIZE,
        overhead=OVERHEAD
    )

    print("\n[1] Divide the SCHC Packet into a matrix (D-matrix):")
    residual_bits, rcs = encoder.divide_packet_into_dmatrix(SCHC_PACKET)

    print("\n[2] Produce encoded blocks (C-matrix) and the encoded SCHC packet:")
    k_value = encoder.create_cmatrix()
    encoded_schc_packet = encoder.create_encoded_schc_packet()

    # 2. Fragmentation
    # =========================================================================

    fragmenter = MatrixFragmenter(
        window_size=WINDOW_SIZE,
        tile_size=TILE_SIZE,
        w_field_size=W_FIELD_SIZE,
        mtu=MTU_SIZE_BYTES,
        residual=residual_bits,
        k=k_value,
        rcs=rcs
    )

    # A) Assembler
    # -------------------------------------------------------------------------
    print(f"\n[3] Divide the SCHC encoded packet into tiles - {WINDOW_SIZE} per window:")
    tiles = fragmenter.divide_encoded_schc_packet_into_tiles(encoded_schc_packet)


    # B) Transporter
    # -------------------------------------------------------------------------
    print(f"\n[4] Start Machine")
    fragmenter.run();

    print(f"\n[5] Closing Socket")
    fragmenter.stop_socket()

    [stats_regular_count, stats_regular_tx_attempts, stats_regular_lost, stats_all1_lost, stats_c0_recvd, stats_c1_recvd, stats_ackreq_sent, stats_attempts, stats_aborted] = fragmenter.get_stats()

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
