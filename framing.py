import asyncio

COMPANION_START = 0x3E  # companion → client frames
CLIENT_START = 0x3C     # client → companion frames
MAX_PAYLOAD = 300


async def read_frame(reader: asyncio.StreamReader, start_byte: int) -> bytes:
    """Read one complete MeshCore TCP frame. Returns raw bytes including the 3-byte header."""
    start = start_byte.to_bytes(1, "big")

    # Scan for the start byte, discarding junk (e.g. console debug output)
    while True:
        b = await reader.readexactly(1)
        if b[0] == start_byte:
            break

    length_bytes = await reader.readexactly(2)
    length = int.from_bytes(length_bytes, "little")

    if length > MAX_PAYLOAD:
        # Invalid frame — resync
        return await read_frame(reader, start_byte)

    payload = await reader.readexactly(length) if length else b""
    return start + length_bytes + payload
