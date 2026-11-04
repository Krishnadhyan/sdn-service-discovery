import socket, sys

msg = sys.argv[1]
host = sys.argv[2] if len(sys.argv) > 2 else "127.0.0.1"
port = int(sys.argv[3]) if len(sys.argv) > 3 else 5000

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.settimeout(2)
s.sendto(msg.encode(), (host, port))
try:
    print(s.recvfrom(2048)[0].decode())
except socket.timeout:
    print("(no reply)")
