"""No-network tests for privacy-preserving MAX binary framing/MessagePack probe."""
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ws_binary_probe import scan_binary_frame


def pack(value):
    """Minimal *test-only* MessagePack encoder."""
    if value is None:
        return b"\xc0"
    if isinstance(value, int):
        return (bytes([value]) if 0 <= value < 128 else b"\xcf" + struct.pack(">Q", value))
    if isinstance(value, str):
        buf = value.encode("utf-8")
        return (bytes([0xa0 | len(buf)]) + buf if len(buf) < 32
                else b"\xd9" + bytes([len(buf)]) + buf)
    if isinstance(value, list):
        return bytes([0x90 | len(value)]) + b"".join(pack(item) for item in value)
    if isinstance(value, dict):
        return (bytes([0x80 | len(value)])
                + b"".join(pack(key) + pack(item) for key, item in value.items()))
    raise TypeError(type(value))


def frame(data, *, flags=0, opcode=128):
    return struct.pack(">BBHHI", 10, 0, 1, opcode,
                       (flags << 24) | len(data)) + data


def literal_lz4(data):
    n = len(data)
    out = bytearray([min(n, 15) << 4])
    if n >= 15:
        more = n - 15
        while more >= 255:
            out.append(255)
            more -= 255
        out.append(more)
    return bytes(out) + data


class BinaryProbeTests(unittest.TestCase):
    def test_chat_and_message_id_without_message_or_token_values(self):
        plain = pack({
            "chatId": 123456789012345,
            "message": {"id": 989989989989, "text": "SECRET_CLIENT_MESSAGE",
                        "sender": 555, "token": "SECRET_TOKEN",
                        "attaches": [{"raw": "SECRET_BINARY"}]},
            "accessToken": "SECRET_AUTHENTICATION",
        })
        status, structure = scan_binary_frame(frame(plain))
        self.assertEqual(status, "msgpack")
        self.assertEqual(structure, {"chatId": 1, "message": {"id": 1}})
        self.assertNotIn("SECRET", repr(structure))
        self.assertNotIn("1234567", repr(structure))

    def test_history_with_leading_ints_and_many_messages(self):
        plain = b"\xff\x00" + pack({
            "chatId": 23, "messages": [
                {"id": 98989, "text": "SECRET_ONE"},
                {"id": 98990, "text": "SECRET_TWO"}
            ],
            "chats": [{"id": 77, "title": "SECRET_DIALOG",
                       "lastMessage": {"id": 988, "text": "SECRET_HISTORY"}}]
        })
        status, structure = scan_binary_frame(frame(plain, opcode=49))
        self.assertEqual(status, "msgpack")
        self.assertEqual(structure["chatId"], 1)
        self.assertEqual(structure["messages"], [{"id": 1}, {"id": 1}])
        self.assertEqual(structure["chats"], [{"id": 1, "lastMessage": {"id": 1}}])
        self.assertNotIn("SECRET", repr(structure))

    def test_lz4_literal_compression(self):
        compressed = literal_lz4(pack({
            "chatId": 12, "message": {"id": 999, "text": "SECRET_CONTENT"}}))
        status, structure = scan_binary_frame(frame(compressed, flags=1))
        self.assertEqual(status, "lz4_msgpack")
        self.assertEqual(structure, {"chatId": 1, "message": {"id": 1}})

    def test_msgpack_ext_id(self):
        # message.id is a fixext 1 value (extension code 1); actual bytes never read as ID.
        message = b"\x82\xa2id\xd4\x01\xee\xa4text\xa6SECRET"
        body = b"\x82\xa6chatId\x2a\xa7message" + message
        status, structure = scan_binary_frame(frame(body))
        self.assertEqual(status, "msgpack")
        self.assertEqual(structure, {"chatId": 1, "message": {"id": 1}})

    def test_zstd_and_invalid_length_fail_closed(self):
        status, payload = scan_binary_frame(frame(b"COMPRESSED_SECRET", flags=255))
        self.assertEqual((status, payload), ("zstd", None))
        status, payload = scan_binary_frame(frame(b"\x80")[:-1])
        self.assertEqual((status, payload), ("missing_header", None))
        status, payload = scan_binary_frame(b"X")
        self.assertEqual((status, payload), ("missing_header", None))

    def test_unrelated_message_text_is_not_scanned_as_id(self):
        status, payload = scan_binary_frame(frame(pack({
            "metadata": {"id": 123, "chatId": 345, "message": {"id": 67}},
            "text": "SECRET_TEXT",
            "messages": [{"id": 8}],
        })))
        self.assertEqual(status, "msgpack")
        self.assertEqual(payload, {"messages": [{"id": 1}]})


if __name__ == "__main__":
    unittest.main()
