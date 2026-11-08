"""Read-only MAX binary-frame shape probe (stdlib only, never decode message text).

The probe returns whitelisted structural field *presence*, not ID values. It
does not persist bytes, URLs, decoded chat content or authentication material.
It is diagnostic only; not a general-purpose MAX message decoder.
"""
import hashlib
import hmac
import struct

MAX_FRAME = 2 * 1024 * 1024
MAX_DECOMPRESSED = 4 * 1024 * 1024
MAX_ITEMS = 40000
MAX_DEPTH = 24
MAX_ARRAY_OUTPUT = 1000

# Only these structural keys survive the parser. All other values are skipped.
KEEP = frozenset({"chatId", "chat", "chats", "message", "messages", "lastMessage", "id"})
HDR = struct.Struct(">BBHHI")


class ProbeDecodeError(ValueError):
    pass


def _lz4_block(data: bytes) -> bytes:
    """Bounded LZ4 block decoder, matching MAX TCP framing flags."""
    output = bytearray()
    pos = 0
    while pos < len(data):
        token = data[pos]
        pos += 1
        lit = token >> 4
        if lit == 15:
            while True:
                if pos >= len(data):
                    raise ProbeDecodeError("truncated LZ4 literals")
                b = data[pos]
                pos += 1
                lit += b
                if b < 255:
                    break
        if pos + lit > len(data) or len(output) + lit > MAX_DECOMPRESSED:
            raise ProbeDecodeError("LZ4 literals beyond bound")
        output.extend(data[pos:pos + lit])
        pos += lit
        if pos == len(data):
            break
        if pos + 2 > len(data):
            raise ProbeDecodeError("missing LZ4 offset")
        offset = data[pos] | (data[pos + 1] << 8)
        pos += 2
        if not 0 < offset <= len(output):
            raise ProbeDecodeError("invalid LZ4 offset")
        n = (token & 15) + 4
        if (token & 15) == 15:
            while True:
                if pos >= len(data):
                    raise ProbeDecodeError("truncated LZ4 repeat")
                b = data[pos]
                pos += 1
                n += b
                if b < 255:
                    break
        if len(output) + n > MAX_DECOMPRESSED:
            raise ProbeDecodeError("LZ4 output beyond bound")
        for _ in range(n):
            output.append(output[-offset])
    return bytes(output)


class _MsgpackStructuralProbe:
    """Traverse MessagePack but only materialize approved *field names*.

    ID and chatId scalar values are reduced to an arbitrary constant 1,
    without decoding or storing the actual value.
    """

    def __init__(self, data: bytes, fingerprint_key=None):
        self.buf = memoryview(data)
        self.pos = 0
        self.steps = 0
        self.fingerprint_key = fingerprint_key

    def _take(self, n: int) -> memoryview:
        if n < 0 or n > len(self.buf) - self.pos:
            raise ProbeDecodeError("truncated msgpack")
        begin = self.pos
        self.pos += n
        return self.buf[begin:self.pos]

    def _number(self, n: int) -> int:
        return int.from_bytes(self._take(n), "big")

    def _str(self, n: int, key: bool, capture: bool):
        if key and capture and n <= 48:
            raw = self._take(n)
            # Don't construct arbitrary strings, even as temporary output.
            if any(x > 127 for x in raw):
                return None
            candidate = bytes(raw).decode("ascii")
            return candidate if candidate in KEEP else None
        self._take(n)
        return 1 if capture and n else None

    @staticmethod
    def _canonical_id(raw: bytes):
        """Only canonicalize an ID scalar; no free-form messages are decoded."""
        if not raw or len(raw) > 256:
            return None
        try:
            label = raw.decode("ascii")
            if label and ((label[0] in "+-" and label[1:].isdigit()) or label.isdigit()):
                return b"n:" + str(int(label)).encode("ascii")
        except (UnicodeDecodeError, ValueError):
            pass
        return b"s:" + raw

    def _id_token(self, token: int):
        """Read one targeted MessagePack ID, normalize numeric/string/ext1."""
        if token <= 0x7f:
            return b"n:" + str(token).encode("ascii")
        if token >= 0xe0:
            return b"n:" + str(token - 256).encode("ascii")
        nums = {
            0xcc: (1, False), 0xcd: (2, False), 0xce: (4, False),
            0xcf: (8, False), 0xd0: (1, True), 0xd1: (2, True),
            0xd2: (4, True), 0xd3: (8, True),
        }
        if token in nums:
            size, signed = nums[token]
            n = int.from_bytes(self._take(size), "big", signed=signed)
            return b"n:" + str(n).encode("ascii")
        if 0xa0 <= token <= 0xbf:
            return self._canonical_id(bytes(self._take(token & 31)))
        if token in (0xd9, 0xda, 0xdb):
            n = self._number({0xd9: 1, 0xda: 2, 0xdb: 4}[token])
            return self._canonical_id(bytes(self._take(n)))
        ext_size = {0xd4: 1, 0xd5: 2, 0xd6: 4, 0xd7: 8, 0xd8: 16}
        if token in ext_size:
            code = self._number(1)
            payload = bytes(self._take(ext_size[token]))
        elif token in (0xc7, 0xc8, 0xc9):
            n = self._number({0xc7: 1, 0xc8: 2, 0xc9: 4}[token])
            code = self._number(1)
            payload = bytes(self._take(n))
        else:
            # Re-run normal safe reader to skip arbitrary unexpected ID fields.
            self.pos -= 1
            self.item(depth=1, capture=False)
            return None
        if code != 1 or len(payload) > 32:
            return None
        inner = _MsgpackStructuralProbe(payload)
        try:
            inner_token = inner._number(1)
            canonical = inner._id_token(inner_token)
            return canonical if inner.pos == len(inner.buf) else None
        except ProbeDecodeError:
            return None

    def _opaque_id(self, token: int):
        canonical = self._id_token(token)
        if canonical is None:
            return None
        return hmac.new(
            self.fingerprint_key,
            b"max-id\0" + canonical,
            hashlib.sha256,
        ).hexdigest()

    def _array(self, n: int, depth: int, capture: bool):
        if n > MAX_ITEMS:
            raise ProbeDecodeError("unbounded msgpack array")
        out = [] if capture else None
        for i in range(n):
            obj = self.item(depth + 1, capture and i < MAX_ARRAY_OUTPUT)
            if capture and i < MAX_ARRAY_OUTPUT:
                out.append(obj)
        return out

    def _map(self, n: int, depth: int, capture: bool):
        if n > MAX_ITEMS:
            raise ProbeDecodeError("unbounded msgpack map")
        out = {} if capture else None
        for _ in range(n):
            key = self.item(depth + 1, capture, key=True)
            wanted = capture and isinstance(key, str) and key in KEEP
            value = self.item(depth + 1, wanted,
                              id_field=(key if wanted and key in ("id", "chatId")
                                        and self.fingerprint_key is not None else None))
            if wanted:
                out[key] = value
        return out

    def item(self, depth: int = 0, capture: bool = True, key: bool = False,
             id_field=None):
        if depth > MAX_DEPTH or self.steps > MAX_ITEMS:
            raise ProbeDecodeError("msgpack nesting exceeds budget")
        self.steps += 1
        token = self._number(1)
        if id_field is not None:
            return self._opaque_id(token)
        if token <= 0x7f or token >= 0xe0:
            return None if key else (1 if capture else None)
        if 0x80 <= token <= 0x8f:
            return self._map(token & 15, depth, capture)
        if 0x90 <= token <= 0x9f:
            return self._array(token & 15, depth, capture)
        if 0xa0 <= token <= 0xbf:
            return self._str(token & 31, key, capture)

        if token == 0xc0:
            return None
        if token in (0xc2, 0xc3):
            return None if key else (1 if capture else None)
        if token in (0xc4, 0xc5, 0xc6):
            n = self._number({0xc4: 1, 0xc5: 2, 0xc6: 4}[token])
            self._take(n)
            return 1 if capture and n else None
        if token in (0xc7, 0xc8, 0xc9):
            n = self._number({0xc7: 1, 0xc8: 2, 0xc9: 4}[token])
            self._take(1 + n)  # ext code + payload; never extract ID values
            return 1 if capture else None
        if token in (0xca, 0xcb):
            self._take(4 if token == 0xca else 8)
            return 1 if capture else None
        if token in (0xcc, 0xcd, 0xce, 0xcf, 0xd0, 0xd1, 0xd2, 0xd3):
            self._take({0xcc: 1, 0xcd: 2, 0xce: 4, 0xcf: 8,
                        0xd0: 1, 0xd1: 2, 0xd2: 4, 0xd3: 8}[token])
            return 1 if capture else None
        if token in (0xd4, 0xd5, 0xd6, 0xd7, 0xd8):
            self._take(1 + {0xd4: 1, 0xd5: 2, 0xd6: 4, 0xd7: 8, 0xd8: 16}[token])
            return 1 if capture else None
        if token in (0xd9, 0xda, 0xdb):
            return self._str(self._number({0xd9: 1, 0xda: 2, 0xdb: 4}[token]), key, capture)
        if token in (0xdc, 0xdd):
            return self._array(self._number(2 if token == 0xdc else 4), depth, capture)
        if token in (0xde, 0xdf):
            return self._map(self._number(2 if token == 0xde else 4), depth, capture)
        raise ProbeDecodeError("unknown msgpack code")

    def structural_map(self):
        # Some server responses start with two msgpack integers before the map.
        for _ in range(4):
            if self.pos >= len(self.buf):
                return None
            result = self.item()
            if isinstance(result, dict):
                return result
        return None



def frame_meta(raw: bytes):
    """Return only (command, request sequence, opcode), never message identifiers.

    These are packet-routing fields, not MAX chat/message IDs.
    """
    if not isinstance(raw, bytes) or len(raw) < HDR.size or len(raw) > MAX_FRAME:
        return None
    ver, cmd, seq, opcode, packed = HDR.unpack_from(raw)
    payload_size = packed & 0x00ffffff
    if (ver not in (10, 11) or cmd not in (0, 1, 2, 3)
            or opcode > 6000 or payload_size != len(raw) - HDR.size):
        return None
    return cmd, seq, opcode

def scan_binary_frame(raw: bytes, fingerprint_key=None):
    """Return (status, whitelisted_payload) without returning identifiers.

    status: missing_header | malformed | zstd | lz4_error | msgpack_error
            | msgpack | lz4_msgpack
    The caller may increment counters; NEVER log raw/decoded payloads.
    """
    if not isinstance(raw, bytes) or len(raw) > MAX_FRAME:
        return "malformed", None
    if len(raw) < HDR.size:
        return "missing_header", None
    ver, cmd, seq, opcode, packed = HDR.unpack_from(raw)
    flags, n = packed >> 24, packed & 0x00ffffff
    if ver not in (10, 11) or cmd not in (0, 1, 2, 3) or n != len(raw) - HDR.size:
        return "missing_header", None
    if not 0 <= opcode <= 6000:
        return "missing_header", None
    body = raw[HDR.size:]
    if flags == 0xff:
        return "zstd", None  # no unsafe unbounded zstd fallback
    if flags:
        try:
            body = _lz4_block(body)
        except ProbeDecodeError:
            return "lz4_error", None
    if len(body) > MAX_DECOMPRESSED:
        return "malformed", None
    try:
        root = _MsgpackStructuralProbe(body, fingerprint_key).structural_map()
    except (ProbeDecodeError, ValueError, OverflowError):
        return "msgpack_error", None
    if not isinstance(root, dict):
        return "msgpack_error", None
    # Preserve only structural keys and constant markers or HMAC fingerprints.
    return ("lz4_msgpack" if flags else "msgpack"), root
