"""Run only synthetic tests in a credential-free, network-blocked subprocess."""
from pathlib import Path
import subprocess,sys
root=Path(__file__).resolve().parents[1]
code="""import socket,unittest
from unittest.mock import patch
with patch.object(socket.socket,'connect',side_effect=AssertionError('offline test network blocked')),patch.object(socket.socket,'connect_ex',side_effect=AssertionError('offline test network blocked')),patch.object(socket,'create_connection',side_effect=AssertionError('offline test network blocked')):
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.discover('tests'))
    raise SystemExit(not result.wasSuccessful())
"""
raise SystemExit(subprocess.run([sys.executable,'-c',code],cwd=root,env={'PATH':'/usr/bin:/bin','PYTHONUTF8':'1'}).returncode)
