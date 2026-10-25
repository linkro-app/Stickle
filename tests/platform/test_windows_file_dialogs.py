"""The system's file dialog on Windows, set up as asked: read back from the
dialog itself, not shown (showing waits for a person)."""

import ctypes
import sys
from ctypes import wintypes
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows' own dialog")

# Read-back methods of IFileDialog, used here only.
GET_FILE_TYPE_INDEX, GET_FOLDER, GET_FILE_NAME = 6, 13, 16


def test_saving_starts_in_the_suggested_folder_with_its_name(tmp_path: Path) -> None:
    if sys.platform != "win32":  # also tells the type checker this is Windows
        return
    from stickle.platform.windows import file_dialogs as windows

    started = windows.ole32.CoInitializeEx(None, windows.COINIT_APARTMENTTHREADED)
    dialog = windows._dialog("save")  # pyright: ignore[reportPrivateUsage]
    try:
        windows._set_up(  # pyright: ignore[reportPrivateUsage]
            dialog,
            "save",
            "복구 키 저장",
            tmp_path / "Stickle recovery key.txt",
            [("Text files (*.txt)", "*.txt"), ("All (*)", "*")],
            "txt",
        )
        options = wintypes.DWORD()
        dialog.check(windows.GET_OPTIONS, [ctypes.POINTER(wintypes.DWORD)], ctypes.byref(options))
        wanted = windows.FOS_FORCEFILESYSTEM | windows.FOS_NOCHANGEDIR | windows.FOS_OVERWRITEPROMPT
        assert options.value & wanted == wanted
        assert not options.value & windows.FOS_PICKFOLDERS

        index = wintypes.UINT()
        dialog.check(GET_FILE_TYPE_INDEX, [ctypes.POINTER(wintypes.UINT)], ctypes.byref(index))
        assert index.value == 1

        name = ctypes.c_void_p()
        dialog.check(GET_FILE_NAME, [ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(name))
        assert ctypes.wstring_at(name.value or 0) == "Stickle recovery key.txt"
        windows.ole32.CoTaskMemFree(name)

        folder = ctypes.c_void_p()
        dialog.check(GET_FOLDER, [ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(folder))
        path = windows._path_of(windows._Com(folder))  # pyright: ignore[reportPrivateUsage]
        assert path.resolve() == tmp_path.resolve()
    finally:
        dialog.release()
        if started >= 0:
            windows.ole32.CoUninitialize()


def test_a_folder_is_asked_for_as_a_folder_and_a_file_to_open_must_be_there(
    tmp_path: Path,
) -> None:
    if sys.platform != "win32":
        return
    from stickle.platform.windows import file_dialogs as windows

    kinds: list[tuple[windows.Kind, int]] = [
        ("folder", windows.FOS_PICKFOLDERS),
        ("open", windows.FOS_FILEMUSTEXIST),
    ]
    started = windows.ole32.CoInitializeEx(None, windows.COINIT_APARTMENTTHREADED)
    try:
        for kind, wanted in kinds:
            dialog = windows._dialog(kind)  # pyright: ignore[reportPrivateUsage]
            try:
                windows._set_up(dialog, kind, "t", tmp_path / "missing", [], "")  # pyright: ignore[reportPrivateUsage]
                options = wintypes.DWORD()
                dialog.check(
                    windows.GET_OPTIONS, [ctypes.POINTER(wintypes.DWORD)], ctypes.byref(options)
                )
                assert options.value & wanted
            finally:
                dialog.release()
    finally:
        if started >= 0:
            windows.ole32.CoUninitialize()
