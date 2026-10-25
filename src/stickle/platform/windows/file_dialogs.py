"""The system's file dialog, asked for through its own COM objects with ctypes
(no extra library): a file to open, where to save one, or a folder.

Qt's QFileDialog shows this same dialog, but each time one closes it records
the last folder and the folders visited in the registry (under QtProject).
Asked for here, Stickle writes nothing there. The dialog itself remembers the
recent folders of each program, as it does for every program.
"""

import ctypes
import sys
import uuid
from collections.abc import Callable, Sequence
from ctypes import wintypes
from pathlib import Path
from typing import Any, Literal

assert sys.platform == "win32"  # also tells the type checker the rest is Windows only

CLSID_FILE_OPEN_DIALOG = uuid.UUID("DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7")
CLSID_FILE_SAVE_DIALOG = uuid.UUID("C0B4E2F3-BA21-4773-8DBA-335EC946EB8B")
IID_FILE_DIALOG = uuid.UUID("42F85136-DB7E-439C-85F1-E4075D135FC8")
IID_SHELL_ITEM = uuid.UUID("43826D1E-E718-42EE-BC55-A1E261C37BFE")
CLSCTX_INPROC_SERVER = 1
COINIT_APARTMENTTHREADED = 2
RPC_E_CHANGED_MODE = -2147417850
CANCELLED = -2147023673  # the user closed the dialog: HRESULT_FROM_WIN32(ERROR_CANCELLED)
SIGDN_FILESYSPATH = 0x80058000
FOS_OVERWRITEPROMPT = 0x2
FOS_NOCHANGEDIR = 0x8
FOS_PICKFOLDERS = 0x20
FOS_FORCEFILESYSTEM = 0x40
FOS_PATHMUSTEXIST = 0x800
FOS_FILEMUSTEXIST = 0x1000
# Positions in the method tables: IUnknown's three, then IModalWindow's Show.
RELEASE = 2
SHOW, SET_FILE_TYPES, SET_FILE_TYPE_INDEX = 3, 4, 5
SET_OPTIONS, GET_OPTIONS, SET_FOLDER = 9, 10, 12
SET_FILE_NAME, SET_TITLE, GET_RESULT, SET_DEFAULT_EXTENSION = 15, 17, 20, 22
GET_DISPLAY_NAME = 5  # of IShellItem


class Guid(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def of(cls, value: uuid.UUID) -> Guid:
        return cls.from_buffer_copy(value.bytes_le)


ole32 = ctypes.WinDLL("ole32")
ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
ole32.CoInitializeEx.restype = ctypes.c_long
ole32.CoUninitialize.argtypes = []
ole32.CoUninitialize.restype = None
ole32.CoCreateInstance.argtypes = [
    ctypes.POINTER(Guid),
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(Guid),
    ctypes.POINTER(ctypes.c_void_p),
]
ole32.CoCreateInstance.restype = ctypes.c_long
ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
ole32.CoTaskMemFree.restype = None
shell32 = ctypes.WinDLL("shell32")
shell32.SHCreateItemFromParsingName.argtypes = [
    wintypes.LPCWSTR,
    ctypes.c_void_p,
    ctypes.POINTER(Guid),
    ctypes.POINTER(ctypes.c_void_p),
]
shell32.SHCreateItemFromParsingName.restype = ctypes.c_long

type Kind = Literal["open", "save", "folder"]


class FilterSpec(ctypes.Structure):  # COMDLG_FILTERSPEC
    _fields_ = [("name", wintypes.LPCWSTR), ("spec", wintypes.LPCWSTR)]
    name: str | None
    spec: str | None


class _Com:
    """One COM interface pointer, with its methods called by table position."""

    def __init__(self, pointer: ctypes.c_void_p) -> None:
        self.pointer = pointer

    def call(self, index: int, types: Sequence[Any], *arguments: Any) -> int:
        """The method's HRESULT, failures included (cancelling is one)."""
        table = ctypes.cast(self.pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
        prototype = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *types)
        function: Callable[..., int] = prototype(table[index])
        return function(self.pointer, *arguments)

    def check(self, index: int, types: Sequence[Any], *arguments: Any) -> None:
        result = self.call(index, types, *arguments)
        if result < 0:
            raise OSError(f"file dialog method {index} failed: {result & 0xFFFFFFFF:#010x}")

    def release(self) -> None:
        table = ctypes.cast(self.pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(table[RELEASE])(self.pointer)


def choose(
    kind: Kind,
    title: str,
    parent_window: int = 0,
    suggested: Path | None = None,
    file_types: Sequence[tuple[str, str]] = (),
    extension: str = "",
) -> Path | None:
    """The file or folder chosen; None if the user cancelled. parent_window: the
    window the dialog is modal to (0: none). suggested: for saving, the folder
    and name; otherwise the folder to start in. file_types: (name, "*.txt;*.md")
    pairs, the first chosen; extension: added to a name typed without one.
    OSError if the dialog cannot be shown."""
    started = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    if started < 0 and started != RPC_E_CHANGED_MODE:  # already set up by Qt: fine
        raise OSError(f"COM could not start: {started & 0xFFFFFFFF:#010x}")
    try:
        dialog = _dialog(kind)
        try:
            kept = _set_up(dialog, kind, title, suggested, file_types, extension)
            shown = dialog.call(SHOW, [wintypes.HWND], parent_window or None)
            del kept  # read by the dialog while it was open
            if shown == CANCELLED:
                return None
            if shown < 0:
                raise OSError(f"the file dialog failed: {shown & 0xFFFFFFFF:#010x}")
            item = ctypes.c_void_p()
            dialog.check(GET_RESULT, [ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(item))
            return _path_of(_Com(item))
        finally:
            dialog.release()
    finally:
        if started >= 0:
            ole32.CoUninitialize()


def _dialog(kind: Kind) -> _Com:
    dialog = ctypes.c_void_p()
    result = ole32.CoCreateInstance(
        ctypes.byref(Guid.of(CLSID_FILE_SAVE_DIALOG if kind == "save" else CLSID_FILE_OPEN_DIALOG)),
        None,
        CLSCTX_INPROC_SERVER,
        ctypes.byref(Guid.of(IID_FILE_DIALOG)),
        ctypes.byref(dialog),
    )
    if result < 0:
        raise OSError(f"cannot create the file dialog: {result & 0xFFFFFFFF:#010x}")
    return _Com(dialog)


def _set_up(
    dialog: _Com,
    kind: Kind,
    title: str,
    suggested: Path | None,
    file_types: Sequence[tuple[str, str]],
    extension: str,
) -> object:
    """Set the dialog up; what it returns is to be kept until the dialog closes."""
    options = wintypes.DWORD()
    dialog.check(GET_OPTIONS, [ctypes.POINTER(wintypes.DWORD)], ctypes.byref(options))
    # Paths only, and the program's working folder left as it is.
    wanted = options.value | FOS_FORCEFILESYSTEM | FOS_NOCHANGEDIR | FOS_PATHMUSTEXIST
    if kind == "folder":
        wanted |= FOS_PICKFOLDERS
    elif kind == "open":
        wanted |= FOS_FILEMUSTEXIST
    else:
        wanted |= FOS_OVERWRITEPROMPT
    dialog.check(SET_OPTIONS, [wintypes.DWORD], wanted)
    dialog.check(SET_TITLE, [wintypes.LPCWSTR], title)
    specs = (FilterSpec * len(file_types))()
    if file_types and kind != "folder":
        for spec, (name, patterns) in zip(specs, file_types, strict=True):
            spec.name, spec.spec = name, patterns
        dialog.check(
            SET_FILE_TYPES, [wintypes.UINT, ctypes.POINTER(FilterSpec)], len(file_types), specs
        )
        dialog.check(SET_FILE_TYPE_INDEX, [wintypes.UINT], 1)  # counted from 1
    if extension:
        dialog.check(SET_DEFAULT_EXTENSION, [wintypes.LPCWSTR], extension)
    if suggested is not None:
        folder = _shell_item(suggested.parent if kind == "save" else suggested)
        if folder is not None:  # not there: the dialog starts where it would
            try:
                dialog.check(SET_FOLDER, [ctypes.c_void_p], folder.pointer)
            finally:
                folder.release()
        if kind == "save":
            dialog.check(SET_FILE_NAME, [wintypes.LPCWSTR], suggested.name)
    return specs


def _shell_item(path: Path) -> _Com | None:
    item = ctypes.c_void_p()
    result = shell32.SHCreateItemFromParsingName(
        str(path), None, ctypes.byref(Guid.of(IID_SHELL_ITEM)), ctypes.byref(item)
    )
    return _Com(item) if result >= 0 and item.value else None


def _path_of(item: _Com) -> Path:
    name = ctypes.c_void_p()
    try:
        item.check(
            GET_DISPLAY_NAME,
            [wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)],
            SIGDN_FILESYSPATH,
            ctypes.byref(name),
        )
        return Path(ctypes.wstring_at(name.value or 0))
    finally:
        if name.value:
            ole32.CoTaskMemFree(name)
        item.release()
