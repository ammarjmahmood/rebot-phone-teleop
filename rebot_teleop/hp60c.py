import ctypes as ct
import threading
import time
import numpy as np
from rebot_teleop import config as C

USB_LISTENER = 0
MEDIA_DEPTH = 0
MEDIA_RGB = 1


class Frame(ct.Structure):
    _fields_ = [("type", ct.c_int), ("width", ct.c_uint), ("height", ct.c_uint), ("data", ct.c_void_p), ("size", ct.c_uint), ("bufferSize", ct.c_uint), ("frameId", ct.c_uint), ("ts", ct.c_ulonglong)]


class Data(ct.Structure):
    _fields_ = [("depthImg", Frame), ("rgbImg", Frame), ("irImg", Frame), ("pointCloud", Frame), ("yuyvImg", Frame), ("peakImg", Frame), ("mjpegImg", Frame)]


class UsbAttr(ct.Structure):
    _fields_ = [("vid", ct.c_int), ("pid", ct.c_int), ("serial", ct.c_char * 64), ("bnum", ct.c_int), ("dnum", ct.c_int), ("port_number", ct.c_int), ("port_numbers", ct.c_char * 64)]


class NetAttr(ct.Structure):
    _fields_ = [("ip_addr", ct.c_char * 64), ("port", ct.c_int), ("model", ct.c_int)]


class WinAttr(ct.Structure):
    _fields_ = [("vid", ct.c_int), ("pid", ct.c_int), ("deviceID", ct.c_int), ("symbol_link", ct.c_char * 64), ("location_path", ct.c_char * 64), ("serialPort", ct.c_char * 64)]


class AttrUnion(ct.Union):
    _fields_ = [("usbAttrs", UsbAttr), ("netAttrs", NetAttr), ("winAttrs", WinAttr)]


class CamAttr(ct.Structure):
    _fields_ = [("type", ct.c_int), ("attr", AttrUnion)]


class StreamParam(ct.Structure):
    _fields_ = [("width", ct.c_int), ("height", ct.c_int), ("fps", ct.c_int)]


class CamParameter(ct.Structure):
    _fields_ = [(name, ct.c_float) for name in ("fxir", "fyir", "cxir", "cyir", "fxrgb", "fyrgb", "cxrgb", "cyrgb", "R00", "R01", "R02", "R10", "R11", "R12", "R20", "R21", "R22", "T1", "T2", "T3", "K1ir", "K2ir", "K3ir", "P1ir", "P2ir", "K1rgb", "K2rgb", "K3rgb", "P1rgb", "P2rgb")]


LISTENER_FN = ct.CFUNCTYPE(None, ct.POINTER(CamAttr), ct.c_void_p)
STREAM_FN = ct.CFUNCTYPE(None, ct.c_void_p, ct.POINTER(Data), ct.c_void_p)


class ListenerCb(ct.Structure):
    _fields_ = [("onAttached", LISTENER_FN), ("onDetached", LISTENER_FN), ("privateData", ct.c_void_p)]


class StreamCb(ct.Structure):
    _fields_ = [("callback", STREAM_FN), ("privateData", ct.c_void_p)]


SYMBOLS = {
    "init": ("_Z11AS_SDK_Initv", [], ct.c_int),
    "deinit": ("_Z13AS_SDK_Deinitv", [], ct.c_int),
    "listen": ("_Z20AS_SDK_StartListenerRK20AS_LISTENER_CALLBACK16AS_LISTENER_TYPEb", [ct.POINTER(ListenerCb), ct.c_int, ct.c_bool], ct.c_int),
    "unlisten": ("_Z19AS_SDK_StopListenerv", [], ct.c_int),
    "create": ("_Z22AS_SDK_CreateCamHandleRPvP11AS_CAM_ATTR", [ct.POINTER(ct.c_void_p), ct.POINTER(CamAttr)], ct.c_int),
    "destroy": ("_Z23AS_SDK_DestoryCamHandlePv", [ct.c_void_p], ct.c_int),
    "model": ("_Z21AS_SDK_GetCameraModelPvR14CAMERA_MODEL_E", [ct.c_void_p, ct.POINTER(ct.c_int)], ct.c_int),
    "open": ("_Z17AS_SDK_OpenCameraPvPKc", [ct.c_void_p, ct.c_char_p], ct.c_int),
    "close": ("_Z18AS_SDK_CloseCameraPv", [ct.c_void_p], ct.c_int),
    "set_param": ("_Z21AS_SDK_SetStreamParamPv12MEDIA_TYPE_EPK14STREAM_PARAM_S", [ct.c_void_p, ct.c_int, ct.POINTER(StreamParam)], ct.c_int),
    "register": ("_Z29AS_SDK_RegisterStreamCallbackPvP17STREAM_CALLBACK_S", [ct.c_void_p, ct.POINTER(StreamCb)], ct.c_int),
    "start": ("_Z18AS_SDK_StartStreamPvi", [ct.c_void_p, ct.c_int], ct.c_int),
    "stop": ("_Z17AS_SDK_StopStreamPvi", [ct.c_void_p, ct.c_int], ct.c_int),
    "intrinsics": ("_Z22AS_SDK_GetCamParameterPvP13CAM_Parameter", [ct.c_void_p, ct.POINTER(CamParameter)], ct.c_int),
    "serial": ("_Z22AS_SDK_GetSerialNumberPvPci", [ct.c_void_p, ct.c_char_p, ct.c_int], ct.c_int),
}
MODELS = {9: "hp60c", 10: "hp60cn"}


class HP60C:
    def __init__(self, sdk=C.ANGSTROM_SDK, width=640, height=480, fps=25, rotate=0):
        self.sdk = sdk
        self.size = (width, height, fps)
        self.rotate = rotate
        self.lib = None
        self.fn = {}
        self.handle = None
        self.attached = threading.Event()
        self.pending = None
        self.lock = threading.Lock()
        self.bgr = None
        self.depth = None
        self.at = 0.0
        self.frames = 0
        self.jpeg = None
        self.jpeg_frame = -1
        self.intrinsics = None
        self.serial = None
        self.note = "starting"
        self.thread = None
        self.running = threading.Event()
        self._listener = ListenerCb(LISTENER_FN(self._on_attach), LISTENER_FN(self._on_detach), None)
        self._stream = StreamCb(STREAM_FN(self._on_frame), None)

    def _load(self):
        folder = self.sdk / "lib"
        for name in ("libjpeg.so", "libturbojpeg.so", "libzmq.so", "libasusb.so", "libasuvc.so", "libFilt.so", "libalg_kunlun.so", "libAngKondyorArith.so"):
            if (folder / name).exists():
                ct.CDLL(str(folder / name), mode=ct.RTLD_GLOBAL)
        self.lib = ct.CDLL(str(folder / "libAngstrongCameraSdk.so"), mode=ct.RTLD_GLOBAL)
        for key, (symbol, args, result) in SYMBOLS.items():
            function = getattr(self.lib, symbol)
            function.argtypes = args
            function.restype = result
            self.fn[key] = function

    def start(self):
        if self.thread is None:
            self.running.set()
            self.thread = threading.Thread(target=self._run, name="hp60c", daemon=True)
            self.thread.start()

    def close(self):
        self.running.clear()

    def _on_attach(self, attr, private):
        copy = CamAttr()
        ct.memmove(ct.byref(copy), attr, ct.sizeof(CamAttr))
        self.pending = copy
        self.attached.set()

    def _on_detach(self, attr, private):
        self.note = "unplugged"
        self.attached.clear()
        self.pending = None

    def _on_frame(self, camera, data, private):
        d = data.contents
        rgb, depth = d.rgbImg, d.depthImg
        bgr = None
        if rgb.size and rgb.data and rgb.size == rgb.width * rgb.height * 3:
            bgr = np.frombuffer(ct.string_at(rgb.data, rgb.size), dtype=np.uint8).reshape(rgb.height, rgb.width, 3)
        dep = None
        if depth.size and depth.data and depth.size == depth.width * depth.height * 2:
            dep = np.frombuffer(ct.string_at(depth.data, depth.size), dtype=np.uint16).reshape(depth.height, depth.width)
        if bgr is None and dep is None:
            return
        if self.rotate == 180:
            bgr = None if bgr is None else bgr[::-1, ::-1]
            dep = None if dep is None else dep[::-1, ::-1]
        with self.lock:
            if bgr is not None:
                self.bgr = bgr
                self.frames += 1
                self.at = time.monotonic()
            if dep is not None:
                self.depth = dep
        self.note = "streaming"

    def _open(self, attr):
        handle = ct.c_void_p()
        if self.fn["create"](ct.byref(handle), ct.byref(attr)) != 0 or not handle.value:
            raise RuntimeError("could not create a camera handle")
        model = ct.c_int(0)
        self.fn["model"](handle, ct.byref(model))
        name = MODELS.get(model.value, "hp60c")
        config = next(iter(sorted((self.sdk / "config").glob(f"{name}_v*.json"))), None)
        if self.fn["open"](handle, str(config).encode() if config else None) != 0:
            self.fn["destroy"](handle)
            raise RuntimeError("could not open the camera")
        width, height, fps = self.size
        for media in (MEDIA_DEPTH, MEDIA_RGB):
            param = StreamParam(width, height, fps)
            self.fn["set_param"](handle, media, ct.byref(param))
        self.fn["register"](handle, ct.byref(self._stream))
        if self.fn["start"](handle, 0) != 0:
            raise RuntimeError("could not start streaming")
        params = CamParameter()
        if self.fn["intrinsics"](handle, ct.byref(params)) == 0:
            self.intrinsics = {"fx": params.fxrgb, "fy": params.fyrgb, "cx": params.cxrgb, "cy": params.cyrgb}
        buffer = ct.create_string_buffer(64)
        if self.fn["serial"](handle, buffer, 64) == 0:
            self.serial = buffer.value.decode(errors="ignore")
        self.handle = handle

    def _shutdown_handle(self):
        if self.handle is not None:
            for key in ("stop", "close"):
                try:
                    self.fn[key](self.handle) if key == "close" else self.fn[key](self.handle, 0)
                except Exception:
                    pass
            try:
                self.fn["destroy"](self.handle)
            except Exception:
                pass
            self.handle = None

    def _run(self):
        try:
            self._load()
            self.fn["init"]()
            self.fn["listen"](ct.byref(self._listener), USB_LISTENER, True)
        except Exception as error:
            self.note = f"Angstrong SDK failed to load: {error}"
            return
        self.note = "waiting for the HP60C to be plugged in"
        while self.running.is_set():
            if self.handle is None:
                if not self.attached.wait(1.0):
                    continue
                attr = self.pending
                if attr is None:
                    continue
                try:
                    time.sleep(0.5)
                    self._open(attr)
                    self.note = "starting stream"
                except Exception as error:
                    self.note = f"HP60C: {error}"
                    self._shutdown_handle()
                    time.sleep(2.0)
                continue
            time.sleep(0.5)
            stale = time.monotonic() - self.at if self.at else 0.0
            if not self.attached.is_set() or (self.frames and stale > 5.0):
                self.note = "no frames; reopening" if self.attached.is_set() else "unplugged"
                self._shutdown_handle()
                self.at = 0.0
                self.frames = 0
        self._shutdown_handle()

    def frame(self):
        with self.lock:
            return self.bgr, self.depth, self.frames

    def jpeg_bytes(self, quality=75):
        import cv2
        with self.lock:
            bgr, count = self.bgr, self.frames
        if bgr is None:
            return None, 0
        if count != self.jpeg_frame:
            ok, data = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, quality])
            if ok:
                self.jpeg, self.jpeg_frame = data.tobytes(), count
        return self.jpeg, self.jpeg_frame

    def status(self):
        return {"kind": "hp60c", "note": self.note, "frames": self.frames, "age_ms": round((time.monotonic() - self.at) * 1000) if self.at else None, "serial": self.serial, "intrinsics": self.intrinsics}
