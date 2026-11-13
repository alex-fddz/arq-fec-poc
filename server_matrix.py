#!/usr/bin/env python3
"""
ARQ-FEC Server (Receiver) - Matrix Encoding Geometry
Implements the receiver side of ARQ-FEC with matrix encoding geometry.
Receives fragmented encoded SCHC packets via loopback UDP and attempts reconstruction.
"""

import socket
import math
import zlib
import time
import pickle
from enum import Enum
import reedsolo
from bitstring import BitArray
import os

from config_matrix import *
from colors import Color

class MsgType(Enum):
    SCHC_NotValid = -1
    SCHC_Fragment = 1
    SCHC_All1 = 2
    SCHC_SenderAbort = 3
    SCHC_AckReq = 4

class States(Enum):
    RX_INIT = 1
    RX_RCV_WINDOW = 2
    RX_WAIT_X_ALL1 = 3
    RX_WAIT_X_MISSING_FRAG = 4
    RX_END = 5

class MatrixReassembler:
    """Matrix encoding geometry reassembler for ARQ-FEC."""

    def __init__(self, socket, window_size: int, tile_size: int, w_field_size: int, mtu: int, overhead: float):
        self._window_size           = window_size                                          # tiles
        self._tile_size_bytes       = tile_size                                        # bytes
        self._w_field_size          = w_field_size                                        # bits
        self._fcn_field_size        = max(1, math.ceil(math.log2(self._window_size)))    # bits
        self._mtu                   = mtu
        self._overhead              = overhead
        self._cmatrix_initialised   = False
        self._current_state         = States.RX_INIT

        self._currentWindow         = 0
        self._currentFcn            = (self._window_size)-1
        self._currentTile_ptr       = 0   

        self._counter               = 0    
        self._socket                = socket

        self._all_1_payload = []
        self._recv_rcs      = []
        
        unos = '1' * self._window_size
        self.bitmap_array = [BitArray(bin=unos) for _ in range(2**self._w_field_size)]

        # Init Ack "bitmap"
        self.received_frags = set()
        self.retx_called = False
        self.all1_received = False

        # Initialize statistics
        self.stats_regular_recvd    = 0
        self.stats_c1_sent          = 0
        self.stats_early_ack_sent   = 0  # W=1, C=1 ACKs (matrix only)
        self.stats_c0_sent          = 0
        self.stats_ackreq_recvd     = 0
        self.stats_decodeable_before_retx = False
        self.stats_decodeable       = False
        self.stats_rcs_ok           = False

    def process_msg(self, data: bytes) -> bool:
        """Process messages."""

        type_msg = self.get_msg_type(data)

        if type_msg == MsgType.SCHC_SenderAbort:
            print(f"    {Color.RED}RX SENDER-ABORT{Color.ENDC} - device gave up")
            return False

        if self._current_state == States.RX_INIT or self._current_state == States.RX_RCV_WINDOW:


            # ## Usado para perdida determinista ######
            # if self._counter == 1 or self._counter == 2 or self._counter == 3:
            #     print("Discarting message")
            #     self._counter = self._counter + 1
            #     return True
            # self._counter = self._counter + 1

            if type_msg == MsgType.SCHC_Fragment:

                self.stats_regular_recvd += 1
                # Decoding message
                w, fcn, k, payload_len, payload, extra = self.decode_msg(data, type_msg)

                # Extract the value k
                self._r = math.ceil(k * self._overhead)
                self._k = k
                n       = k + self._r

                # Initialise the C-matrix
                if self._cmatrix_initialised == False:
                    self._cmatrix_initialised = True
                    self.initialise_cmatrix(self._tile_size_bytes, n)
                
                # Extract and save the tiles
                tiles = [payload[i : i + self._tile_size_bytes] for i in range(0, payload_len, self._tile_size_bytes)]
                self.save_tiles_in_cmatrix(tiles, w, fcn)
                
                # Check if there are enough symbols
                if self.check_enough_tiles(k):
                    #print("Sending a SCHC ACK with W=1 and C=1")
                    print(f"    {Color.RED}TX ACK :{Color.ENDC}")
                    self.send_ack(1,1)
                    self._current_state = States.RX_WAIT_X_ALL1


            elif type_msg == MsgType.SCHC_All1:
                self.all1_received = True
                # Decoding message
                w, fcn, k, payload_len, payload, recv_rcs = self.decode_msg(data, type_msg)

                self._all_1_payload = payload
                self._recv_rcs      = recv_rcs

                # Safeguard: no Regular Fragment received yet, so k (and the C-matrix) is unknown.
                # Nothing can be done; the device will resend the All-1 or give up.
                if not self._cmatrix_initialised:
                    print(f"   {Color.RED}No regular fragment received yet (k unknown){Color.ENDC}")
                    return True

                # Check if there are enough symbols
                if self.check_enough_tiles(self._k):
                    res = self.get_schc_packet(payload, recv_rcs)

                    if res == True: # RCS is correct
                        #print("Sending a SCHC ACK with W=3 and C=1")
                        print(f"    {Color.RED}TX ACK :{Color.ENDC}")
                        self.send_ack(3,1)
                        self._current_state = States.RX_END
                        return False

                else:
                    self.get_bitmap()
                    #print("Sending a SCHC ACK with W=1 and C=0")
                    print(f"    {Color.RED}TX ACK :{Color.ENDC}")
                    self.send_c0_ack()
                    self._current_state = States.RX_WAIT_X_MISSING_FRAG

        elif self._current_state == States.RX_WAIT_X_ALL1:

            if type_msg == MsgType.SCHC_Fragment:
                self.stats_regular_recvd += 1
                print("Discarting SCHC regular fragment message")

            elif type_msg == MsgType.SCHC_All1:
                self.all1_received = True
                # Decoding message
                w, fcn, k, payload_len, payload, recv_rcs = self.decode_msg(data, type_msg)

                self._all_1_payload = payload
                self._recv_rcs      = recv_rcs
                # Check if there are enough symbols
                if self.check_enough_tiles(self._k):
                    # Extract
                    res = self.get_schc_packet(payload, recv_rcs)

                    if res == True: # RCS is correct
                        # Sending session ACK
                        print(f"    {Color.RED}TX ACK :{Color.ENDC}")
                        self.send_ack(3,1)
                        self._current_state = States.RX_END
                        return False

        elif self._current_state == States.RX_WAIT_X_MISSING_FRAG:
            if type_msg == MsgType.SCHC_Fragment:
                self.stats_regular_recvd += 1
                # Decoding message
                w, fcn, k, payload_len, payload, extra = self.decode_msg(data, type_msg)

                # Extract the value k
                self._r = math.ceil(k * self._overhead)
                self._k = k
                n       = k + self._r

                # Initialise the C-matrix
                if self._cmatrix_initialised == False:
                    self._cmatrix_initialised = True
                    self.initialise_cmatrix(self._tile_size_bytes, n)
                
                # Extract and save the tiles
                tiles = [payload[i : i + self._tile_size_bytes] for i in range(0, payload_len, self._tile_size_bytes)]
                self.save_tiles_in_cmatrix(tiles, w, fcn)


            elif type_msg == MsgType.SCHC_AckReq:
                self.stats_ackreq_recvd += 1
                # Check if there are enough symbols
                if self.check_enough_tiles(self._k):
                    # Extract
                    res = self.get_schc_packet(self._all_1_payload, self._recv_rcs)

                    if res == True: # RCS is correct
                        #print("Sending a SCHC ACK with W=3 and C=1")
                        print(f"    {Color.RED}TX ACK :{Color.ENDC}")
                        self.send_ack(3,1)
                        return False
                    else:
                        print(f"RCS Error")


                else:
                    self.get_bitmap()
                    #print("Sending a SCHC ACK with W=1 and C=0")
                    print(f"    {Color.RED}TX ACK :{Color.ENDC}")
                    self.send_c0_ack()
                    self._current_state = States.RX_WAIT_X_MISSING_FRAG

        elif self._current_state == States.RX_END:
            return False

        return True

    def get_msg_type(self, data: bytes) -> int:
        """Get the message type based in the headers of the message"""
        schc_header = data[0]
        msg_len         = len(data)
        w           = schc_header >> self._fcn_field_size
        fcn_mask    = (1 << self._fcn_field_size) - 1
        fcn         = schc_header & fcn_mask
        #print(f"   w={w}, fcn={fcn}")

        if msg_len==1 and fcn==fcn_mask:
            # SCHC Sender-abort
            #print(f"    {Color.PURPLE}RX SENDER-ABORT:{Color.ENDC}", data)
            return MsgType.SCHC_SenderAbort
        elif msg_len==1 and fcn!=fcn_mask:
            # SCHC ACK REQ
            return MsgType.SCHC_AckReq
        elif msg_len>1 and fcn==fcn_mask:
            # SCHC All-1
            return MsgType.SCHC_All1
        elif msg_len>1 and fcn!=fcn_mask:
            # SCHC regular fragment
            return MsgType.SCHC_Fragment
        else:
            print(f"    {Color.PURPLE}RX NOT VALID:{Color.ENDC}", data)
            return MsgType.SCHC_NotValid

    def decode_msg(self, data: bytes, type_msg: int) ->tuple[int, int, int| None, int | None, bytes | None, bytes | None]:
        if type_msg==MsgType.SCHC_Fragment:
            print(f"    {Color.PURPLE}RX FRAG:{Color.ENDC}", data)
            schc_header = data[0]
            w           = schc_header >> self._fcn_field_size
            fcn_mask    = (1 << self._fcn_field_size) - 1
            fcn         = schc_header & fcn_mask
            k           = data[1]
            payload     = data[2:]
            msg_len     = len(payload)
            return w, fcn, k, msg_len, payload, None

        elif type_msg==MsgType.SCHC_All1:
            print(f"    {Color.PURPLE}RX ALL1:{Color.ENDC}", data)
            schc_header = data[0]
            w           = schc_header >> self._fcn_field_size
            fcn_mask    = (1 << self._fcn_field_size) - 1
            fcn         = schc_header & fcn_mask
            rcs         = data[1:5]
            payload     = data[5:]
            msg_len     = len(payload)
            return w, fcn, None, msg_len, payload, rcs       

        elif type_msg==MsgType.SCHC_AckReq:
            print(f"    {Color.PURPLE}RX ACK REQ:{Color.ENDC}", data)
            schc_header = data[0]
            w           = schc_header >> self._fcn_field_size
            fcn_mask    = (1 << self._fcn_field_size) - 1
            fcn         = schc_header & fcn_mask
            return w, fcn, None, None, None, None   

    def initialise_cmatrix(self, s: int, n: int):
        self._cmatrix           = [[0] * n for _ in range(s)]
        self._cmatrix_validator = [0] * n

    def save_tiles_in_cmatrix(self, tiles: list[bytes], w: int, fcn: int):
        start_col   = self._window_size * (w + 1) - fcn - 1  # Columna donde quieres comenzar a guardar
        num_rows    = len(tiles[0])  # Número de filas (longitud de cada tile)
        num_tiles   = len(tiles)     # Cantidad de tiles

        self._cmatrix_validator[start_col : start_col + num_tiles] = [1] * num_tiles
        #print(self._cmatrix_validator)

        # Recorrer los tiles y mapearlos a partir de la columna inicial
        for col_idx, tile in enumerate(tiles):
            # Calculamos la columna real de destino sumando el offset
            target_col = start_col + col_idx  
            
            for row_idx in range(num_rows):
                # Usamos slicing [row_idx : row_idx + 1] para guardar el byte como tipo 'bytes'
                self._cmatrix[row_idx][target_col] = tile[row_idx : row_idx + 1]

        # print("   C-matrix")
        # for row in self._cmatrix:
        #     # Convierte cada elemento 'bytes' a su representación hexadecimal (ej. b'\x8f' -> '8f')
        #     # y los une con un espacio entre ellos.
        #     row_str = "   " +" ".join(cell.hex() if isinstance(cell, bytes) else str(cell) for cell in row)
        #     print(row_str)

    def check_enough_tiles(self, k) -> bool:
        if self._cmatrix_validator.count(1) >= k:
            self.stats_decodeable = True
            if not self.retx_called:
                self.stats_decodeable_before_retx = True
            return True
        else:
            return False

    def get_schc_packet(self, all1_payload: bytes, rcs: bytes) -> bool:
        codec = reedsolo.RSCodec(self._r)
        self._dmatrix = []

        # print("   C-matrix")
        # for row in self._cmatrix:
        #     # Convierte cada elemento 'bytes' a su representación hexadecimal (ej. b'\x8f' -> '8f')
        #     # y los une con un espacio entre ellos.
        #     row_str = "   " +" ".join(cell.hex() if isinstance(cell, bytes) else str(cell) for cell in row)
        #     print(row_str)

        # Calculamos las posiciones borradas una sola vez (aplican para todas las filas)
        erase_pos = []
        for idx in range(len(self._cmatrix_validator)):
            if self._cmatrix_validator[idx] == 0:
                erase_pos.append(idx)

        # Recorremos cada fila de la matriz codificada de forma independiente
        for row in self._cmatrix:
            bytes_list = []

            # Construimos la lista de bytes para esta fila usando el validador global
            for idx in range(len(row)):
                is_received = self._cmatrix_validator[idx]

                if is_received == 0:
                    # Si el validador global dice 0 en esta columna, ponemos relleno temporal
                    bytes_list.append(0)
                else:
                    # Si dice 1, extraemos el valor real de la celda de forma segura
                    cell = row[idx]
                    if isinstance(cell, bytes):
                        val = cell[0] if len(cell) > 0 else 0
                    else:
                        val = cell
                    bytes_list.append(val)

            # Convertimos la lista de enteros a una tira de bytes continua
            row_bytes = bytes(bytes_list)

            # Decodificamos la fila pasándole las posiciones de borrado globales
            decoded_row, _, _ = codec.decode(row_bytes, erase_pos=erase_pos)

            # Convertimos cada byte resultante de vuelta a un objeto bytes de longitud 1
            row_list = [bytes([b]) for b in decoded_row]
            self._dmatrix.append(row_list)

        # print("")
        # print("   D-matrix")
        # for row in self._dmatrix:
        #     print("   " + " ".join(b.hex() for b in row))

        dmatrix_row_format = []
        for row in self._dmatrix:
            for cell in row:
                dmatrix_row_format.append(cell)

        for cell in all1_payload:
            dmatrix_row_format.append(bytes([cell]))

        print("   SCHC packet received: " + " ".join(b.hex() for b in dmatrix_row_format))

        cal_rcs = self.calculate_crc32(dmatrix_row_format)

        print("   RCS RCV: ", rcs.hex(' ').upper())
        print("   RCS CAL: ", cal_rcs.hex(' ').upper()) 

        if cal_rcs == rcs:
            self.stats_rcs_ok       = True
            return True
        else:
            return False

    def calculate_crc32(self, data: bytes) -> bytes:
        """Return CRC-32 (poly 0xEDB88320) for given data."""
        data = b"".join(data)
        val = zlib.crc32(data) & 0xFFFFFFFF
        return val.to_bytes(4, "big")

    def send_ack(self, w: int, c: int):
        """Helper to send C=1 Ack."""
        if c==1:
            # W=3: end-of-session ACK (same as C=1 in server.py). W=1: matrix-only early ACK
            if w==3:
                self.stats_c1_sent += 1
            else:
                self.stats_early_ack_sent += 1
            byte_val = (w << 3) | (c << 2) | 0b11
            msg = bytes([byte_val])


        self._socket.sendto(msg, (IP_ADDR, DEV_PORT))
        time.sleep(0.01) # Small delay after Tx

    def send_c0_ack(self):
        """Helper to send C=0 Ack."""

        compound_ack = BitArray()   # BitArray que almacena el SCHC Compound ACK
        first_win = True            # flag para la primera ventana con tiles perdidos

        for pos, bitmap in enumerate(self.bitmap_array):
            if bitmap.any(False):   # Si el bitmap tiene al menos un cero (tile perdido)
                
                # El campo 'win' siempre se añade, tanto en la primera como en las siguientes
                win = BitArray(uint=pos, length=self._w_field_size)
                compound_ack.append(win)

                if first_win:
                    # Si es la primera vez, añadimos el parámetro c = 0 (1 bit en 0)
                    first_win = False
                    compound_ack.append(BitArray(bin='0'))

                # Añadimos el bitmap en ambos casos
                compound_ack.append(bitmap)                

        # Rellenar con unos ('1') para que el largo sea múltiplo de 8
        bits_faltantes = (8 - (len(compound_ack) % 8)) % 8
        if bits_faltantes > 0:
            compound_ack.append(BitArray(bin='1' * bits_faltantes))

        # Convertir a bytes y enviar por UDP
        compound_ack_bytes = compound_ack.bytes
        self._socket.sendto(compound_ack_bytes, (IP_ADDR, DEV_PORT))
        self.stats_c0_sent += 1
        self.retx_called = True
        time.sleep(0.01)  # Small delay after Tx

    def get_stats(self):
        return (
            self.stats_regular_recvd,
            self.all1_received,
            self.stats_c1_sent,
            self.stats_c0_sent,
            self.stats_ackreq_recvd,
            self.retx_called,
            self.stats_decodeable_before_retx,
            self.stats_decodeable,
            self.stats_rcs_ok,
            self.stats_early_ack_sent,
        )

    def get_bitmap(self):

        # Start from "all received", then mark the tiles still missing
        unos = '1' * self._window_size
        self.bitmap_array = [BitArray(bin=unos) for _ in range(2**self._w_field_size)]

        for posicion, ind in enumerate(self._cmatrix_validator):
            if ind == 0:
                w   = math.floor((posicion)/self._window_size)
                fcn = (math.floor((posicion)/self._window_size) + 1) * self._window_size - (posicion + 1)
                self.bitmap_array[w][self._window_size - 1 - fcn] = 0

def main(ready_event=None):
    """Main function for the ARQ-FEC application server (receiver)."""

    print(f"{Color.PURPLE}ARQ-FEC App Server (Receiver) - Matrix Encoding Geometry{Color.ENDC}")
    print(Color.PURPLE, "=" * 52, Color.ENDC)



    # Create UDP socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((IP_ADDR, APP_PORT))
    # Inactivity timeout: must outlast all device retries (each waits RX_TIMEOUT - 1)
    sock.settimeout(RX_TIMEOUT * MAX_ATTEMPTS)

    # Initialize Reassembler and Decoder
    reassembler = MatrixReassembler(
        socket          = sock,
        window_size     = WINDOW_SIZE,
        tile_size       = TILE_SIZE,
        w_field_size    = W_FIELD_SIZE,
        mtu             = MTU_SIZE_BYTES,
        overhead        = OVERHEAD
    )   


    if ready_event is not None:
        ready_event.set()

    print(f"{Color.PURPLE}Listening on {IP_ADDR}:{APP_PORT}...{Color.ENDC}")

    timed_out = False # Session ended by inactivity timeout (ARQ-FEC session failed)

    try:

        while True:
            try:
                # -- Reception -------------------------------------------------

                packet, dev = sock.recvfrom(RX_BUFFER_SIZE)
                res = reassembler.process_msg(packet)
                if not res:
                    print(f"   {Color.BOLD}{Color.RED}End Reassembler{Color.ENDC} - no more packets expected")
                    break

                # -- End of main loop ------------------------------------------

            except socket.timeout:
                print(f"   {Color.BOLD}{Color.RED}Timeout reached{Color.ENDC} - no more packets expected")
                timed_out = True
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

    stats_regular_recvd, all1_received, stats_c1_sent, stats_c0_sent, stats_ackreq_recvd, retx_called, stats_decodeable_before_retx, stats_decodeable, stats_rcs_ok, stats_early_ack_sent = reassembler.get_stats()

    # Return statistics
    return dict(
        regular_recvd=stats_regular_recvd,
        all1_recvd=all1_received,
        c1_sent=stats_c1_sent,
        c0_sent=stats_c0_sent,
        ackreq_recvd=stats_ackreq_recvd,
        retx_called=retx_called,
        decodeable_before_retx=stats_decodeable_before_retx,
        decodeable=stats_decodeable,
        rcs_ok=stats_rcs_ok,
        timed_out=timed_out,
        early_ack_sent=stats_early_ack_sent,
    )

if __name__ == "__main__":
    main()
