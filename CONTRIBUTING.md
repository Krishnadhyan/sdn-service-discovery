# Contributing to Service Discovery SDN

Thank you for your interest in contributing! This project implements an SDN-integrated dynamic service discovery framework running over Mininet and OpenFlow with live dashboard observability and failure recovery.

## Development Setup

1. **Clone the repository**:
   ```bash
   git clone https://github.com/<your-username>/service-discovery-sdn.git
   cd service-discovery-sdn
   ```

2. **Create a virtual environment**:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install --upgrade pip
   pip install -r requirements-dev.txt
   ```

## Running Tests

### Standalone Tests (No Mininet Required)
The core logic for discovery protocols, registry store concurrency, providers, client failover, and frontend bridges can run on any host OS:

```bash
# Run all standalone test suites
python3 -m unittest discover -s registry/tests -v
python3 -m unittest discover -s tests -v
python3 -m unittest discover -s benchmark/tests -v
python3 -m unittest discover -s providers/tests -v
```

### Full Integration & Topology Tests (Mininet VM Required)
Mininet, Open vSwitch, and the os-ken OpenFlow controller require a Linux environment with root privileges:

```bash
# Inside the Ubuntu Mininet VM:
sudo python3 -m unittest discover -s integration/tests -v
sudo python3 integration/test_sdn_failure_recovery.py
```

## Code Guidelines

- Maintain Python 3 compatibility (3.10+).
- Follow PEP 8 style recommendations.
- Keep network and wire-protocol formats consistent with `protocol.py`.
- Add unit tests for any bug fixes or new features.

## Submitting Pull Requests

1. Create a descriptive topic branch (`git checkout -b feature/my-new-feature`).
2. Ensure all standalone unit tests pass.
3. Commit your changes with clear and meaningful commit messages.
4. Push your branch and open a Pull Request against `main`.

