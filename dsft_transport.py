"""DS FT (FT232H MPSSE GPIO) transport: open one exact device, verify it, then drive only pressed pins low."""
import ctypes
from pathlib import Path

from input_protocols import encode

FT232H = 8
CAPTURE_DESCRIPTIONS = ('NDS.1', 'NDS.2')


class DsFtNative:
    def __init__(self, path=None):
        self.dll = ctypes.WinDLL(str(path or Path(__file__).resolve().parent / 'ftd2xx.dll'))

    def check(self, status, operation):
        if status != 0:
            raise OSError('D2XX failed to %s (status %d)' % (operation, status))

    def enumerate(self):
        count = ctypes.c_uint32()
        self.check(self.dll.FT_CreateDeviceInfoList(ctypes.byref(count)), 'enumerate devices')
        result = []
        for index in range(count.value):
            flags, kind, ident, location = (ctypes.c_uint32() for _ in range(4))
            serial, description = ctypes.create_string_buffer(16), ctypes.create_string_buffer(64)
            handle = ctypes.c_void_p()
            self.check(self.dll.FT_GetDeviceInfoDetail(
                ctypes.c_uint32(index), ctypes.byref(flags), ctypes.byref(kind), ctypes.byref(ident),
                ctypes.byref(location), serial, description, ctypes.byref(handle)), 'read device identity')
            result.append({'serial': serial.value.decode('ascii', 'replace'),
                           'description': description.value.decode('ascii', 'replace'),
                           'type': kind.value, 'id': ident.value, 'open': bool(flags.value & 1)})
        return result

    def open(self, serial):
        handle = ctypes.c_void_p()
        status = self.dll.FT_OpenEx(ctypes.c_char_p(serial.encode('ascii')), ctypes.c_uint32(1), ctypes.byref(handle))
        if status != 0:
            if handle.value:
                self.dll.FT_Close(handle)
            self.check(status, 'open selected serial')
        return handle.value

    def identity(self, handle):
        kind, ident = ctypes.c_uint32(), ctypes.c_uint32()
        serial, description = ctypes.create_string_buffer(16), ctypes.create_string_buffer(64)
        self.check(self.dll.FT_GetDeviceInfo(ctypes.c_void_p(handle), ctypes.byref(kind), ctypes.byref(ident),
                                             serial, description, None), 'verify opened device')
        return {'serial': serial.value.decode('ascii', 'replace'),
                'description': description.value.decode('ascii', 'replace'),
                'type': kind.value, 'id': ident.value}

    def reset(self, handle):
        self.check(self.dll.FT_ResetDevice(ctypes.c_void_p(handle)), 'reset selected device')

    def timeouts(self, handle, read, write):
        self.check(self.dll.FT_SetTimeouts(ctypes.c_void_p(handle), ctypes.c_uint32(read), ctypes.c_uint32(write)),
                   'set timeouts')

    def bit_mode(self, handle, mask, mode):
        self.check(self.dll.FT_SetBitMode(ctypes.c_void_p(handle), ctypes.c_ubyte(mask), ctypes.c_ubyte(mode)),
                   'set GPIO mode')

    def latency(self, handle, milliseconds):
        self.check(self.dll.FT_SetLatencyTimer(ctypes.c_void_p(handle), ctypes.c_ubyte(milliseconds)),
                   'set latency')

    def write(self, handle, data):
        written = ctypes.c_uint32()
        self.check(self.dll.FT_Write(ctypes.c_void_p(handle), ctypes.c_char_p(bytes(data)),
                                     ctypes.c_uint32(len(data)), ctypes.byref(written)), 'write GPIO state')
        return written.value

    def close(self, handle):
        self.check(self.dll.FT_Close(ctypes.c_void_p(handle)), 'close selected device')


def is_capture(description):
    return description.strip().upper() in CAPTURE_DESCRIPTIONS


def candidates(devices):
    """FT232H with a unique serial that is not a known DS capture board. Listing is not identification."""
    counts = {}
    for device in devices:
        if device['serial'].strip():
            counts[device['serial']] = counts.get(device['serial'], 0) + 1
    return [device for device in devices if device['type'] == FT232H and device['serial'].strip()
            and counts[device['serial']] == 1 and not is_capture(device['description'])]


class DsFtPort:
    """Port-like object for perform_sequence. Opening forces neutral; closing releases before closing."""

    def __init__(self, serial, revision, native=None):
        if not serial or not str(serial).strip():
            raise ValueError('DS FT requires the exact FT232H serial in the profile or --serial')
        self.neutral = encode('dsft', revision=revision)[0]
        self.native = native or DsFtNative()
        self.serial = serial
        self.handle = None
        self.mpsse = False

    def __enter__(self):
        matches = [device for device in self.native.enumerate() if device['serial'] == self.serial]
        if len(matches) != 1 or matches[0]['type'] != FT232H or matches[0]['open'] or is_capture(matches[0]['description']):
            raise OSError('The selected serial must identify one available FT232H that is not a known capture device')
        selected = matches[0]
        try:
            self.handle = self.native.open(self.serial)
            if not self.handle:
                raise OSError('D2XX returned an empty DS FT handle')
            opened = self.native.identity(self.handle)
            if (opened['type'] != FT232H or opened['id'] != selected['id'] or opened['serial'] != self.serial
                    or is_capture(opened['description']) or opened['description'] != selected['description']):
                raise OSError('The opened FTDI device does not match the selected DS FT device')
            self.native.reset(self.handle)
            self.native.timeouts(self.handle, 250, 250)
            self.native.bit_mode(self.handle, 0, 0)
            # MPSSE with all pins as inputs; only pressed buttons become outputs driven low.
            self.native.bit_mode(self.handle, 0, 2)
            self.mpsse = True
            self.write(self.neutral)
            self.native.latency(self.handle, 2)
        except BaseException:
            self.close()
            raise
        return self

    def write(self, data):
        return self.native.write(self.handle, data)

    def flush(self):
        pass

    def close(self):
        handle, self.handle = self.handle, None
        mpsse, self.mpsse = self.mpsse, False
        if not handle:
            return
        # Before MPSSE a write would go out as UART data on the L pin, so release only after it.
        if mpsse:
            try:
                self.native.write(handle, self.neutral)
            except Exception:
                pass
        try:
            self.native.close(handle)
        except Exception:
            pass

    def __exit__(self, *exc):
        self.close()
