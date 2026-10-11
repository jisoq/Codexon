"""Native CPU kernels, with an explicit reference backend for reproducible comparisons.

Source checkouts may run without a compiled extension. Release packaging checks
the extension separately and fails if it is missing. A requested Rust backend
never silently falls back to a missing or incompatible extension.
"""
from functools import lru_cache
import importlib
import os


@lru_cache(maxsize=1)
def _load():
    try:
        module = importlib.import_module('cachemonitor._cpu_native')
    except ModuleNotFoundError as error:
        if error.name != 'cachemonitor._cpu_native':
            raise
        return None
    if module.ABI_VERSION != 1:
        raise ImportError('Incompatible Codexon CPU extension; run tools/build_native.py')
    return module


def get_native():
    mode = os.environ.get('CODEXON_CPU_BACKEND', 'auto')
    if mode == 'python':
        return None
    if mode not in ('auto', 'rust'):
        raise ValueError('CODEXON_CPU_BACKEND must be auto, python, or rust')
    module = _load()
    if module is None and mode == 'rust':
        raise RuntimeError('Rust CPU backend required; run tools/build_native.py')
    return module
