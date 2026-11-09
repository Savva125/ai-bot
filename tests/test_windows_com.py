"""COM initialization only: no window enumeration, screen reads or input."""
import os
import subprocess
import sys

import pytest


@pytest.mark.skipif(os.name != "nt", reason="Windows COM only")
@pytest.mark.parametrize("preinitialized", [False, True])
def test_com_import_order_in_actual_worker(preinitialized):
    # Fresh process avoids interference with GUI tests' own COM initialization.
    code = '''
from threading import Thread
from creator_desk.screen_agent import initialize_uia_com
errors = []
def work():
    try:
        if PREINITIALIZED:
            import comtypes  # Reproduce the old first-import STA behavior.
        apartment = initialize_uia_com()
        import comtypes
        comtypes.CoInitializeEx(apartment)
        comtypes.CoUninitialize()
        assert initialize_uia_com() == apartment
        assert apartment == (2 if PREINITIALIZED else 0)
        from creator_desk.screen_agent import NativeScreen
        assert NativeScreen.desktop().backend.name == 'uia'
    except Exception as error:
        errors.append(repr(error))
thread = Thread(target=work)
thread.start()
thread.join(10)
assert not thread.is_alive()
assert not errors, errors
'''.replace("PREINITIALIZED", repr(preinitialized))
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
