import sys, os, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import protocol
from protocol import ProtocolError

class T(unittest.TestCase):
    def test_register(self):
        k, f = protocol.parse(b"REGISTER|FILE-1|FILE|TCP|5001")
        self.assertEqual(k, "REGISTER"); self.assertEqual(f["port"], 5001)
    def test_bad_cases(self):
        for bad in [b"REGISTER|FILE-1|FILE|TCP", b"REGISTER|F|FILE|TCP|abc",
                    b"REGISTER|F|FILE|XYZ|5001", b"REGISTER|F|GAME|TCP|5001",
                    b"REGISTER|F|FILE|TCP|99999", b"HELLO", b"", b"DISCOVER|"]:
            with self.assertRaises(ProtocolError): protocol.parse(bad)
    def test_discover(self):
        self.assertEqual(protocol.parse(b"DISCOVER|FILE")[1]["service"], "FILE")

unittest.main() if __name__ == "__main__" else None
