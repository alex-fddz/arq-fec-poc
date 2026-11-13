# SCHC Packet
#SCHC_PACKET = bytes.fromhex('8ff85f20010db8000a0000000000000000000390a04914517cdc244704f3ed00c4cccccccccccc')
SCHC_PACKET = bytes.fromhex('8ff85f20010db8000a0000000000000000000390a04914517cdc244704f3ed00c4cccccccccccccc8ff85f20010db8000a0000000000000000000390a04914517cdc244704f3ed00c4cccccccccccccced00c4ccccccccccccccddff')

# Matrix Encoding Geometry Parameters
SYMBOL_SIZE_BITS = 8          # m = 8 bits (1 byte per symbol)
OVERHEAD = 0.4                 

WINDOW_SIZE = 7               # tiles per window (determines FCN field size)
W_FIELD_SIZE = 5              # "M" bits; make M+FCN byte-aligned
TILE_SIZE = 10                # bytes per tile

MTU_SIZE_BYTES = 22           # Maximum Transmission Unit size in bytes
LOSS_PROBABILITY = 0.1        # 10% packet loss probability
ALLOW_ALL1_LOSS = False        # Allow (or prevent) losing the All-1 fragment
MAX_DELAY_MS = 50             # Maximum delay in milliseconds for simulation

IP_ADDR = "127.0.0.1"         # Loopback IP
DEV_PORT = 5004               # UDP port for Device
APP_PORT = 5005               # UDP port for App Server
RX_TIMEOUT = 5                # Device retry timer = RX_TIMEOUT - 1; server inactivity = RX_TIMEOUT * MAX_ATTEMPTS
RX_BUFFER_SIZE = MTU_SIZE_BYTES # Server reception buffer size
MAX_ATTEMPTS = 2              # Max All-1 + ACK-REQ sent by the device before giving up (Sender-Abort)

SCENARIO_ITERATIONS = 10      # Times to run the scenario automatically
