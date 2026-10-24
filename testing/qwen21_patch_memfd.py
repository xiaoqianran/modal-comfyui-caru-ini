"""Patch ComfyUI anonymous_file.py without modifying Modal worker environment."""
from pathlib import Path

path = Path("/root/comfy/ComfyUI/comfy/anonymous_file.py")
marker = "# QWEN21_MODAL_MEMFD_COMPAT"
patch = """
# QWEN21_MODAL_MEMFD_COMPAT
# The Python build may omit os.memfd_create despite Linux exposing libc.memfd_create.
import ctypes as _qwen_ctypes
import os as _qwen_os
if not hasattr(_qwen_os, "MFD_CLOEXEC"):
    _qwen_os.MFD_CLOEXEC = 1
if not hasattr(_qwen_os, "memfd_create"):
    _qwen_libc = _qwen_ctypes.CDLL("libc.so.6", use_errno=True)
    _qwen_native = _qwen_libc.memfd_create
    _qwen_native.argtypes = [_qwen_ctypes.c_char_p, _qwen_ctypes.c_uint]
    _qwen_native.restype = _qwen_ctypes.c_int
    def _qwen_memfd_create(name, flags=1):
        fd = _qwen_native(_qwen_os.fsencode(name), flags)
        if fd < 0:
            err = _qwen_ctypes.get_errno()
            raise OSError(err, _qwen_os.strerror(err))
        return fd
    _qwen_os.memfd_create = _qwen_memfd_create
"""
text = path.read_text(encoding="utf-8")
if marker not in text:
    path.write_text(text + "\n" + patch, encoding="utf-8")
print("patched", path)
