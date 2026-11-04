import ipaddress
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import topology


class TopologyConfigTests(unittest.TestCase):
    def test_exact_host_inventory_and_unique_addresses(self):
        expected_hosts = {f"h{i}" for i in range(1, 14)}
        host_names = [name for name, _ in topology.HOST_SPECS]
        self.assertEqual(set(host_names), expected_hosts)
        self.assertEqual(len(host_names), 13)
        self.assertEqual(len(host_names), len(set(host_names)))
        subnet = ipaddress.ip_network("10.0.0.0/24")
        addresses = [ipaddress.ip_interface(address).ip for _, address in topology.HOST_SPECS]
        self.assertEqual(len(addresses), len(set(addresses)))
        self.assertTrue(all(address in subnet for address in addresses))
        self.assertEqual(topology.ROOT_IP, "10.0.0.254/24")
        self.assertNotIn(ipaddress.ip_interface(topology.ROOT_IP).ip, addresses)

    def test_five_clients_keep_the_requested_addresses(self):
        host_ips = dict(topology.HOST_SPECS)
        expected = {"h1": "10.0.0.1/24", "h2": "10.0.0.2/24", "h11": "10.0.0.11/24",
                    "h12": "10.0.0.12/24", "h13": "10.0.0.13/24"}
        self.assertEqual(topology.CLIENT_HOSTS, ("h1", "h2", "h11", "h12", "h13"))
        self.assertEqual({host: host_ips[host] for host in topology.CLIENT_HOSTS}, expected)

    def test_s1_has_all_clients_and_registry_and_s2_has_all_providers(self):
        self.assertEqual(topology.S1_HOSTS, ("h1", "h2", "h3", "h11", "h12", "h13"))
        self.assertEqual(topology.REGISTRY_HOST, "h3")
        self.assertEqual(dict(topology.HOST_SPECS)[topology.REGISTRY_HOST], "10.0.0.3/24")
        registry_config = (Path(__file__).resolve().parents[2] / "registry" / "config.py").read_text(encoding="utf-8")
        self.assertIn('REGISTRY_PORT = int(os.getenv("REGISTRY_PORT", "5000"))', registry_config)
        self.assertEqual(topology.S2_HOSTS, ("h4", "h5", "h6", "h7", "h8", "h9", "h10"))
        self.assertEqual(topology.PROVIDER_HOSTS, topology.S2_HOSTS)
        self.assertEqual(len(topology.CLIENT_HOSTS) + 1 + len(topology.PROVIDER_HOSTS), 13)

    def test_provider_address_and_service_endpoint_inventory_is_unchanged(self):
        host_ips = dict(topology.HOST_SPECS)
        expected = {
            "h4": ("10.0.0.4/24", "FILE-1", "FILE", "TCP", 5001),
            "h5": ("10.0.0.5/24", "FILE-2", "FILE", "TCP", 5001),
            "h8": ("10.0.0.8/24", "FILE-3", "FILE", "TCP", 5001),
            "h6": ("10.0.0.6/24", "CHAT-1", "CHAT", "TCP", 5002),
            "h9": ("10.0.0.9/24", "CHAT-2", "CHAT", "TCP", 5002),
            "h7": ("10.0.0.7/24", "VIDEO-1", "VIDEO", "UDP", 5003),
            "h10": ("10.0.0.10/24", "VIDEO-2", "VIDEO", "UDP", 5003),
        }
        self.assertEqual(set(expected), set(topology.PROVIDER_HOSTS))
        self.assertEqual({host: host_ips[host] for host in expected},
                         {host: spec[0] for host, spec in expected.items()})
        # The existing launch table is the source for provider/service/protocol/port arguments.
        readme = (Path(__file__).resolve().parents[1] / "INTEGRATION_README.md").read_text(encoding="utf-8")
        for host, (_ip, provider, service, protocol, port) in expected.items():
            self.assertIn(f"| {host} |", readme)
            self.assertIn(f"{provider} | {service} | {protocol}/{port}", readme)

    def test_switch_link_controller_and_root_api_configuration(self):
        self.assertEqual(topology.S1_DPID, "0000000000000001")
        self.assertEqual(topology.S2_DPID, "0000000000000002")
        self.assertEqual(topology.OPENFLOW_PROTOCOL, "OpenFlow13")
        self.assertEqual((topology.CONTROLLER_IP, topology.CONTROLLER_PORT), ("127.0.0.1", 6653))
        self.assertEqual((topology.ROOT_IF, topology.SW_IF, topology.ROOT_IP),
                         ("sdn-root", "sdn-s1", "10.0.0.254/24"))
        source = (Path(__file__).resolve().parents[1] / "topology.py").read_text(encoding="utf-8")
        self.assertIn('net.addController("c0", controller=RemoteController, ip=CONTROLLER_IP, port=CONTROLLER_PORT)', source)
        self.assertIn('net.addSwitch("s1", dpid=S1_DPID, protocols=OPENFLOW_PROTOCOL)', source)
        self.assertIn('net.addSwitch("s2", dpid=S2_DPID, protocols=OPENFLOW_PROTOCOL)', source)
        self.assertIn("net.addLink(s1, s2)", source)
        self.assertIn('add_root_api_link("s1")', source)
        self.assertIn("create_lifecycle_token()", source)
        self.assertIn("LIFECYCLE_TOKEN_FILE.unlink(missing_ok=True)", source)


if __name__ == "__main__":
    unittest.main()
