"""Game console CLI for Windows x64: input through Qingpi/3DS-Con/DS FT/NS adapters, capture through Capture3DS and DirectShow."""
import argparse
import contextlib
import ctypes
import datetime
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid

from input_protocols import (BUTTON_NAMES, KEY_NAMES, HATS, ReleaseNotConfirmed, encode, release_packets,
                             perform_sequence, validate_timing)

ROOT = Path(__file__).resolve().parent
CAPTURE_EXE = ROOT / 'CaptureCli.exe'
DEFAULT_PROFILE = ROOT / 'device.json'
SESSION_ROOT = ROOT / 'input-sessions'
BACKENDS = ('ll-spa3', 'ds-ftd2', 'n3dsxl', 'loopy-old3ds', 'dshow')
BAUD = {'qingpi': 9600, '3dscon': 115200, 'dsft': None, 'ns': 9600}
STOPPED_EXIT_CODE = 3


class StopRequested(Exception):
    pass


def load_profile(args):
    return json.loads(Path(args.profile).read_text(encoding='utf-8-sig'))


def input_settings(args):
    profile = load_profile(args)
    configured = profile.get('input', {})
    if not configured and 'qingpi' in profile:
        configured = dict(profile['qingpi'], backend='qingpi')
    backend = args.backend or configured.get('backend')
    selected = dict(configured) if backend == configured.get('backend') else {}
    selected['backend'] = backend
    for name in ('vid', 'pid', 'serial', 'firmware', 'revision'):
        value = getattr(args, name, None)
        if value is not None:
            selected[name] = value
    return selected


def usb_id(value):
    result = int(value, 0) if isinstance(value, str) else value
    if type(result) is not int or not 0 <= result <= 65535:
        raise ValueError('VID/PID must be integers in 0..65535 or strings such as 0x16c0')
    return result


def select_port(ports, settings):
    if any(settings.get(key) in (None, '') for key in ('vid', 'pid', 'serial')):
        raise ValueError('Specify exact USB VID, PID and serial in the profile or input options')
    matches = [p for p in ports if p.vid == usb_id(settings['vid'])
               and p.pid == usb_id(settings['pid']) and p.serial_number == settings['serial']]
    if len(matches) != 1:
        raise RuntimeError('Exactly one matching USB serial device is required; found ' + str(len(matches)))
    return matches[0]


def devices(args):
    if args.backend == 'dsft':
        from dsft_transport import DsFtNative, candidates
        print(json.dumps([{'serial': d['serial'], 'description': d['description'], 'open': d['open']}
                          for d in candidates(DsFtNative().enumerate())], ensure_ascii=False))
        return
    from serial.tools import list_ports
    print(json.dumps([{'port': p.device, 'vid': p.vid, 'pid': p.pid,
                       'serial': p.serial_number, 'location': p.location,
                       'description': p.description} for p in list_ports.comports()], ensure_ascii=False))


def prepare_steps(args, settings):
    validate_timing(0, args.wait)
    backend, firmware, revision = settings['backend'], settings.get('firmware'), settings.get('revision')
    if args.command == 'sequence':
        text = sys.stdin.read() if args.file == '-' else Path(args.file).read_text(encoding='utf-8-sig')
        document = json.loads(text)
        if not isinstance(document, dict) or set(document) != {'steps'}:
            raise ValueError('Sequence must be a JSON object containing only a steps array')
        steps = document['steps']
        if not isinstance(steps, list) or not 1 <= len(steps) <= 128:
            raise ValueError('Sequence must contain 1..128 steps')
        result, total = [], 0
        for step in steps:
            if not isinstance(step, dict) or 'seconds' not in step or set(step) - {'seconds', 'buttons', 'dpad', 'circle', 'cstick', 'touch'}:
                raise ValueError('Each step needs seconds and optional buttons/dpad/circle/cstick/touch')
            seconds = step['seconds']
            if type(seconds) not in (int, float):
                raise ValueError('Step seconds must be a number')
            validate_timing(seconds, 0)
            if not isinstance(step.get('buttons', []), list):
                raise ValueError('Step buttons must be an array, such as ["a", "b"]')
            state = {key: value for key, value in step.items() if key != 'seconds'}
            result.append((encode(backend, firmware, revision=revision, **state), seconds))
            total += seconds
        if total > 30:
            raise ValueError('Total sequence duration must be at most 30 seconds')
        return result
    validate_timing(args.seconds, args.wait)
    if args.command == 'state':
        return [(encode(backend, firmware, buttons=args.buttons, dpad=args.dpad,
                        circle=args.circle, cstick=args.cstick, touch=args.touch, revision=revision), args.seconds)]
    button = args.button if args.command == 'press' else 'neutral'
    touch = (args.x, args.y) if args.command == 'touch' else None
    circle = (args.x, args.y) if args.command == 'circle' else (128, 128)
    cstick = (args.x, args.y) if args.command == 'cstick' else (128, 128)
    if args.command == 'cstick' and backend not in ('3dscon', 'ns'):
        raise ValueError('cstick requires the 3dscon or ns backend')
    return [(encode(backend, firmware, button, touch, circle, cstick, revision=revision), args.seconds)]


def session_directory(settings):
    """One directory per physical input device, shared by every command that addresses it."""
    if settings['backend'] == 'dsft':
        if not settings.get('serial') or not str(settings['serial']).strip():
            raise ValueError('DS FT requires the exact FT232H serial in the profile or --serial')
        identity = 'dsft-' + str(settings['serial'])
    else:
        if any(settings.get(key) in (None, '') for key in ('vid', 'pid', 'serial')):
            raise ValueError('Specify exact USB VID, PID and serial in the profile or input options')
        identity = '%04x-%04x-%s' % (usb_id(settings['vid']), usb_id(settings['pid']), settings['serial'])
    return SESSION_ROOT / re.sub(r'[^A-Za-z0-9_.-]', '_', identity)


def process_start_filetime():
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    kernel.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(ctypes.c_ulonglong)] * 4
    times = [ctypes.c_ulonglong() for _ in range(4)]
    if not kernel.GetProcessTimes(kernel.GetCurrentProcess(), *(ctypes.byref(t) for t in times)):
        raise OSError('Could not read the current process start time')
    return times[0].value


def write_json_atomic(path, value):
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    temp.write_text(json.dumps(value))
    os.replace(temp, path)


class InputSession:
    """Marks the device as in use, and turns a stop request from another process into StopRequested.

    Holds and waits poll the session-specific stop file every 5 ms. After the first stop, sleeps no
    longer raise, so the release in perform_sequence's finally block always runs to completion.
    """

    def __init__(self, directory, command):
        self.directory = directory
        self.command = command
        self.token = uuid.uuid4().hex
        self.active = directory / 'active.json'
        self.stop_file = directory / ('stop-' + self.token + '.request')
        self.stopped = False

    def __enter__(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        current = read_json(self.active) if self.active.exists() else None
        if current and running(current.get('pid'), current.get('start_filetime')):
            raise RuntimeError('Another input command is running on this device; wait for it or use stop')
        write_json_atomic(self.active, {'pid': os.getpid(), 'start_filetime': process_start_filetime(),
                                        'token': self.token, 'command': self.command})
        return self

    def sleep(self, seconds):
        deadline = time.perf_counter() + seconds
        while True:
            if not self.stopped and self.stop_file.exists():
                self.stopped = True
                raise StopRequested()
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                return
            time.sleep(min(remaining, .005))

    def __exit__(self, kind, error, trace):
        if self.stopped or self.stop_file.exists():
            detail = None if error is None or isinstance(error, StopRequested) else type(error).__name__ + ': ' + str(error)
            write_json_atomic(self.directory / ('result-' + self.token + '.json'),
                              {'released': not isinstance(error, ReleaseNotConfirmed), 'error': detail})
        with contextlib.suppress(OSError):
            self.stop_file.unlink()
        current = read_json(self.active) if self.active.exists() else None
        if current and current.get('token') == self.token:
            with contextlib.suppress(OSError):
                self.active.unlink()
        return False


@contextlib.contextmanager
def open_input(settings):
    """Yield (port, output key, device label) for the selected input device."""
    if settings['backend'] == 'dsft':
        from dsft_transport import DsFtPort
        with DsFtPort(settings.get('serial'), settings.get('revision')) as port:
            yield port, 'device', 'DSFT:' + settings['serial']
        return
    import serial
    from serial.tools import list_ports
    selected = select_port(list_ports.comports(), settings)
    # Windows opens serial ports exclusively. Never close an owner or try another port.
    with serial.Serial(selected.device, BAUD[settings['backend']], timeout=.2, write_timeout=.3) as port:
        yield port, 'port', selected.device


def milliseconds(start):
    return round((time.perf_counter() - start) * 1000, 1)


def input_pulse(args):
    started = time.perf_counter()
    settings = input_settings(args)
    backend, firmware = settings['backend'], settings.get('firmware')
    steps = prepare_steps(args, settings)
    release = release_packets(backend, firmware, settings.get('revision'))
    if args.dry_run:
        description = {'dry_run': True, 'backend': backend, 'baud': BAUD[backend],
                       'initial_release': [p.hex(' ') for p in release],
                       'steps': [{'seconds': seconds, 'packets': [p.hex(' ') for p in packets]}
                                 for packets, seconds in steps],
                       'final_release': [p.hex(' ') for p in release]}
        if len(steps) == 1:
            description['active'] = description['steps'][0]['packets']
        print(json.dumps(description))
        return
    # Refuse before any input is sent when the requested screen cannot be taken afterwards.
    capture_dir = capture_session = None
    if args.snapshot:
        capture_dir = capture_directory(args, args.capture_backend or load_profile(args).get('capture', {}).get('backend', 'll-spa3'))
        capture_session = live_capture_session(capture_dir)
    result = {'backend': backend, 'input': args.command, 'released': True}
    timing = {}
    phase = 'connect'
    try:
        with InputSession(session_directory(settings), args.command) as session:
            with open_input(settings) as (port, key, label):
                result = dict({key: label}, **result)
                timing['connect'] = milliseconds(started)
                phase, mark = 'input', time.perf_counter()
                perform_sequence(port, steps, release, session.sleep)
                timing['input'] = milliseconds(mark)
            phase, mark = 'wait', time.perf_counter()
            session.sleep(args.wait)
            timing['wait'] = milliseconds(mark)
            if capture_dir is not None:
                phase, mark = 'snapshot', time.perf_counter()
                destination = Path(args.snapshot).resolve()
                status, captured = fresh_snapshot(capture_dir, capture_session, destination, time.time(), session.sleep)
                timing['snapshot'] = milliseconds(mark)
                result['snapshot'] = {'path': str(destination), 'frame': status.get('frame'),
                                      'captured_utc': status.get('captured_utc') or status.get('utc'),
                                      'age_ms': round((time.time() - captured) * 1000, 1)}
    except StopRequested:
        result['stopped'] = phase
    except Exception as error:
        # Report where it failed and how long each finished phase took; the exit code stays 1.
        result['released'] = not isinstance(error, ReleaseNotConfirmed)
        timing['total'] = milliseconds(started)
        print(json.dumps(dict(result, error=type(error).__name__ + ': ' + str(error), phase=phase, timing_ms=timing)))
        raise
    timing['total'] = milliseconds(started)
    result['timing_ms'] = timing
    print(json.dumps(result))
    return STOPPED_EXIT_CODE if 'stopped' in result else 0


def stop_input(args):
    started = time.perf_counter()
    settings = input_settings(args)
    directory = session_directory(settings)
    active = directory / 'active.json'
    current = read_json(active) if active.exists() else None
    if current and running(current.get('pid'), current.get('start_filetime')):
        stop_file = directory / ('stop-' + current['token'] + '.request')
        result_path = directory / ('result-' + current['token'] + '.json')
        stop_file.write_text('stop')
        deadline = time.monotonic() + args.timeout
        while running(current['pid'], current.get('start_filetime')) and time.monotonic() < deadline:
            time.sleep(.005)
        if running(current['pid'], current.get('start_filetime')):
            raise RuntimeError('Stop requested, but the input command did not exit within %gs. No force kill performed.' % args.timeout)
        detail = read_json(result_path) if result_path.exists() else None
        for path in (stop_file, result_path):
            with contextlib.suppress(OSError):
                path.unlink()
        released = bool(detail and detail.get('released'))
        print(json.dumps({'stopped': current.get('command'), 'released': released,
                          'error': detail.get('error') if detail else 'The input command exited without reporting a release',
                          'timing_ms': {'total': milliseconds(started)}}))
        return 0 if released else 1
    # No live input command, for example after a forced kill. Release everything from here.
    with contextlib.suppress(OSError):
        active.unlink()
    release = release_packets(settings['backend'], settings.get('firmware'), settings.get('revision'))
    with InputSession(directory, 'stop') as session:
        with open_input(settings) as (port, key, label):
            perform_sequence(port, [], release, session.sleep)
    print(json.dumps({key: label, 'stopped': None, 'released': True, 'timing_ms': {'total': milliseconds(started)}}))
    return 0


def running(pid, start_filetime=None):
    if not pid:
        return False
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    kernel.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(ctypes.c_ulonglong)] * 4
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:
            return False
        if start_filetime is not None:
            times = [ctypes.c_ulonglong() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                return False
            return times[0].value == start_filetime
        return True
    finally:
        kernel.CloseHandle(handle)


def read_json(path):
    for _ in range(20):
        try:
            return json.loads(path.read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            time.sleep(.025)
    return None


def capture_directory(args, backend):
    default_dir = 'capture' if backend == 'll-spa3' else 'capture-' + backend
    return Path(args.capture_dir).resolve() if args.capture_dir else ROOT / default_dir


def capture_settings(args):
    configured = load_profile(args).get('capture', {})
    configured_backend = configured.get('backend', 'll-spa3')
    backend = args.backend or configured_backend
    if backend not in BACKENDS:
        raise ValueError('Unknown capture backend: ' + str(backend))
    device = args.device or (configured.get('stable_id') if backend == configured_backend else None)
    return backend, device, capture_directory(args, backend)


def capture(args):
    backend, device, directory = capture_settings(args)
    executable = Path(args.capture_exe).resolve() if args.capture_exe else CAPTURE_EXE
    if args.save_interval_ms is not None and not 20 <= args.save_interval_ms <= 10000:
        raise ValueError('Save interval must be 20..10000 milliseconds')
    if (args.size or args.mjpg) and backend != 'dshow':
        raise ValueError('--size and --mjpg are only for the dshow backend')
    if args.obs and backend == 'dshow':
        raise ValueError('--obs is only for the 3DS/DS backends; add an HDMI capture board to OBS directly')
    if args.size is not None:
        width, _, height = args.size.partition('x')
        if not (width.isdigit() and height.isdigit() and int(width) > 0 and int(height) > 0):
            raise ValueError('Use --size WIDTHxHEIGHT, such as 1920x1080')
    if args.action == 'list':
        return subprocess.run([str(executable), 'list', backend], cwd=ROOT).returncode
    session = read_json(directory / 'process.json') if (directory / 'process.json').exists() else None
    session = session or {}
    pid = session.get('pid')
    active = running(pid, session.get('start_filetime'))
    if args.action == 'start':
        if not device:
            raise ValueError('capture start requires --device STABLE_ID or a matching profile capture.stable_id')
        if active:
            raise RuntimeError('This capture directory already has a running session; inspect status or stop it')
        legacy_pid = directory / 'process.pid'
        if not session and legacy_pid.exists() and running(int(legacy_pid.read_text().strip())):
            raise RuntimeError('Legacy capture process exists; stop it before upgrading this directory')
        directory.mkdir(parents=True, exist_ok=True)
        token = uuid.uuid4().hex
        command = [str(executable), 'stream', backend, str(directory), device, token]
        if args.save_interval_ms is not None:
            command += ['--save-interval-ms', str(args.save_interval_ms)]
        if args.size is not None:
            command += ['--size', args.size]
        if args.mjpg:
            command += ['--mjpg']
        if args.obs:
            # The OBS browser source opens obs.html here; it reads obs-live.json next to it.
            for name in ('obs.html', 'obs-audio.js'):
                shutil.copyfile(ROOT / 'obs' / name, directory / name)
            command += ['--obs', args.obs]
        out_path, err_path = directory / (token + '.stdout.log'), directory / (token + '.stderr.log')
        with out_path.open('w') as out, err_path.open('w') as err:
            process = subprocess.Popen(command, cwd=ROOT, stdout=out, stderr=err,
                                       creationflags=subprocess.CREATE_NO_WINDOW)
        deadline = time.monotonic() + 15
        try:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(err_path.read_text() or 'Capture exited before the first frame')
                status = read_json(directory / 'status.json') if (directory / 'status.json').exists() else None
                if status and status.get('session') == token:
                    started = {'started': True, 'pid': process.pid, 'session': token,
                               'backend': backend, 'device': device, 'directory': str(directory)}
                    if args.obs:
                        started['obs_page'] = str(directory / 'obs.html')
                    print(json.dumps(started))
                    return 0
                time.sleep(.15)
            raise RuntimeError('No first frame in 15 seconds')
        except BaseException:
            (directory / ('stop-' + token + '.request')).write_text('stop')
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                print('Graceful stop requested; worker still exiting. No force kill. Logs: ' + str(err_path), file=sys.stderr)
            raise
    if args.action == 'stop':
        if active:
            (directory / ('stop-' + session['session'] + '.request')).write_text('stop')
            deadline = time.monotonic() + 5
            while running(pid, session.get('start_filetime')) and time.monotonic() < deadline:
                time.sleep(.1)
            if running(pid, session.get('start_filetime')):
                raise RuntimeError('Graceful stop requested; process still exiting. No force kill performed.')
        print('Capture stopped (or no current session)'); return 0
    if args.action == 'status':
        frame = read_json(directory / 'status.json') if (directory / 'status.json').exists() else None
        print(json.dumps({'running': active, 'process': session or None, 'frame': frame,
                          'directory': str(directory)}, ensure_ascii=False)); return 0


def live_capture_session(directory):
    session = read_json(directory / 'process.json') if (directory / 'process.json').exists() else None
    if not session or not running(session.get('pid'), session.get('start_filetime')):
        raise RuntimeError('No active capture session; start capture before taking a snapshot')
    return session


def utc_seconds(text):
    """Seconds since the epoch for the worker's DateTime.UtcNow.ToString("o"), or None."""
    try:
        whole, _, fraction = text.rstrip('Z').partition('.')
        moment = datetime.datetime.strptime(whole, '%Y-%m-%dT%H:%M:%S').replace(tzinfo=datetime.timezone.utc)
        return moment.timestamp() + (float('0.' + fraction) if fraction else 0)
    except (AttributeError, ValueError):
        return None


def fresh_snapshot(directory, session, destination, not_before, sleep):
    """Copy the first published frame captured at or after not_before (epoch seconds)."""
    interval = session.get('save_interval_ms') or 150
    timeout = max(1.0, interval * 3 / 1000)
    deadline = time.monotonic() + timeout
    source = directory / 'latest.png'
    while True:
        status = read_json(directory / 'status.json')
        if status and status.get('session') == session.get('session'):
            captured = utc_seconds(status.get('captured_utc') or status.get('utc'))
            # status.json is written after latest.png, so the copied image is at least this frame.
            if captured is not None and captured >= not_before:
                try:
                    shutil.copyfile(source, destination)
                    return status, captured
                except (PermissionError, FileNotFoundError):
                    pass
        if time.monotonic() >= deadline:
            raise RuntimeError('No capture frame newer than the input arrived within %.1fs' % timeout)
        sleep(.01)


def snapshot(args):
    _, _, directory = capture_settings(args)
    destination = Path(args.output).resolve()
    source = directory / 'latest.png'
    session = live_capture_session(directory)
    for _ in range(20):
        try:
            status = read_json(directory / 'status.json')
            if not status or status.get('session') != session.get('session') or time.time() - source.stat().st_mtime > 3:
                raise RuntimeError('Capture frame is stale or from another session')
            shutil.copyfile(source, destination)
            print(str(destination)); return
        except (PermissionError, FileNotFoundError):
            time.sleep(.03)
    raise RuntimeError('Could not copy the current capture frame')


def add_input_identity(command):
    command.add_argument('--backend', choices=('qingpi', '3dscon', 'dsft', 'ns'))
    command.add_argument('--vid', type=lambda x: int(x, 0))
    command.add_argument('--pid', type=lambda x: int(x, 0))
    command.add_argument('--serial')
    command.add_argument('--firmware', choices=('modern', 'legacy'))
    command.add_argument('--revision', type=int, choices=(1, 2, 3), help='DS FT board revision')


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--profile', default=str(DEFAULT_PROFILE))
    sub = result.add_subparsers(dest='command', required=True)
    listing = sub.add_parser('devices', help='List USB serial metadata (or DS FT candidates) without opening devices')
    listing.add_argument('--backend', choices=('qingpi', '3dscon', 'dsft', 'ns'))
    listing.set_defaults(func=devices)
    press = sub.add_parser('press', help='Send one bounded button/dpad pulse')
    press.add_argument('button', choices=BUTTON_NAMES)
    release = sub.add_parser('release', help='Release buttons, dpad, sticks, and touch')
    touch = sub.add_parser('touch', help='Lower-screen touch x=1..320, y=1..240')
    circle = sub.add_parser('circle', aliases=['lstick'], help='Circle pad / left stick x/y=0..255, neutral=128')
    cstick = sub.add_parser('cstick', aliases=['rstick'], help='3DS-Con C-stick / NS right stick x/y=0..255, neutral=128')
    state = sub.add_parser('state', help='Send buttons, dpad, circle pad and touch together')
    state.add_argument('--buttons', nargs='*', choices=KEY_NAMES, default=[])
    state.add_argument('--dpad', choices=['neutral'] + list(HATS), default='neutral')
    state.add_argument('--circle', '--lstick', dest='circle', nargs=2, type=int, default=(128, 128), metavar=('X', 'Y'))
    state.add_argument('--cstick', '--rstick', dest='cstick', nargs=2, type=int, default=(128, 128), metavar=('X', 'Y'))
    state.add_argument('--touch', nargs=2, type=int, metavar=('X', 'Y'))
    sequence = sub.add_parser('sequence', help='Run 1..128 absolute states from JSON, total at most 30s')
    sequence.add_argument('file', help='Sequence JSON file, or - to read it from standard input')
    for command in (touch, circle, cstick):
        command.add_argument('x', type=int); command.add_argument('y', type=int)
    for command in (press, release, touch, circle, cstick, state, sequence):
        if command is not sequence:
            command.add_argument('--seconds', type=float, default=0 if command is release else .12)
        command.add_argument('--wait', type=float, default=.4)
        add_input_identity(command)
        command.add_argument('--dry-run', action='store_true', help='Print packets without enumerating/opening devices')
        command.add_argument('--snapshot', metavar='PNG',
                             help='After the input and --wait, copy the first capture frame taken after the wait')
        command.add_argument('--capture-backend', choices=BACKENDS, help='Capture backend for --snapshot')
        command.add_argument('--capture-dir', help='Capture session directory for --snapshot')
        command.set_defaults(func=input_pulse)
    stop = sub.add_parser('stop', help='Stop a running input command on this device and release all input')
    add_input_identity(stop)
    stop.add_argument('--timeout', type=float, default=3, help='Seconds to wait for the input command to release and exit')
    stop.set_defaults(func=stop_input)
    cap = sub.add_parser('capture', help='Manage independent video capture')
    cap.add_argument('action', choices=['list', 'start', 'stop', 'status'])
    cap.add_argument('--capture-exe', help='Capture worker path; default CaptureCli.exe')
    cap.add_argument('--save-interval-ms', type=int,
                     help='Publication interval 20..10000ms; default 150')
    cap.add_argument('--size', help='dshow only: request WIDTHxHEIGHT from the capture board')
    cap.add_argument('--mjpg', action='store_true', help='dshow only: request MJPG')
    cap.add_argument('--obs', nargs='?', const='both', choices=('both', 'top', 'bottom'),
                     help='3DS/DS only: also feed the screens (default both) and game audio to obs.html '
                          'in the capture directory, for an OBS browser source')
    cap.set_defaults(func=capture)
    snap = sub.add_parser('snapshot', help='Copy a recent combined screen frame')
    snap.add_argument('output'); snap.set_defaults(func=snapshot)
    for command in (cap, snap):
        command.add_argument('--backend', choices=BACKENDS)
        command.add_argument('--device', help='Exact StableId from capture list')
        command.add_argument('--capture-dir', help='Session directory; default is isolated by backend')
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    args.command = {'lstick': 'circle', 'rstick': 'cstick'}.get(args.command, args.command)
    try:
        return args.func(args) or 0
    except KeyboardInterrupt:
        print('Interrupted; any opened input port had all-input release attempted.', file=sys.stderr); return 130
    except Exception as error:
        print(type(error).__name__ + ': ' + str(error), file=sys.stderr); return 1


if __name__ == '__main__':
    sys.exit(main())
