import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.skipif(sys.platform!='win32',reason='Windows monitor DPI comparison')
def test_fractional_dpi_and_monitor_transition_in_fresh_process():
    # Separate application lifetime: Qt captures the policy on construction.
    env={k:v for k,v in os.environ.items() if not k.startswith('QT_')}
    root=Path(__file__).resolve().parents[1]
    result=subprocess.run([sys.executable,str(root/'tools/probe_display_scaling.py')],
                          cwd=root,env=env,capture_output=True,text=True,timeout=20)
    assert result.returncode==0, result.stderr+result.stdout
    report=json.loads(result.stdout)
    assert report['passed']
    assert len(report['screens'])>=2
