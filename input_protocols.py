"""Pure packet encoders for the supported input adapters."""
import math

QINGPI_BUTTONS = {'y': 1, 'b': 2, 'a': 4, 'x': 8, 'l': 16, 'r': 32,
                  'zl': 64, 'zr': 128, 'select': 256, 'start': 512, 'home': 4096}
CON_BUTTONS = {'a': 1, 'b': 2, 'x': 4, 'y': 8, 'l': 16, 'r': 32,
               'home': 64, 'start': 128, 'select': 256, 'zl': 1024, 'zr': 2048}
HATS = {'up': 0, 'up-right': 1, 'right': 2, 'down-right': 3,
        'down': 4, 'down-left': 5, 'left': 6, 'up-left': 7}
CON_HATS = {'up': 8, 'up-right': 12, 'right': 4, 'down-right': 6,
            'down': 2, 'down-left': 3, 'left': 1, 'up-left': 9}
# DS FT GPIO pins: C0..C7 are bits 0..7, D0..D7 are bits 8..15. B/X/Y depend on the board revision.
DSFT_PINS = {'left': 0, 'down': 1, 'right': 2, 'up': 3, 'a': 4,
             'l': 8, 'r': 9, 'select': 10, 'start': 11, 'power': 12}
DSFT_DIRECTIONS = {'up': ('up',), 'up-right': ('up', 'right'), 'right': ('right',),
                   'down-right': ('down', 'right'), 'down': ('down',), 'down-left': ('down', 'left'),
                   'left': ('left',), 'up-left': ('up', 'left')}
DSFT_BUTTONS = ('a', 'b', 'x', 'y', 'l', 'r', 'select', 'start', 'power')
# Switch and GC adapters both use the NS packet; plus/minus share the start/select bits.
NS_BUTTONS = {'y': 1, 'b': 2, 'a': 4, 'x': 8, 'l': 16, 'r': 32, 'zl': 64, 'zr': 128,
              'minus': 256, 'select': 256, 'plus': 512, 'start': 512,
              'lclick': 1024, 'rclick': 2048, 'home': 4096, 'capture': 8192}
KEY_NAMES = list(CON_BUTTONS) + ['power', 'plus', 'minus', 'lclick', 'rclick', 'capture']
AVAILABLE = {'qingpi': QINGPI_BUTTONS, '3dscon': CON_BUTTONS, 'dsft': DSFT_BUTTONS, 'ns': NS_BUTTONS}
BUTTON_NAMES = ['neutral'] + KEY_NAMES + list(HATS)
MAX_HOLD_SECONDS = 5


class ReleaseNotConfirmed(RuntimeError):
    pass


def validate_timing(seconds, wait):
    if not math.isfinite(seconds) or not 0 <= seconds <= MAX_HOLD_SECONDS:
        raise ValueError('Hold must be finite and 0..5 seconds')
    if not math.isfinite(wait) or not 0 <= wait <= 30:
        raise ValueError('Wait must be finite and 0..30 seconds')


def circle_axis(value, firmware):
    if not 0 <= value <= 255:
        raise ValueError('Axis must be 0..255')
    if firmware == 'modern':
        return 255 - value
    if firmware == 'legacy':
        encoded = value if value >= 128 else 127 - value
        return encoded or 128
    raise ValueError('3DS-Con requires explicit --firmware modern|legacy (or profile firmware)')


def dsft_pin(name, revision):
    if name == 'b':
        return 14 if revision == 2 else 5
    if name == 'x':
        return 15 if revision == 2 else 6
    if name == 'y':
        return 7 if revision == 1 else 13
    return DSFT_PINS[name]


def encode(backend, firmware=None, button='neutral', touch=None,
           circle=(128, 128), cstick=(128, 128), buttons=None, dpad=None, revision=None):
    if button not in BUTTON_NAMES:
        raise ValueError('Unsupported button: ' + button)
    if len(circle) != 2 or len(cstick) != 2 or any(type(v) is not int or not 0 <= v <= 255 for v in (*circle, *cstick)):
        raise ValueError('Axis values must be integers in 0..255')
    if touch is not None and tuple(touch) == (0, 0):
        touch = None
    if touch is not None and (len(touch) != 2 or any(type(v) is not int for v in touch)
                             or not (1 <= touch[0] <= 320 and 1 <= touch[1] <= 240)):
        raise ValueError('Touch coordinates must be x=1..320, y=1..240')
    names = ([button] if button in KEY_NAMES else []) + list(buttons or [])
    if any(name not in KEY_NAMES for name in names):
        raise ValueError('buttons must contain only button names, not dpad directions')
    direction = dpad if dpad is not None else (button if button in HATS else 'neutral')
    if direction != 'neutral' and direction not in HATS:
        raise ValueError('Unsupported dpad direction: ' + str(direction))
    for name in names:
        if backend in AVAILABLE and name not in AVAILABLE[backend]:
            raise ValueError(name + ' is not available on the ' + backend + ' backend')
    if backend == 'dsft':
        if type(revision) is not int or revision not in (1, 2, 3):
            raise ValueError('DS FT requires explicit --revision 1|2|3 (or profile revision)')
        if touch is not None or tuple(circle) != (128, 128) or tuple(cstick) != (128, 128):
            raise ValueError('DS FT supports buttons and the dpad only; no touch or sticks')
        pressed = 0
        for name in names:
            pressed |= 1 << dsft_pin(name, revision)
        for part in DSFT_DIRECTIONS.get(direction, ()):
            pressed |= 1 << DSFT_PINS[part]
        c, d = pressed & 255, pressed >> 8
        # A pressed pin is an output driven low; a released pin stays an input.
        return [bytes([0x82, ~c & 255, c, 0x80, ~d & 255, d])]
    if backend == 'ns':
        if touch is not None:
            raise ValueError('NS has no touch screen')
        bits = 0
        for name in names:
            bits |= NS_BUTTONS[name]
        # circle is the left stick and cstick the right stick; the last three bytes are keyboard input.
        return [bytes([0xAB, bits & 255, bits >> 8, HATS.get(direction, 8), *circle, *cstick, 0, 0, 0])]
    x, y = touch or (0, 0)
    if backend == 'qingpi':
        if tuple(cstick) != (128, 128):
            raise ValueError('Current Qingpi protocol does not support the C-stick')
        bits = 0
        for name in names:
            bits |= QINGPI_BUTTONS[name]
        return [bytes([0xAB, bits & 255, bits >> 8, HATS.get(direction, 8),
                       *circle, 128, 128, x & 255, x >> 8, y])]
    if backend == '3dscon':
        bits = 0
        for name in names:
            bits |= CON_BUTTONS[name]
        return [bytes([0xA1, ((bits & 15) << 4) | CON_HATS.get(direction, 0),
                       (bits >> 4) & 255, 0xA2,
                       circle_axis(circle[0], firmware), circle_axis(circle[1], firmware)]),
                bytes([0xA4, (cstick[0] - 128) & 255, (cstick[1] - 128) & 255]),
                bytes([0xB2, int(touch is not None), x >> 8, x & 255, y])]
    raise ValueError('Unknown input backend: ' + str(backend))


def release_packets(backend, firmware=None, revision=None):
    packets = encode(backend, firmware, revision=revision)
    if backend == '3dscon':
        packets.append(packets[-1])  # The touch release is sent twice with a 1ms gap.
    return packets


def write_packets(port, packets, sleep, best_effort=False):
    errors = []
    for index, data in enumerate(packets):
        try:
            if index and data == bytes([0xB2, 0, 0, 0, 0]) == packets[index - 1]:
                sleep(.001)
            count = port.write(data)
            if count != len(data):
                raise OSError('Incomplete serial write: %s/%s bytes' % (count, len(data)))
            port.flush()
        except (Exception, KeyboardInterrupt) as error:
            if not best_effort:
                raise
            errors.append(error)
    if errors:
        raise ReleaseNotConfirmed('All-input release was attempted but not confirmed: ' + str(errors[0]))


def perform_pulse(port, active, release, seconds, sleep):
    perform_sequence(port, [(active, seconds)], release, sleep)


def perform_sequence(port, steps, release, sleep):
    """Apply absolute states without intermediate neutral, then always release."""
    try:
        write_packets(port, release, sleep)
        sleep(.06)
        for packets, seconds in steps:
            write_packets(port, packets, sleep)
            sleep(seconds)
    finally:
        write_packets(port, release, sleep, best_effort=True)
        sleep(.08)

