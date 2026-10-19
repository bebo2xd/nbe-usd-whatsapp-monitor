import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
import monitor


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patch = patch.object(monitor, 'STATE', Path(self.temp.name) / 'state.json')
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def test_initial_unchanged_change_and_normalization(self):
        sender = Mock()
        monitor.run(reader=lambda: '50', sender=sender)
        sender.assert_called_once_with('50', None)
        sender.reset_mock()
        monitor.run(reader=lambda: monitor.normalize_price('50.0000'), sender=sender)
        sender.assert_not_called()
        monitor.run(reader=lambda: '50.1', sender=sender)
        sender.assert_called_once_with('50.1', '50')

    def test_send_failure_preserves_baseline_for_retry(self):
        monitor.run(reader=lambda: '50', sender=Mock())
        before = monitor.STATE.read_text()
        with self.assertRaises(RuntimeError):
            monitor.run(reader=lambda: '51', sender=Mock(side_effect=RuntimeError('offline')))
        self.assertEqual(monitor.STATE.read_text(), before)

    def test_read_failure_and_dry_run_do_not_initialize(self):
        sender = Mock()
        monitor.run(True, reader=lambda: '50', sender=sender)
        sender.assert_not_called()
        self.assertFalse(monitor.STATE.exists())
        with self.assertRaises(RuntimeError):
            monitor.run(reader=Mock(side_effect=RuntimeError('NBE unavailable')), sender=sender)
        self.assertFalse(monitor.STATE.exists())

    def test_invalid_prices_fail_closed(self):
        for value in ('', '0', '-1', 'NaN', 'Buy 50', '50 51', '1,234.5'):
            with self.assertRaises(ValueError):
                monitor.normalize_price(value)

    def test_instance_discovery_flat_and_nested_and_ambiguous(self):
        for data in ([{'name': 'one', 'connectionStatus': 'open'}],
                     [{'instance': {'instanceName': 'one', 'state': 'open'}}]):
            with patch.object(monitor, 'api', return_value=data):
                self.assertEqual(monitor.resolve_instance('https://example.com', 'secret'), 'one')
        with patch.object(monitor, 'api', return_value=[
                {'name': 'one', 'connectionStatus': 'open'},
                {'name': 'two', 'connectionStatus': 'open'}]):
            with self.assertRaises(RuntimeError):
                monitor.resolve_instance('https://example.com', 'secret')

    def test_corrupt_state_does_not_silently_reset(self):
        monitor.STATE.write_text('{}')
        with self.assertRaises(ValueError):
            monitor.run(reader=lambda: '50', sender=Mock())


if __name__ == '__main__':
    unittest.main()
