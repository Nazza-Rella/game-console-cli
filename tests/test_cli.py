import contextlib
import datetime
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import input_protocols as protocol
import dsft_transport
import console_cli as cli

# Commands without --profile use this fictitious Qingpi + LL-SPA3 device (the older profile layout).
cli.DEFAULT_PROFILE = ROOT / 'tests' / 'device.test.json'


class ProtocolTests(unittest.TestCase):
    def test_qingpi_combined_state_is_one_atomic_packet(self):
        actual = protocol.encode('qingpi', buttons=['a', 'l', 'start'], dpad='up-right',
                                 circle=[255, 0], touch=[320, 240])
        self.assertEqual(actual, [bytes.fromhex('ab140201ff0080804001f0')])
        self.assertEqual(protocol.encode('qingpi', touch=[0, 0]), protocol.release_packets('qingpi'))

    def test_sequence_does_not_release_between_steps(self):
        first = protocol.encode('qingpi', circle=(255, 128), touch=(160, 120))
        second = protocol.encode('qingpi', circle=(128, 0), touch=(160, 120), buttons=['l'])
        release = protocol.release_packets('qingpi')
        port, sleeps = mock.Mock(), []
        port.write.side_effect = len
        protocol.perform_sequence(port, [(first, .1), (second, .06)], release, sleeps.append)
        self.assertEqual([call.args[0] for call in port.write.call_args_list], release + first + second + release)
        self.assertEqual(sleeps, [.06, .1, .06, .08])

    def test_sequence_release_when_interrupted_during_hold(self):
        port = mock.Mock()
        port.write.side_effect = len
        active = protocol.encode('qingpi', touch=(200, 100), circle=(0, 255))
        release = protocol.release_packets('qingpi')
        def sleep(seconds):
            if seconds == .2:
                raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            protocol.perform_sequence(port, [(active, .2), (active, .3)], release, sleep)
        self.assertEqual([call.args[0] for call in port.write.call_args_list], release + active + release)

    def test_reference_vectors(self):
        rows = (ROOT / 'tests' / 'vectors-qingpi-3dscon.txt').read_text().splitlines()
        self.assertEqual(len(rows), 792)
        for row in rows:
            parts = row.split('|')
            with self.subTest(vector=row):
                kind = parts[0]
                if kind == 'axis':
                    fw, value = parts[1], int(parts[2])
                    actual = protocol.encode('3dscon', fw, circle=(value, 255-value))[0]
                elif kind == 'cstick':
                    value = int(parts[1])
                    actual = protocol.encode('3dscon', 'modern', cstick=(value, 255-value))[1]
                elif kind in ('button', 'hat'):
                    actual = protocol.encode('3dscon', 'modern', button=parts[1])[0]
                elif kind == 'touch':
                    actual = protocol.encode('3dscon', 'modern', touch=(int(parts[1]), int(parts[2])))[2]
                else:
                    actual = protocol.release_packets('3dscon', 'modern')[-1]
                self.assertEqual(actual.hex(), parts[-1])

    def test_qingpi_regression(self):
        self.assertEqual(protocol.encode('qingpi')[0].hex(), 'ab00000880808080000000')
        self.assertEqual(protocol.encode('qingpi', button='a')[0].hex(), 'ab04000880808080000000')
        self.assertEqual(protocol.encode('qingpi', button='up')[0].hex(), 'ab00000080808080000000')
        self.assertEqual(protocol.encode('qingpi', touch=(320, 240))[0].hex(), 'ab000008808080804001f0')
        with self.assertRaises(ValueError):
            protocol.encode('qingpi', cstick=(0, 255))

    def test_explicit_firmware_and_bounds(self):
        for fw in (None, 'auto', 'unknown'):
            with self.assertRaises(ValueError):
                protocol.encode('3dscon', fw)
        for point in ((0, 1), (321, 1), (1, 0), (1, 241)):
            with self.assertRaises(ValueError):
                protocol.encode('3dscon', 'modern', touch=point)
        for duration in (-1, 5.01, float('nan'), float('inf')):
            with self.assertRaises(ValueError):
                protocol.validate_timing(duration, .4)

    def test_release_on_interrupt_and_write_failure(self):
        release = protocol.release_packets('3dscon', 'modern')
        active = protocol.encode('3dscon', 'modern', 'a')
        for failure in (KeyboardInterrupt(), OSError('write failed')):
            port = mock.Mock()
            writes = []
            def write(data):
                writes.append(data)
                if data == active[0] and writes.count(data) == 1:
                    raise failure
                return len(data)
            port.write.side_effect = write
            with self.assertRaises(type(failure)):
                protocol.perform_pulse(port, active, release, .1, lambda _: None)
            self.assertEqual(writes[-4:], release)

    def test_all_releases_attempted_even_if_first_release_fails(self):
        release = protocol.release_packets('3dscon', 'legacy')
        port = mock.Mock()
        port.write.side_effect = [OSError('lost port'), 3, 5, 5]
        with self.assertRaisesRegex(RuntimeError, 'not confirmed'):
            protocol.write_packets(port, release, lambda _: None, best_effort=True)
        self.assertEqual([c.args[0] for c in port.write.call_args_list], release)

    def test_short_write_fails(self):
        port = mock.Mock()
        port.write.return_value = 1
        with self.assertRaises(OSError):
            protocol.write_packets(port, [b'abc'], lambda _: None)

    def test_release_touch_gap(self):
        sleeps = []
        release = protocol.release_packets('3dscon', 'modern')
        port = mock.Mock()
        port.write.side_effect = len
        protocol.write_packets(port, release, sleeps.append)
        self.assertEqual(sleeps, [.001])


class CliTests(unittest.TestCase):
    def test_charge_hold_2300ms_and_coordinate_tracking(self):
        args = cli.parser().parse_args(['touch', '160', '120', '--seconds', '2.3', '--dry-run'])
        self.assertEqual(cli.prepare_steps(args, cli.input_settings(args))[0][1], 2.3)
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests') as temp:
            sequence = Path(temp) / 'charge.json'
            sequence.write_text(json.dumps({'steps': [
                {'seconds': 1.1, 'touch': [160, 120], 'circle': [255, 128]},
                {'seconds': 1.2, 'touch': [200, 100], 'circle': [128, 0]},
                {'seconds': 0}
            ]}))
            args = cli.parser().parse_args(['sequence', str(sequence), '--dry-run'])
            steps = cli.prepare_steps(args, cli.input_settings(args))
            port = mock.Mock()
            port.write.side_effect = len
            protocol.perform_sequence(port, steps, protocol.release_packets('qingpi'), lambda _: None)
            writes = [call.args[0] for call in port.write.call_args_list]
            self.assertEqual(writes[1][-3:], bytes([160, 0, 120]))
            self.assertEqual(writes[2][-3:], bytes([200, 0, 100]))
            self.assertEqual(writes[3][-3:], bytes([0, 0, 0]))

    def test_sequence_example_and_missing_state_defaults(self):
        args = cli.parser().parse_args(['sequence', str(ROOT / 'examples' / 'sequence.example.json'), '--dry-run'])
        steps = cli.prepare_steps(args, cli.input_settings(args))
        self.assertEqual([seconds for _, seconds in steps], [.1, .06, .05])
        self.assertEqual(steps[0][0][0].hex(), 'ab000008ff808080a00078')
        self.assertEqual(steps[1][0][0].hex(), 'ab10000880008080a00078')
        self.assertEqual(steps[2][0], protocol.release_packets('qingpi'))

    def test_entire_sequence_validated_before_serial_import(self):
        invalid_documents = [
            {'steps': []}, {'steps': [{'seconds': .1}] * 129},
            {'steps': [{'seconds': 2}] * 16},
            {'steps': [{'seconds': .1}, {'seconds': .1, 'touch': [321, 1]}]},
            {'steps': [{'seconds': .1, 'unknown': 1}]},
            {'steps': [{'seconds': .1, 'buttons': 'a'}]},
            {'steps': [{'seconds': float('nan')}]},
            {'steps': [{'seconds': True}]},
        ]
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests') as temp:
            sequence = Path(temp) / 'sequence.json'
            for document in invalid_documents:
                sequence.write_text(json.dumps(document))
                with mock.patch.dict(sys.modules, {'serial': None}), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(cli.main(['sequence', str(sequence)]), 1)

    def test_serial_sequence_opens_once_and_closes(self):
        selected = SimpleNamespace(vid=11914, pid=10, serial_number='QINGPI-TEST', device='COM_TEST')
        port = mock.Mock()
        port.write.side_effect = len
        connection = mock.MagicMock()
        connection.__enter__.return_value = port
        serial = mock.Mock()
        serial.Serial.return_value = connection
        list_ports = mock.Mock()
        list_ports.comports.return_value = [selected]
        modules = {'serial': serial, 'serial.tools': SimpleNamespace(list_ports=list_ports)}
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests') as temp, \
             mock.patch.object(cli, 'SESSION_ROOT', Path(temp)), \
             mock.patch.dict(sys.modules, modules), mock.patch.object(cli.time, 'sleep'), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(['sequence', str(ROOT / 'examples' / 'sequence.example.json'), '--wait', '0']), 0)
            self.assertEqual(list(Path(temp).rglob('*.json')), [])
        serial.Serial.assert_called_once_with('COM_TEST', 9600, timeout=.2, write_timeout=.3)
        connection.__exit__.assert_called_once()
        self.assertEqual(port.write.call_count, 5)
        self.assertEqual(port.write.call_args_list[-1].args[0], protocol.release_packets('qingpi')[0])
        value = json.loads(output.getvalue())
        self.assertEqual((value['port'], value['released']), ('COM_TEST', True))
        self.assertEqual(set(value['timing_ms']), {'connect', 'input', 'wait', 'total'})

    def fake_serial(self):
        selected = SimpleNamespace(vid=11914, pid=10, serial_number='QINGPI-TEST', device='COM_TEST')
        port = mock.Mock()
        port.write.side_effect = len
        connection = mock.MagicMock()
        connection.__enter__.return_value = port
        serial = mock.Mock()
        serial.Serial.return_value = connection
        list_ports = mock.Mock()
        list_ports.comports.return_value = [selected]
        return port, {'serial': serial, 'serial.tools': SimpleNamespace(list_ports=list_ports)}

    def test_stop_request_interrupts_a_long_hold_and_always_releases(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests') as temp:
            session = cli.InputSession(Path(temp), 'press')
            release = protocol.release_packets('qingpi')
            active = protocol.encode('qingpi', button='a')
            port = mock.Mock()
            port.write.side_effect = len
            with session:
                # After the initial release and its .06s settle, while the 5s hold is in progress.
                threading.Timer(.15, session.stop_file.write_text, args=('stop',)).start()
                begun = time.perf_counter()
                with self.assertRaises(cli.StopRequested):
                    protocol.perform_sequence(port, [(active, 5)], release, session.sleep)
                elapsed = time.perf_counter() - begun
            self.assertLess(elapsed, .5)
            self.assertEqual([c.args[0] for c in port.write.call_args_list], release + active + release)
            result = json.loads((Path(temp) / ('result-' + session.token + '.json')).read_text())
            self.assertEqual(result, {'released': True, 'error': None})
            self.assertFalse((Path(temp) / 'active.json').exists())
            self.assertFalse(session.stop_file.exists())

    def test_stop_waits_for_the_running_input_to_release(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests') as temp, mock.patch.object(cli, 'SESSION_ROOT', Path(temp)):
            directory = cli.session_directory(cli.input_settings(cli.parser().parse_args(['stop'])))
            directory.mkdir(parents=True)
            (directory / 'active.json').write_text(json.dumps(
                {'pid': 12, 'start_filetime': 34, 'token': 'own', 'command': 'sequence'}))
            def running(pid, start_filetime=None):
                # The input command sees its stop file, releases, reports and exits.
                if (directory / 'stop-own.request').exists():
                    (directory / 'result-own.json').write_text('{"released": true, "error": null}')
                    return False
                return True
            with mock.patch.object(cli, 'running', side_effect=running), mock.patch.dict(sys.modules, {'serial': None}), \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(cli.main(['stop']), 0)
            value = json.loads(output.getvalue())
            self.assertEqual((value['stopped'], value['released']), ('sequence', True))
            self.assertFalse((directory / 'stop-own.request').exists())
            self.assertFalse((directory / 'result-own.json').exists())

    def test_stop_without_a_live_input_sends_release_itself(self):
        port, modules = self.fake_serial()
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests') as temp, mock.patch.object(cli, 'SESSION_ROOT', Path(temp)):
            directory = cli.session_directory(cli.input_settings(cli.parser().parse_args(['stop'])))
            directory.mkdir(parents=True)
            # Left behind by a forcibly killed command.
            (directory / 'active.json').write_text('{"pid": 0, "token": "killed", "command": "press"}')
            with mock.patch.dict(sys.modules, modules), contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(cli.main(['stop']), 0)
            self.assertFalse((directory / 'active.json').exists())
        self.assertEqual([c.args[0] for c in port.write.call_args_list], protocol.release_packets('qingpi') * 2)
        value = json.loads(output.getvalue())
        self.assertEqual((value['port'], value['stopped'], value['released']), ('COM_TEST', None, True))

    def test_snapshot_after_input_returns_a_frame_captured_after_the_wait(self):
        port, modules = self.fake_serial()
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests') as temp, \
             mock.patch.object(cli, 'SESSION_ROOT', Path(temp) / 'sessions'):
            capture = Path(temp) / 'capture'
            capture.mkdir()
            (capture / 'process.json').write_text('{"pid": 12, "session": "cap", "save_interval_ms": 50}')
            publishing = threading.Event()
            frames = []
            def publish():
                frame = 0
                while not publishing.is_set():
                    frame += 1
                    moment = time.time()
                    frames.append((frame, moment))
                    with contextlib.suppress(OSError):
                        (capture / 'latest.png').write_bytes(b'frame %d' % frame)
                        cli.write_json_atomic(capture / 'status.json', {
                            'session': 'cap', 'frame': frame,
                            'captured_utc': datetime.datetime.fromtimestamp(moment, datetime.timezone.utc)
                                            .strftime('%Y-%m-%dT%H:%M:%S.%f') + '0Z'})
                    time.sleep(.05)
            worker = threading.Thread(target=publish)
            worker.start()
            output_png = Path(temp) / 'after.png'
            try:
                with mock.patch.dict(sys.modules, modules), mock.patch.object(cli, 'running', return_value=True), \
                     contextlib.redirect_stdout(io.StringIO()) as output:
                    begun = time.time()
                    self.assertEqual(cli.main(['press', 'a', '--wait', '.2', '--snapshot', str(output_png),
                                               '--capture-dir', str(capture)]), 0)
            finally:
                publishing.set()
                worker.join()
            value = json.loads(output.getvalue())
            captured_at = dict(frames)[value['snapshot']['frame']]
            # Never a frame from before the hold and wait finished.
            self.assertGreaterEqual(captured_at, begun + .12 + .2)
            self.assertTrue(output_png.read_bytes().startswith(b'frame '))
            self.assertEqual(set(value['timing_ms']), {'connect', 'input', 'wait', 'snapshot', 'total'})

    def test_snapshot_is_refused_before_any_input_when_capture_is_not_running(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests') as temp, \
             mock.patch.dict(sys.modules, {'serial': None}), contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(cli.main(['press', 'a', '--snapshot', str(Path(temp) / 'x.png'),
                                       '--capture-dir', temp]), 1)
        self.assertIn('No active capture session', error.getvalue())

    def test_sequence_from_standard_input(self):
        document = json.dumps({'steps': [{'seconds': .1, 'buttons': ['a']}, {'seconds': .05}]})
        with mock.patch.object(sys, 'stdin', io.StringIO(document)), mock.patch.dict(sys.modules, {'serial': None}), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(['sequence', '-', '--dry-run']), 0)
        self.assertEqual([step['seconds'] for step in json.loads(output.getvalue())['steps']], [.1, .05])

    def test_capture_time_with_seven_fraction_digits(self):
        self.assertAlmostEqual(cli.utc_seconds('2026-10-18T03:12:01.1234567Z'),
                               datetime.datetime(2026, 10, 18, 3, 12, 1, tzinfo=datetime.timezone.utc).timestamp()
                               + .1234567, places=6)
        self.assertIsNone(cli.utc_seconds(None))

    def test_state_dry_run(self):
        with mock.patch.dict(sys.modules, {'serial': None}), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(['state', '--buttons', 'a', 'l', '--circle', '255', '128',
                                      '--touch', '160', '120', '--dry-run']), 0)
        self.assertEqual(json.loads(output.getvalue())['active'], ['ab 14 00 08 ff 80 80 80 a0 00 78'])

    def test_exact_device_selection(self):
        settings = dict(vid=0x16c0, pid=0x483, serial='wanted')
        desired = SimpleNamespace(vid=5824, pid=1155, serial_number='wanted', device='COM99')
        other = SimpleNamespace(vid=5824, pid=1155, serial_number='other', device='COM6')
        self.assertIs(cli.select_port([other, desired], settings), desired)
        for ports in ([other], [desired, desired], []):
            with self.assertRaises(RuntimeError):
                cli.select_port(ports, settings)
        with self.assertRaises(ValueError):
            cli.select_port([desired], dict(vid=5824, pid=1155))

    def test_backend_override_cannot_reuse_profile_identity(self):
        args = cli.parser().parse_args(['press', 'a', '--backend', '3dscon', '--firmware', 'modern', '--dry-run'])
        settings = cli.input_settings(args)
        self.assertEqual(settings, {'backend': '3dscon', 'firmware': 'modern'})

    def test_dry_run_never_imports_or_opens_serial(self):
        with mock.patch.dict(sys.modules, {'serial': None}), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(['press', 'a', '--backend', '3dscon',
                                       '--firmware', 'modern', '--dry-run']), 0)
        value = json.loads(output.getvalue())
        self.assertEqual(value['baud'], 115200)
        self.assertEqual(value['active'][0], 'a1 10 00 a2 7f 7f')
        self.assertEqual(len(value['final_release']), 4)

    def test_capture_backend_isolates_profile_device_and_directory(self):
        args = cli.parser().parse_args(['capture', 'start', '--backend', 'n3dsxl'])
        self.assertIsNone(args.capture_exe)
        self.assertIsNone(args.save_interval_ms)
        backend, device, directory = cli.capture_settings(args)
        self.assertEqual(backend, 'n3dsxl')
        self.assertIsNone(device)
        self.assertEqual(directory.name, 'capture-n3dsxl')
        with mock.patch('subprocess.Popen', side_effect=AssertionError('must not launch')):
            with self.assertRaisesRegex(ValueError, 'requires --device'):
                cli.capture(args)

    def test_legacy_profile_still_selects_qingpi_llspa3(self):
        args = cli.parser().parse_args(['press', 'up', '--dry-run'])
        self.assertEqual(cli.input_settings(args)['backend'], 'qingpi')
        args = cli.parser().parse_args(['capture', 'status'])
        self.assertEqual(cli.capture_settings(args)[:2],
                         ('ll-spa3', 'LlSpa3:path:TEST'))

    def test_stale_snapshot_rejected_even_if_worker_runs(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests') as temp:
            path = Path(temp)
            (path / 'process.json').write_text('{"pid":12,"session":"abc"}')
            (path / 'status.json').write_text('{"session":"abc"}')
            (path / 'latest.png').write_bytes(b'old')
            os.utime(path / 'latest.png', (0, 0))
            args = cli.parser().parse_args(['snapshot', str(path / 'output.png'), '--capture-dir', temp])
            with mock.patch.object(cli, 'running', return_value=True):
                with self.assertRaisesRegex(RuntimeError, 'stale'):
                    cli.snapshot(args)
            self.assertFalse((path / 'output.png').exists())

    def test_stop_only_targets_its_session(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests') as temp:
            path = Path(temp)
            (path / 'process.json').write_text('{"pid":12,"session":"own","start_filetime":123}')
            args = cli.parser().parse_args(['capture', 'stop', '--capture-dir', temp])
            with mock.patch.object(cli, 'running', side_effect=[True, False, False]), contextlib.redirect_stdout(io.StringIO()):
                cli.capture(args)
            self.assertTrue((path / 'stop-own.request').exists())
            self.assertFalse((path / 'stop.request').exists())

    def test_start_timeout_requests_only_its_worker_stop(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests') as temp:
            args = cli.parser().parse_args(['capture', 'start', '--backend', 'n3dsxl',
                                           '--device', 'N3dsxl:serial:test', '--capture-dir', temp,
                                           '--capture-exe', str(ROOT / 'CaptureCli.Custom.exe'),
                                           '--save-interval-ms', '20'])
            process = mock.Mock(pid=123)
            process.poll.return_value = None
            with mock.patch.object(cli.subprocess, 'Popen', return_value=process) as launch, \
                 mock.patch.object(cli.time, 'monotonic', side_effect=[0, 16]):
                with self.assertRaisesRegex(RuntimeError, 'No first frame'):
                    cli.capture(args)
            self.assertEqual(len(list(Path(temp).glob('stop-*.request'))), 1)
            process.wait.assert_called_once_with(timeout=5)
            process.kill.assert_not_called()
            process.terminate.assert_not_called()
            command = launch.call_args.args[0]
            self.assertEqual(command[0], str(ROOT / 'CaptureCli.Custom.exe'))
            self.assertEqual(command[-2:], ['--save-interval-ms', '20'])

    def test_obs_start_copies_the_page_and_passes_the_screen(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests') as temp:
            args = cli.parser().parse_args(['capture', 'start', '--backend', 'n3dsxl',
                                           '--device', 'N3dsxl:serial:test', '--capture-dir', temp, '--obs', 'top'])
            process = mock.Mock(pid=123)
            process.poll.return_value = None
            with mock.patch.object(cli.subprocess, 'Popen', return_value=process) as launch, \
                 mock.patch.object(cli.time, 'monotonic', side_effect=[0, 16]):
                with self.assertRaisesRegex(RuntimeError, 'No first frame'):
                    cli.capture(args)
            self.assertEqual(launch.call_args.args[0][-2:], ['--obs', 'top'])
            for name in ('obs.html', 'obs-audio.js'):
                self.assertEqual((Path(temp) / name).read_bytes(), (ROOT / 'web' / name).read_bytes())
        self.assertEqual(cli.parser().parse_args(['capture', 'start', '--obs']).obs, 'both')
        self.assertIsNone(cli.parser().parse_args(['capture', 'start']).obs)

    def test_obs_is_refused_for_hdmi_before_launch(self):
        with mock.patch.object(cli.subprocess, 'Popen') as launch, contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(cli.main(['capture', 'start', '--backend', 'dshow', '--device', 'Dshow:x', '--obs']), 1)
        launch.assert_not_called()
        self.assertIn('--obs is only for the 3DS/DS backends', error.getvalue())

    def test_view_start_passes_the_port_and_prints_the_addresses(self):
        for backend, device in (('dshow', 'Dshow:x'), ('n3dsxl', 'N3dsxl:serial:test')):
            with tempfile.TemporaryDirectory(dir=ROOT / 'tests') as temp:
                token = 'a' * 32
                (Path(temp) / 'status.json').write_text(json.dumps({'session': token}))
                urls = ['http://192.168.11.12:8080/', 'http://100.101.102.103:8080/']
                (Path(temp) / 'process.json').write_text(json.dumps({'session': token, 'view': {'port': 8080, 'urls': urls}}))
                process = mock.Mock(pid=123)
                process.poll.return_value = None
                with mock.patch.object(cli.subprocess, 'Popen', return_value=process) as launch, \
                     mock.patch.object(cli.uuid, 'uuid4', return_value=SimpleNamespace(hex=token)), \
                     contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(cli.main(['capture', 'start', '--backend', backend, '--device', device,
                                               '--capture-dir', temp, '--view', '8080']), 0)
                self.assertEqual(launch.call_args.args[0][-2:], ['--view', '8080'])
                self.assertEqual(json.loads(output.getvalue())['view_urls'], urls)
        self.assertEqual(cli.parser().parse_args(['capture', 'start', '--view']).view, 8000)
        self.assertIsNone(cli.parser().parse_args(['capture', 'start']).view)

    def test_capture_list_needs_no_default_profile_yet(self):
        missing = ROOT / 'tests' / 'no-such-device.json'
        with mock.patch.object(cli, 'DEFAULT_PROFILE', missing), \
             mock.patch.object(cli.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run:
            self.assertEqual(cli.main(['capture', 'list', '--backend', 'dshow']), 0)
        self.assertEqual(run.call_args.args[0][1:], ['list', 'dshow'])
        with mock.patch.object(cli.subprocess, 'run') as run, contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(cli.main(['--profile', str(missing), 'capture', 'list', '--backend', 'dshow']), 1)
        run.assert_not_called()
        self.assertIn('FileNotFoundError', error.getvalue())

    def test_view_port_is_checked_before_launch(self):
        with mock.patch.object(cli.subprocess, 'Popen') as launch, contextlib.redirect_stderr(io.StringIO()) as error:
            for port in ('80', '1023', '65536'):
                self.assertEqual(cli.main(['capture', 'start', '--backend', 'dshow', '--device', 'Dshow:x', '--view', port]), 1)
        launch.assert_not_called()
        self.assertIn('--view PORT must be 1024..65535', error.getvalue())

    def test_invalid_capture_interval_never_launches_worker(self):
        with mock.patch.object(cli.subprocess, 'Popen') as launch, \
             mock.patch.object(cli, 'running') as running, contextlib.redirect_stderr(io.StringIO()):
            for interval in ('0', '19', '10001'):
                self.assertEqual(cli.main(['capture', 'start', '--save-interval-ms', interval]), 1)
        launch.assert_not_called()
        running.assert_not_called()


class FakeDsFtNative:
    def __init__(self, devices=None, identity=None, fail=None):
        self.devices = devices if devices is not None else [
            {'serial': 'DS-TEST', 'description': 'DS FT', 'type': 8, 'id': 0x04036014, 'open': False}]
        self.identity_value = identity
        self.fail = fail
        self.calls, self.writes = [], []

    def call(self, name):
        self.calls.append(name)
        if name == self.fail:
            raise OSError('fake failure: ' + name)

    def enumerate(self):
        self.call('enumerate')
        return [dict(device) for device in self.devices]

    def open(self, serial):
        self.call('open')
        return 42

    def identity(self, handle):
        self.call('identity')
        return self.identity_value or {key: self.devices[0][key] for key in ('serial', 'description', 'type', 'id')}

    def reset(self, handle):
        self.call('reset')

    def timeouts(self, handle, read, write):
        self.call('timeouts:%d,%d' % (read, write))

    def bit_mode(self, handle, mask, mode):
        self.call('mode:%d,%d' % (mask, mode))

    def latency(self, handle, milliseconds):
        self.call('latency:%d' % milliseconds)

    def write(self, handle, data):
        self.call('write')
        self.writes.append(bytes(data))
        return len(data)

    def close(self, handle):
        self.call('close')


class DsFtTests(unittest.TestCase):
    neutral = bytes.fromhex('82ff0080ff00')

    def test_reference_vectors(self):
        names = ['a', 'b', 'x', 'y', 'l', 'r', 'select', 'start', 'power']
        rows = (ROOT / 'tests' / 'vectors-dsft.txt').read_text().splitlines()
        self.assertEqual(len(rows), 13824)
        for row in rows:
            revision, mask, hat, expected = row.split('|')
            buttons = [name for index, name in enumerate(names) if int(mask) & (1 << index)]
            actual = protocol.encode('dsft', buttons=buttons, dpad=hat, revision=int(revision))[0]
            self.assertEqual(actual.hex(), expected, row)

    def test_explicit_revision_and_digital_only(self):
        for revision in (None, 0, 4, '3', True):
            with self.assertRaises(ValueError):
                protocol.encode('dsft', revision=revision)
        for state in ({'touch': (160, 120)}, {'circle': (255, 128)}, {'cstick': (128, 0)},
                      {'buttons': ['home']}, {'buttons': ['zl']}, {'buttons': ['zr']}):
            with self.assertRaises(ValueError):
                protocol.encode('dsft', revision=3, **state)
        for backend, firmware in (('qingpi', None), ('3dscon', 'modern'), ('ns', None)):
            with self.assertRaisesRegex(ValueError, 'not available'):
                protocol.encode(backend, firmware, button='power')
        self.assertEqual(protocol.release_packets('dsft', revision=3), [self.neutral])

    def test_listing_filters_capture_boards_and_ambiguous_serials(self):
        devices = [
            {'serial': 'DS-TEST', 'description': 'DS FT', 'type': 8, 'id': 1, 'open': False},
            {'serial': 'BUSY', 'description': '', 'type': 8, 'id': 1, 'open': True},
            {'serial': 'CAP', 'description': 'NDS.1', 'type': 8, 'id': 1, 'open': False},
            {'serial': 'TWICE', 'description': '', 'type': 8, 'id': 1, 'open': False},
            {'serial': 'TWICE', 'description': '', 'type': 8, 'id': 1, 'open': False},
            {'serial': 'FT232R', 'description': '', 'type': 5, 'id': 1, 'open': False},
            {'serial': ' ', 'description': '', 'type': 8, 'id': 1, 'open': False},
        ]
        self.assertEqual([d['serial'] for d in dsft_transport.candidates(devices)], ['DS-TEST', 'BUSY'])

    def test_safe_initialization_order_and_release_before_close(self):
        native = FakeDsFtNative()
        with dsft_transport.DsFtPort('DS-TEST', 3, native) as port:
            self.assertEqual(native.calls, ['enumerate', 'open', 'identity', 'reset', 'timeouts:250,250',
                                            'mode:0,0', 'mode:0,2', 'write', 'latency:2'])
            active = protocol.encode('dsft', button='a', revision=3)
            protocol.perform_sequence(port, [(active, .1)], protocol.release_packets('dsft', revision=3),
                                      lambda _: None)
        self.assertEqual(native.writes, [self.neutral, self.neutral, bytes.fromhex('82ef1080ff00'),
                                         self.neutral, self.neutral])
        self.assertEqual(native.calls[-2:], ['write', 'close'])

    def test_invalid_selection_never_opens(self):
        for devices in ([], [{'serial': 'DS-TEST', 'description': 'DS FT', 'type': 8, 'id': 1, 'open': True}],
                        [{'serial': 'DS-TEST', 'description': 'NDS.2', 'type': 8, 'id': 1, 'open': False}],
                        [{'serial': 'DS-TEST', 'description': '', 'type': 5, 'id': 1, 'open': False}],
                        [{'serial': 'DS-TEST', 'description': '', 'type': 8, 'id': 1, 'open': False}] * 2):
            native = FakeDsFtNative(devices)
            with self.assertRaises(OSError):
                with dsft_transport.DsFtPort('DS-TEST', 3, native):
                    pass
            self.assertEqual(native.calls, ['enumerate'])

    def test_identity_mismatch_closes_without_writing(self):
        native = FakeDsFtNative(identity={'serial': 'OTHER', 'description': 'DS FT', 'type': 8, 'id': 0x04036014})
        with self.assertRaises(OSError):
            with dsft_transport.DsFtPort('DS-TEST', 3, native):
                pass
        self.assertEqual(native.calls, ['enumerate', 'open', 'identity', 'close'])
        self.assertEqual(native.writes, [])

    def test_failure_before_mpsse_never_writes_gpio(self):
        for failure in ('reset', 'timeouts:250,250', 'mode:0,0', 'mode:0,2'):
            native = FakeDsFtNative(fail=failure)
            with self.assertRaises(OSError):
                with dsft_transport.DsFtPort('DS-TEST', 3, native):
                    pass
            self.assertEqual(native.writes, [], failure)
            self.assertEqual(native.calls[-1], 'close', failure)

    def test_cli_dry_run_and_validation_before_opening(self):
        with mock.patch.dict(sys.modules, {'serial': None}), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(['press', 'a', '--backend', 'dsft', '--revision', '3', '--dry-run']), 0)
        value = json.loads(output.getvalue())
        self.assertEqual(value['active'], ['82 ef 10 80 ff 00'])
        self.assertEqual(value['final_release'], ['82 ff 00 80 ff 00'])
        with mock.patch.object(dsft_transport, 'DsFtPort', side_effect=AssertionError('must not open')), \
             contextlib.redirect_stderr(io.StringIO()):
            for argv in (['press', 'a', '--backend', 'dsft', '--serial', 'DS-TEST'],
                         ['touch', '160', '120', '--backend', 'dsft', '--serial', 'DS-TEST', '--revision', '3'],
                         ['circle', '255', '128', '--backend', 'dsft', '--serial', 'DS-TEST', '--revision', '3'],
                         ['press', 'home', '--backend', 'dsft', '--serial', 'DS-TEST', '--revision', '3']):
                self.assertEqual(cli.main(argv), 1, argv)

    def test_cli_sends_through_selected_ds_ft(self):
        native = FakeDsFtNative()
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests') as temp, mock.patch.object(cli, 'SESSION_ROOT', Path(temp)), \
             mock.patch.object(dsft_transport, 'DsFtNative', return_value=native), \
             mock.patch.object(cli.time, 'sleep'), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(['state', '--buttons', 'l', 'r', 'start', 'select', '--backend', 'dsft',
                                       '--serial', 'DS-TEST', '--revision', '2', '--wait', '0']), 0)
        self.assertEqual(json.loads(output.getvalue())['device'], 'DSFT:DS-TEST')
        self.assertEqual(native.writes[2], bytes.fromhex('82ff0080f00f'))
        self.assertEqual(native.writes[-1], self.neutral)
        self.assertEqual(native.calls[-1], 'close')


class NsTests(unittest.TestCase):
    def test_reference_vectors(self):
        rows = (ROOT / 'tests' / 'vectors-ns.txt').read_text().splitlines()
        self.assertEqual(len(rows), 280)
        every = ['y', 'x', 'b', 'a', 'r', 'zr', 'minus', 'plus', 'rclick', 'lclick', 'home', 'capture', 'l', 'zl']
        for row in rows:
            parts = row.split('|')
            if parts[0] == 'button':
                actual = protocol.encode('ns', buttons=every if parts[1] == 'all' else [parts[1]])
            elif parts[0] == 'hat':
                actual = protocol.encode('ns', dpad=parts[1])
            elif parts[0] == 'axis':
                v = int(parts[1])
                actual = protocol.encode('ns', circle=(v, 255 - v), cstick=((v + 85) % 256, (v + 170) % 256))
            else:
                actual = protocol.release_packets('ns')
            self.assertEqual(actual[0].hex(), parts[-1], row)

    def test_switch_names_and_no_touch(self):
        self.assertEqual(protocol.encode('ns', button='plus'), protocol.encode('ns', button='start'))
        self.assertEqual(protocol.encode('ns', button='minus'), protocol.encode('ns', button='select'))
        with self.assertRaises(ValueError):
            protocol.encode('ns', touch=(160, 120))
        for name in ('plus', 'minus', 'lclick', 'rclick', 'capture'):
            with self.assertRaisesRegex(ValueError, 'not available'):
                protocol.encode('qingpi', button=name)

    def test_stick_aliases_and_right_stick(self):
        with mock.patch.dict(sys.modules, {'serial': None}), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(['lstick', '255', '128', '--backend', 'ns', '--dry-run']), 0)
        value = json.loads(output.getvalue())
        self.assertEqual(value['baud'], 9600)
        self.assertEqual(value['active'], ['ab 00 00 08 ff 80 80 80 00 00 00'])
        with mock.patch.dict(sys.modules, {'serial': None}), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(['rstick', '128', '0', '--backend', 'ns', '--dry-run']), 0)
        self.assertEqual(json.loads(output.getvalue())['active'], ['ab 00 00 08 80 80 80 00 00 00 00'])
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(['rstick', '128', '0', '--backend', 'qingpi', '--dry-run']), 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)

