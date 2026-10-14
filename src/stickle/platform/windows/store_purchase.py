"""The Microsoft Store's purchase dialog for one of Stickle's add-ons.

Only the copy installed from the Store uses it, for "Support Stickle": the
add-ons are support with nothing in return, so the result is only used to say
thanks there and then. Nothing about it is kept, and no purchase history or
license is ever asked for.

Called through COM with ctypes: StoreContext from Windows.Services.Store, told
which window the dialog belongs to (IInitializeWithWindow, which a desktop app
must do), then RequestPurchaseAsync. The answer comes later; the caller polls
it from the interface's thread, which keeps processing messages meanwhile, as
the dialog needs.
"""

import ctypes
import enum
import sys
from collections.abc import Callable
from ctypes import POINTER, byref, c_int, c_uint, c_void_p, wintypes
from typing import Any

assert sys.platform == "win32"  # also tells the type checker the rest is Windows only


class Outcome(enum.Enum):
    """StorePurchaseStatus, and what went wrong before the Store could answer."""

    SUCCEEDED = 0
    ALREADY_PURCHASED = 1
    NOT_PURCHASED = 2  # the user closed it, or did not pay
    NETWORK_ERROR = 3
    SERVER_ERROR = 4
    FAILED = -1  # the dialog could not be shown, or the call itself failed


class _GUID(ctypes.Structure):
    _fields_ = [
        ("data1", c_uint),
        ("data2", ctypes.c_ushort),
        ("data3", ctypes.c_ushort),
        ("data4", ctypes.c_ubyte * 8),
    ]


def _guid(text: str) -> _GUID:
    value = _GUID()
    ctypes.oledll.ole32.CLSIDFromString(ctypes.c_wchar_p("{" + text + "}"), byref(value))
    return value


IID_STORE_CONTEXT_STATICS = _guid("9c06ee5f-15c0-4e72-9330-d6191cebd19c")
IID_INITIALIZE_WITH_WINDOW = _guid("3e68d4bd-7135-4d10-8018-9fb6d9f33fa1")
IID_ASYNC_INFO = _guid("00000036-0000-0000-c000-000000000046")
STORE_CONTEXT = "Windows.Services.Store.StoreContext"

# Places in the interfaces' method tables (windows.services.store.h): IUnknown
# takes 0-2 and IInspectable 3-5.
RELEASE = 2
QUERY_INTERFACE = 0
STATICS_GET_DEFAULT = 6
CONTEXT_REQUEST_PURCHASE = 21
INITIALIZE = 3
ASYNC_STATUS = 7
ASYNC_ERROR_CODE = 8
OPERATION_GET_RESULTS = 8
RESULT_STATUS = 6

STARTED, COMPLETED = 0, 1


def _method(pointer: c_void_p, index: int, *argtypes: Any) -> Callable[..., Any]:
    table = ctypes.cast(pointer, POINTER(POINTER(c_void_p))).contents
    prototype = ctypes.WINFUNCTYPE(ctypes.HRESULT, c_void_p, *argtypes)
    function = prototype(table[index])
    return lambda *args: function(pointer, *args)


def _release(pointer: c_void_p) -> None:
    if pointer:
        table = ctypes.cast(pointer, POINTER(POINTER(c_void_p))).contents
        ctypes.WINFUNCTYPE(c_uint, c_void_p)(table[RELEASE])(pointer)


def _query(pointer: c_void_p, iid: _GUID) -> c_void_p:
    out = c_void_p()
    _method(pointer, QUERY_INTERFACE, POINTER(_GUID), POINTER(c_void_p))(byref(iid), byref(out))
    return out


class _HString:
    """A Windows Runtime string for the length of a call."""

    def __init__(self, text: str) -> None:
        self._combase = ctypes.WinDLL("combase")
        self.handle = c_void_p()
        self._combase.WindowsCreateString(
            ctypes.c_wchar_p(text), c_uint(len(text)), byref(self.handle)
        )

    def delete(self) -> None:
        self._combase.WindowsDeleteString(self.handle)


class Purchase:
    """One purchase dialog, from the moment it is asked for until it is answered."""

    def __init__(self, window: int, store_id: str) -> None:
        self._operation = c_void_p()
        self._info = c_void_p()
        combase = ctypes.WinDLL("combase")
        name = _HString(STORE_CONTEXT)
        statics = c_void_p()
        context = c_void_p()
        owner = c_void_p()
        item = _HString(store_id)
        try:
            result = combase.RoGetActivationFactory(
                name.handle, byref(IID_STORE_CONTEXT_STATICS), byref(statics)
            )
            if result != 0 or not statics:
                raise OSError(
                    f"the Store's StoreContext is not available ({result & 0xFFFFFFFF:#x})"
                )
            _method(statics, STATICS_GET_DEFAULT, POINTER(c_void_p))(byref(context))
            # Without this a desktop app's dialog has no window to belong to.
            owner = _query(context, IID_INITIALIZE_WITH_WINDOW)
            _method(owner, INITIALIZE, wintypes.HWND)(window)
            _method(context, CONTEXT_REQUEST_PURCHASE, c_void_p, POINTER(c_void_p))(
                item.handle, byref(self._operation)
            )
            self._info = _query(self._operation, IID_ASYNC_INFO)
        finally:
            for pointer in (owner, context, statics):
                _release(pointer)
            name.delete()
            item.delete()

    def outcome(self) -> Outcome | None:
        """None while the dialog is still open; then how it ended (asked once)."""
        if not self._info:
            return Outcome.FAILED
        status = c_int()
        _method(self._info, ASYNC_STATUS, POINTER(c_int))(byref(status))
        if status.value == STARTED:
            return None
        try:
            if status.value != COMPLETED:
                return Outcome.FAILED
            result = c_void_p()
            _method(self._operation, OPERATION_GET_RESULTS, POINTER(c_void_p))(byref(result))
            try:
                purchase = c_int()
                _method(result, RESULT_STATUS, POINTER(c_int))(byref(purchase))
            finally:
                _release(result)
            try:
                return Outcome(purchase.value)
            except ValueError:
                return Outcome.FAILED
        finally:
            self.close()

    def close(self) -> None:
        _release(self._info)
        _release(self._operation)
        self._info = c_void_p()
        self._operation = c_void_p()


def request_purchase(window: int, store_id: str) -> Purchase:
    """Show the Store's purchase dialog for store_id over window. OSError if it cannot."""
    return Purchase(window, store_id)
