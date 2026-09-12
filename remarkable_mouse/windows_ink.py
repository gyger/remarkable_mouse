import ctypes
import logging
import struct
from ctypes import wintypes
from socket import timeout as TimeoutError

from .codes import codes
from .common import get_monitor, log_event

logging.basicConfig(format='%(message)s')
log = logging.getLogger('remouse')

PT_PEN = 3
POINTER_FEEDBACK_DEFAULT = 1

POINTER_FLAG_INRANGE = 0x00000002
POINTER_FLAG_INCONTACT = 0x00000004
POINTER_FLAG_DOWN = 0x00010000
POINTER_FLAG_UPDATE = 0x00020000
POINTER_FLAG_UP = 0x00040000

PEN_FLAG_BARREL = 0x00000001
PEN_FLAG_INVERTED = 0x00000002
PEN_FLAG_ERASER = 0x00000004

PEN_MASK_PRESSURE = 0x00000001
PEN_MASK_TILT_X = 0x00000004
PEN_MASK_TILT_Y = 0x00000008

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79


class POINTER_INFO(ctypes.Structure):
    _fields_ = [
        ('pointerType', wintypes.DWORD),
        ('pointerId', wintypes.UINT),
        ('frameId', wintypes.UINT),
        ('pointerFlags', wintypes.DWORD),
        ('sourceDevice', wintypes.HANDLE),
        ('hwndTarget', wintypes.HWND),
        ('ptPixelLocation', wintypes.POINT),
        ('ptHimetricLocation', wintypes.POINT),
        ('ptPixelLocationRaw', wintypes.POINT),
        ('ptHimetricLocationRaw', wintypes.POINT),
        ('dwTime', wintypes.DWORD),
        ('historyCount', wintypes.UINT),
        ('InputData', ctypes.c_int),
        ('dwKeyStates', wintypes.DWORD),
        ('PerformanceCount', ctypes.c_uint64),
        ('ButtonChangeType', wintypes.DWORD),
    ]


class POINTER_PEN_INFO(ctypes.Structure):
    _fields_ = [
        ('pointerInfo', POINTER_INFO),
        ('penFlags', wintypes.DWORD),
        ('penMask', wintypes.DWORD),
        ('pressure', wintypes.UINT),
        ('rotation', wintypes.UINT),
        ('tiltX', ctypes.c_int),
        ('tiltY', ctypes.c_int),
    ]


class POINTER_TOUCH_INFO(ctypes.Structure):
    _fields_ = [
        ('pointerInfo', POINTER_INFO),
        ('touchFlags', wintypes.DWORD),
        ('touchMask', wintypes.DWORD),
        ('rcContact', wintypes.RECT),
        ('rcContactRaw', wintypes.RECT),
        ('orientation', wintypes.UINT),
        ('pressure', wintypes.UINT),
    ]


class POINTER_TYPE_UNION(ctypes.Union):
    _fields_ = [
        ('penInfo', POINTER_PEN_INFO),
        ('touchInfo', POINTER_TOUCH_INFO),
    ]


class POINTER_TYPE_INFO(ctypes.Structure):
    _anonymous_ = ('pointer',)
    _fields_ = [
        ('type', wintypes.DWORD),
        ('pointer', POINTER_TYPE_UNION),
    ]


class PenDevice:
    def __init__(self):
        if ctypes.sizeof(POINTER_TYPE_INFO) < ctypes.sizeof(POINTER_PEN_INFO) + ctypes.sizeof(wintypes.DWORD):
            raise RuntimeError('Unexpected POINTER_TYPE_INFO layout')

        user32 = ctypes.WinDLL('user32', use_last_error=True)
        try:
            user32.CreateSyntheticPointerDevice
            user32.InjectSyntheticPointerInput
        except AttributeError as exc:
            raise RuntimeError(
                'Windows Ink pen injection requires Windows 10 version 1809 or newer'
            ) from exc

        user32.CreateSyntheticPointerDevice.argtypes = [
            wintypes.DWORD,
            ctypes.c_ulong,
            wintypes.DWORD,
        ]
        user32.CreateSyntheticPointerDevice.restype = wintypes.HANDLE
        user32.InjectSyntheticPointerInput.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(POINTER_TYPE_INFO),
            wintypes.UINT,
        ]
        user32.InjectSyntheticPointerInput.restype = wintypes.BOOL
        user32.DestroySyntheticPointerDevice.argtypes = [wintypes.HANDLE]
        user32.DestroySyntheticPointerDevice.restype = None
        user32.GetSystemMetrics.argtypes = [ctypes.c_int]
        user32.GetSystemMetrics.restype = ctypes.c_int

        self.user32 = user32
        self.handle = user32.CreateSyntheticPointerDevice(
            PT_PEN,
            1,
            POINTER_FEEDBACK_DEFAULT,
        )
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())

        self.virtual_x = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        self.virtual_y = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        self.virtual_width = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
        self.virtual_height = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)

        self.pointer_id = 1
        self.in_range = False
        self.touching = False
        self.last_x = 0
        self.last_y = 0
        self.last_tilt_x = 0
        self.last_tilt_y = 0
        self.last_eraser = False
        self.last_barrel = False

    def close(self):
        if self.handle:
            try:
                if self.touching or self.in_range:
                    self._inject_current_state(
                        self.last_x,
                        self.last_y,
                        pressure=0,
                        tilt_x=self.last_tilt_x,
                        tilt_y=self.last_tilt_y,
                        in_range=False,
                        touching=False,
                        eraser=self.last_eraser,
                        barrel=self.last_barrel,
                    )
            finally:
                self.user32.DestroySyntheticPointerDevice(self.handle)
                self.handle = None

    def _clamp_virtual(self, x, y):
        # Synthetic pen coordinates are relative to the virtual-screen origin.
        rel_x = int(x) - self.virtual_x
        rel_y = int(y) - self.virtual_y
        if self.virtual_width > 0:
            rel_x = min(max(rel_x, 0), self.virtual_width - 1)
        if self.virtual_height > 0:
            rel_y = min(max(rel_y, 0), self.virtual_height - 1)
        return rel_x, rel_y

    def _make_pen_info(self, *, x, y, pressure, tilt_x, tilt_y, flags, eraser, barrel):
        rel_x, rel_y = self._clamp_virtual(x, y)

        info = POINTER_TYPE_INFO()
        info.type = PT_PEN
        info.penInfo.pointerInfo.pointerType = PT_PEN
        info.penInfo.pointerInfo.pointerId = self.pointer_id
        info.penInfo.pointerInfo.pointerFlags = flags
        info.penInfo.pointerInfo.ptPixelLocation = wintypes.POINT(rel_x, rel_y)
        info.penInfo.pointerInfo.ptPixelLocationRaw = wintypes.POINT(rel_x, rel_y)
        info.penInfo.pointerInfo.historyCount = 1

        pen_flags = 0
        if eraser:
            pen_flags |= PEN_FLAG_INVERTED | PEN_FLAG_ERASER
        if barrel:
            pen_flags |= PEN_FLAG_BARREL

        pen_mask = PEN_MASK_PRESSURE
        if tilt_x is not None:
            pen_mask |= PEN_MASK_TILT_X
        if tilt_y is not None:
            pen_mask |= PEN_MASK_TILT_Y

        info.penInfo.penFlags = pen_flags
        info.penInfo.penMask = pen_mask
        info.penInfo.pressure = max(0, min(1024, int(pressure)))
        info.penInfo.tiltX = 0 if tilt_x is None else max(-90, min(90, int(tilt_x)))
        info.penInfo.tiltY = 0 if tilt_y is None else max(-90, min(90, int(tilt_y)))
        return info

    def _inject(self, *, x, y, pressure, tilt_x, tilt_y, flags, eraser, barrel):
        info = self._make_pen_info(
            x=x,
            y=y,
            pressure=pressure,
            tilt_x=tilt_x,
            tilt_y=tilt_y,
            flags=flags,
            eraser=eraser,
            barrel=barrel,
        )
        if not self.user32.InjectSyntheticPointerInput(self.handle, ctypes.byref(info), 1):
            raise ctypes.WinError(ctypes.get_last_error())

    def _inject_current_state(self, x, y, *, pressure, tilt_x, tilt_y, in_range, touching, eraser, barrel):
        if touching and not in_range:
            in_range = True

        if touching:
            if not self.in_range:
                self._inject(
                    x=x,
                    y=y,
                    pressure=0,
                    tilt_x=tilt_x,
                    tilt_y=tilt_y,
                    flags=POINTER_FLAG_UPDATE | POINTER_FLAG_INRANGE,
                    eraser=eraser,
                    barrel=barrel,
                )
            flags = POINTER_FLAG_DOWN | POINTER_FLAG_INRANGE | POINTER_FLAG_INCONTACT
            if self.touching:
                flags = POINTER_FLAG_UPDATE | POINTER_FLAG_INRANGE | POINTER_FLAG_INCONTACT
            self._inject(
                x=x,
                y=y,
                pressure=pressure,
                tilt_x=tilt_x,
                tilt_y=tilt_y,
                flags=flags,
                eraser=eraser,
                barrel=barrel,
            )
        elif in_range:
            flags = POINTER_FLAG_UPDATE | POINTER_FLAG_INRANGE
            if self.touching:
                flags = POINTER_FLAG_UP | POINTER_FLAG_INRANGE
            self._inject(
                x=x,
                y=y,
                pressure=0,
                tilt_x=tilt_x,
                tilt_y=tilt_y,
                flags=flags,
                eraser=eraser,
                barrel=barrel,
            )
        elif self.touching:
            self._inject(
                x=x,
                y=y,
                pressure=0,
                tilt_x=tilt_x,
                tilt_y=tilt_y,
                flags=POINTER_FLAG_UP,
                eraser=eraser,
                barrel=barrel,
            )
        elif self.in_range:
            self._inject(
                x=x,
                y=y,
                pressure=0,
                tilt_x=tilt_x,
                tilt_y=tilt_y,
                flags=POINTER_FLAG_UPDATE,
                eraser=eraser,
                barrel=barrel,
            )

        self.in_range = in_range
        self.touching = touching
        self.last_x = x
        self.last_y = y
        self.last_tilt_x = 0 if tilt_x is None else tilt_x
        self.last_tilt_y = 0 if tilt_y is None else tilt_y
        self.last_eraser = eraser
        self.last_barrel = barrel


def normalize_pressure(value, maximum):
    if maximum <= 0:
        return 0
    return round(max(0, min(value, maximum)) * 1024 / maximum)


def normalize_tilt(value, minimum, maximum):
    if minimum is None or maximum is None or minimum >= maximum:
        return None
    scaled = (value - minimum) * 180 / (maximum - minimum) - 90
    return round(max(-90, min(90, scaled)))


def read_tablet(rm, *, orientation, monitor_num, region, threshold, mode):
    del threshold
    monitor, _ = get_monitor(region, monitor_num, orientation)
    log.debug('Chose monitor: {}'.format(monitor))

    pen = PenDevice()

    x = y = 0
    pressure = 0
    tilt_x = tilt_y = 0
    tool_pen = False
    tool_rubber = False
    touching = False
    barrel = False

    stream = rm.pen
    try:
        while True:
            try:
                data = stream.read(struct.calcsize(rm.e_format))
            except TimeoutError:
                continue

            e_time, e_millis, e_type, e_code, e_value = struct.unpack(rm.e_format, data)

            if log.level == logging.DEBUG:
                log_event(e_time, e_millis, e_type, e_code, e_value)

            try:
                event_name = codes[e_type][e_code]
            except KeyError:
                log.debug(f'Invalid evdev event: type:{e_type} code:{e_code}')
                continue

            if event_name == 'ABS_X':
                x = e_value
            elif event_name == 'ABS_Y':
                y = e_value
            elif event_name == 'ABS_PRESSURE':
                pressure = normalize_pressure(e_value, rm.pen_pressure.max)
            elif event_name == 'ABS_TILT_X':
                tilt_x = normalize_tilt(e_value, rm.pen_tilt_x.min, rm.pen_tilt_x.max)
            elif event_name == 'ABS_TILT_Y':
                tilt_y = normalize_tilt(e_value, rm.pen_tilt_y.min, rm.pen_tilt_y.max)
            elif event_name == 'BTN_TOUCH':
                touching = e_value == 1
            elif event_name == 'BTN_TOOL_PEN':
                tool_pen = e_value == 1
            elif event_name == 'BTN_TOOL_RUBBER':
                tool_rubber = e_value == 1
            elif event_name == 'BTN_STYLUS':
                barrel = e_value == 1
            elif event_name == 'SYN_REPORT':
                mapped_x, mapped_y = rm.remap(
                    x, y,
                    rm.pen_x.max, rm.pen_y.max,
                    monitor.width, monitor.height,
                    mode, orientation,
                )
                pen._inject_current_state(
                    int(round(monitor.x + mapped_x)),
                    int(round(monitor.y + mapped_y)),
                    pressure=pressure,
                    tilt_x=tilt_x,
                    tilt_y=tilt_y,
                    in_range=tool_pen or tool_rubber or touching,
                    touching=touching,
                    eraser=tool_rubber,
                    barrel=barrel,
                )
    finally:
        pen.close()
