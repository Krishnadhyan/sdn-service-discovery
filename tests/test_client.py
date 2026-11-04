import socket
import threading
import unittest
import contextlib
import io
import json
from unittest.mock import patch
from client.client import discover, communicate, run_one_shot, run_service_workload

class ClientProtocolTests(unittest.TestCase):
    def udp_registry(self, response, received=None):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        def serve():
            try:
                payload, addr = sock.recvfrom(2048)
                if received is not None:
                    received.append(payload)
                sock.sendto(response.encode(), addr)
            finally: sock.close()
        threading.Thread(target=serve, daemon=True).start()
        return port

    def fake_registry_socket(self, response, received=None):
        class FakeSocket:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def settimeout(self, value): self.timeout = value
            def sendto(self, payload, target):
                if received is not None: received.append(payload)
            def getsockname(self): return ('10.0.0.1', 34567)
            def recvfrom(self, size): return response.encode(), ('10.0.0.3', 5000)
        return FakeSocket()

    def test_parse_multiple_providers(self):
        port = self.udp_registry('DISCOVER_RESPONSE|FILE|FILE-1,TCP,10.0.0.4,5001|FILE-2,TCP,10.0.0.5,5001')
        providers = discover('127.0.0.1', port, 'FILE', 1)
        self.assertEqual([p['id'] for p in providers], ['FILE-1', 'FILE-2'])
        self.assertEqual(providers[1]['ip'], '10.0.0.5')

    def test_unavailable_is_empty_list(self):
        port = self.udp_registry('ERROR|SERVICE_UNAVAILABLE|CHAT')
        self.assertEqual(discover('127.0.0.1', port, 'CHAT', 1), [])

    def test_direct_tcp_exchange(self):
        server = socket.socket(); server.bind(('127.0.0.1', 0)); server.listen(1)
        port = server.getsockname()[1]
        def serve():
            conn, _ = server.accept()
            with conn:
                conn.recv(1024); conn.sendall(b'FILE-1 OK\n')
            server.close()
        threading.Thread(target=serve, daemon=True).start()
        result = communicate({'id':'FILE-1','protocol':'TCP','ip':'127.0.0.1','port':port}, 'FILE', 'PING', 1)
        self.assertEqual(result, 'FILE-1 OK')

    def test_first_provider_success_stops_before_second(self):
        response = 'DISCOVER_RESPONSE|FILE|FILE-1,TCP,10.0.0.4,5001|FILE-2,TCP,10.0.0.5,5001'
        fake = self.fake_registry_socket(response)
        with patch('client.client.socket.socket', return_value=fake), \
             patch('client.client.communicate', return_value='FILE-1 OK') as service:
            result = run_one_shot('FILE', '10.0.0.3', 5000, 'PING', 1, 'service', 'h1')
        service.assert_called_once_with(result['providers'][0], 'FILE', 'PING', 1)
        self.assertEqual([a['provider_id'] for a in result['provider_attempts']], ['FILE-1'])
        self.assertEqual(result['final_provider']['id'], 'FILE-1')
        self.assertEqual(result['provider_response'], 'FILE-1 OK')
        self.assertEqual(result['operation_status'], 'service_succeeded')

    def test_first_provider_failure_falls_back_in_registry_order(self):
        response = 'DISCOVER_RESPONSE|VIDEO|VIDEO-1,UDP,10.0.0.7,5003|VIDEO-2,UDP,10.0.0.10,5003'
        fake = self.fake_registry_socket(response)
        with patch('client.client.socket.socket', return_value=fake), \
             patch('client.client.communicate', side_effect=[TimeoutError('provider response timeout'),
                                                              'VIDEO-2 OK service=VIDEO request=PING']) as service:
            result = run_one_shot('VIDEO', '10.0.0.3', 5000, 'PING', 1, 'service', 'h1')
        self.assertEqual([call.args[0]['id'] for call in service.call_args_list], ['VIDEO-1', 'VIDEO-2'])
        self.assertEqual([a['provider_id'] for a in result['provider_attempts']], ['VIDEO-1', 'VIDEO-2'])
        self.assertEqual([a['status'] for a in result['provider_attempts']], ['timeout', 'succeeded'])
        self.assertTrue(result['discovery_success'])
        self.assertTrue(result['provider_connection_success'])
        self.assertEqual(result['final_provider']['id'], 'VIDEO-2')
        self.assertEqual(result['provider_response'], 'VIDEO-2 OK service=VIDEO request=PING')
        self.assertEqual(result['operation_status'], 'service_succeeded')

    def test_all_provider_failures_are_reported_without_changing_registry_health(self):
        received = []
        response = 'DISCOVER_RESPONSE|FILE|FILE-1,TCP,10.0.0.4,5001|FILE-2,TCP,10.0.0.5,5001'
        fake = self.fake_registry_socket(response, received)
        with patch('client.client.socket.socket', return_value=fake), \
             patch('client.client.communicate', side_effect=[TimeoutError('provider response timeout'),
                                                              ConnectionRefusedError('connection refused')]) as service:
            result = run_one_shot('FILE', '10.0.0.3', 5000, 'PING', 1, 'service', 'h1')
        self.assertEqual([call.args[0]['id'] for call in service.call_args_list], ['FILE-1', 'FILE-2'])
        self.assertTrue(result['discovery_success'])
        self.assertFalse(result['provider_connection_success'])
        self.assertIsNone(result['final_provider'])
        self.assertEqual(result['operation_status'], 'service_failed')
        self.assertEqual([a['status'] for a in result['provider_attempts']], ['timeout', 'failed'])
        # The client sends only DISCOVER to Registry; provider failures do not
        # report INACTIVE or otherwise mutate Registry health state.
        self.assertEqual(received, [b'DISCOVER|FILE'])

    def test_no_providers_means_no_provider_attempts(self):
        fake = self.fake_registry_socket('ERROR|SERVICE_UNAVAILABLE|FILE')
        with patch('client.client.socket.socket', return_value=fake), \
             patch('client.client.communicate') as service:
            result = run_one_shot('FILE', '10.0.0.3', 5000, 'PING', 1, 'service', 'h1')
        service.assert_not_called()
        self.assertTrue(result['discovery_success'])
        self.assertEqual(result['provider_attempts'], [])
        self.assertIsNone(result['final_provider'])
        self.assertEqual(result['operation_status'], 'no_available_provider')

    def test_one_shot_discovery_only_does_not_contact_provider(self):
        response = b'DISCOVER_RESPONSE|FILE|FILE-1,TCP,10.0.0.4,5001'
        class FakeSocket:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def settimeout(self, value): pass
            def sendto(self, payload, target): self.sent = (payload, target)
            def getsockname(self): return ('10.0.0.1', 34567)
            def recvfrom(self, size): return response, ('10.0.0.3', 5000)
        fake = FakeSocket()
        with patch('client.client.socket.socket', return_value=fake), \
             patch('client.client.communicate', side_effect=AssertionError('provider should not be contacted')):
            result = run_one_shot('FILE', '10.0.0.3', 5000, 'PING', 1, 'discover', 'h1')
        self.assertEqual(fake.sent, (b'DISCOVER|FILE', ('10.0.0.3', 5000)))
        self.assertTrue(result['discovery_success'])
        self.assertIsNone(result['provider_connection_success'])
        self.assertEqual(result['provider_attempts'], [])
        self.assertEqual(result['operation_status'], 'discovery_succeeded')

    def test_tcp_service_use_targets_registry_returned_provider(self):
        class FakeConnection:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def settimeout(self, value): pass
            def sendall(self, payload): self.payload = payload
            def recv(self, size): return b'FILE-1 OK\n'
        connection = FakeConnection()
        with patch('client.client.socket.create_connection', return_value=connection) as connect:
            result = communicate({'id': 'FILE-1', 'protocol': 'TCP', 'ip': '10.0.0.4', 'port': 5001},
                                 'FILE', 'PING', 1)
        connect.assert_called_once_with(('10.0.0.4', 5001), timeout=1)
        self.assertEqual(connection.payload, b'PING\n')
        self.assertEqual(result, 'FILE-1 OK')

    def test_one_shot_registry_timeout_is_not_reported_as_discovery_success(self):
        class TimeoutSocket:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def settimeout(self, value): pass
            def sendto(self, payload, target): pass
            def getsockname(self): return ('10.0.0.1', 34567)
            def recvfrom(self, size): raise TimeoutError('registry timed out')
        with patch('client.client.socket.socket', return_value=TimeoutSocket()):
            result = run_one_shot('FILE', '10.0.0.3', 5000, 'PING', 0.1, 'service', 'h1')
        self.assertFalse(result['discovery_success'])
        self.assertIsNone(result['provider_connection_success'])
        self.assertEqual(result['operation_status'], 'registry_timeout')
        self.assertIn('timed out', result['error'])

    def test_static_file_workload_contacts_fixed_provider_without_registry(self):
        output = io.StringIO()
        endpoint = {'id': 'FILE-1', 'protocol': 'TCP', 'ip': '10.0.0.4', 'port': 5001}
        with patch('client.client.communicate', return_value='FILE-1 OK service=FILE request=PING') as call, \
             patch('client.client.run_one_shot', side_effect=AssertionError('static must not discover')):
            with contextlib.redirect_stdout(output):
                run_service_workload('FILE', '10.0.0.3', 5000, 'PING', 1, 1, 1,
                                     'static', 'h1', endpoint)
        call.assert_called_once_with(endpoint, 'FILE', 'PING', 1)
        row = json.loads(output.getvalue())
        self.assertTrue(row['success'])
        self.assertIsNone(row['discovery_latency_ms'])
        self.assertEqual(row['final_provider']['id'], 'FILE-1')
        self.assertEqual(row['provider_response'], 'FILE-1 OK service=FILE request=PING')

    def test_dynamic_service_workload_preserves_fallback_attempts(self):
        output = io.StringIO()
        dynamic_result = {
            'operation_status': 'service_succeeded', 'provider_attempts': [
                {'provider_id': 'FILE-1', 'status': 'timeout', 'error': 'response timeout'},
                {'provider_id': 'FILE-2', 'status': 'succeeded'}],
            'final_provider': {'id': 'FILE-2', 'protocol': 'TCP', 'ip': '10.0.0.5', 'port': 5001},
            'provider_response': 'FILE-2 OK service=FILE request=PING',
            'discovery_attempted': True, 'discovery_latency_ms': 2.5, 'discovery_success': True,
            'client_ip': '10.0.0.1', 'provider_error': None}
        with patch('client.client.run_one_shot', return_value=dynamic_result) as run:
            with contextlib.redirect_stdout(output):
                run_service_workload('FILE', '10.0.0.3', 5000, 'PING', 1, 1, 1,
                                     'dynamic', 'h1', None)
        run.assert_called_once()
        row = json.loads(output.getvalue())
        self.assertTrue(row['success'])
        self.assertEqual(row['discovery_latency_ms'], 2.5)
        self.assertEqual([a['provider_id'] for a in row['provider_attempts']], ['FILE-1', 'FILE-2'])
        self.assertEqual(row['selected_provider'], 'FILE-2')

if __name__ == '__main__': unittest.main()
