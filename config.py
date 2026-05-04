# SCHC Packet
SCHC_PACKET = bytes.fromhex('8ff85f20010db8000a0000000000000000000390a04914517cdc244704f3ed00c4cccccccccccc')

# Stream Encoding Geometry Parameters
SYMBOL_SIZE_BITS = 8          # m = 8 bits (1 byte per symbol)
SOURCE_BLOCK_SIZE = 2         # k = 2 source symbols per block
ENCODED_BLOCK_SIZE = 3        # n = 3 encoded symbols per block (k source + 1 parity)

WINDOW_SIZE = 7               # tiles per window
TILE_SIZE = SYMBOL_SIZE_BITS  # bits per tile
INTERLEAVING_DEPTH = 3        # depth for interleaving

MTU_SIZE_BYTES = 10           # Maximum Transmission Unit size in bytes
LOSS_PROBABILITY = 0.1        # 10% packet loss probability
MAX_DELAY_MS = 50             # Maximum delay in milliseconds for simulation

IP_ADDR = "127.0.0.1"         # Loopback IP
DEV_PORT = 5004               # UDP port for Device
APP_PORT = 5005               # UDP port for App Server
RX_TIMEOUT = 5                # Server timeout
RX_BUFFER_SIZE = 5            # Server reception buffer size
