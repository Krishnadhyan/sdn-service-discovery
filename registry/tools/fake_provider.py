import argparse
import socket
import time

REGISTRY_IP = "10.0.0.3"
REGISTRY_PORT = 5000
HEARTBEAT_INTERVAL = 2


def main():
    parser = argparse.ArgumentParser(description="Service provider simulator")
    parser.add_argument("--id", required=True, help="Provider ID")
    parser.add_argument("--service", default="FILE", help="Service name")
    parser.add_argument("--protocol", default="TCP", help="Service protocol")
    parser.add_argument("--port", type=int, default=5001, help="Service port")
    args = parser.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(3)

    try:
        registration = (
            f"REGISTER|{args.id}|{args.service}|"
            f"{args.protocol}|{args.port}"
        )
        sock.sendto(registration.encode(), (REGISTRY_IP, REGISTRY_PORT))

        response = sock.recvfrom(2048)[0].decode()
        print(f"Registration: {response}", flush=True)

        if not response.startswith("REGISTER_OK|"):
            print("Registration was not accepted.", flush=True)
            return

        while True:
            time.sleep(HEARTBEAT_INTERVAL)
            heartbeat = f"HEARTBEAT|{args.id}"
            sock.sendto(heartbeat.encode(), (REGISTRY_IP, REGISTRY_PORT))
            print(f"{args.id}: heartbeat sent", flush=True)

    except socket.timeout:
        print("Registry did not respond to registration.", flush=True)
    except KeyboardInterrupt:
        print(f"\n{args.id} simulator stopped.", flush=True)
    finally:
        sock.close()


if __name__ == "__main__":
    main()