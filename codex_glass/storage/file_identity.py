"""Stable Windows identities independent of CPython's stat device width."""

import os


def file_identity(stat, stream):
    if os.name != "nt":
        return f"{stat.st_dev}:{stat.st_ino}" if stat.st_ino else ""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class FileInformation(ctypes.Structure):
        _fields_ = [
            ("attributes", wintypes.DWORD),
            ("creation", wintypes.FILETIME),
            ("access", wintypes.FILETIME),
            ("write", wintypes.FILETIME),
            ("volume", wintypes.DWORD),
            ("size_high", wintypes.DWORD),
            ("size_low", wintypes.DWORD),
            ("links", wintypes.DWORD),
            ("index_high", wintypes.DWORD),
            ("index_low", wintypes.DWORD),
        ]

    api = ctypes.WinDLL("kernel32", use_last_error=True).GetFileInformationByHandle
    api.argtypes = (wintypes.HANDLE, ctypes.POINTER(FileInformation))
    api.restype = wintypes.BOOL
    information = FileInformation()
    if not api(msvcrt.get_osfhandle(stream.fileno()), ctypes.byref(information)):
        # Do not fall back to a different format and erase indexed facts.
        raise ctypes.WinError(ctypes.get_last_error())
    inode = (information.index_high << 32) | information.index_low
    return f"{information.volume}:{inode}" if inode else ""
