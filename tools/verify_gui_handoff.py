"""Exercise packaged GUI replacement between two install paths and custom homes."""
import argparse
import ctypes
from ctypes import wintypes as W
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cachemonitor.observer_state import read_json
from tools.verify_recovery import wait_report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--executable',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    root=args.output.resolve();root.mkdir(parents=True,exist_ok=False)
    original=args.executable.resolve()
    previous=root/'previous';shutil.copytree(original.parent,previous)
    homes=[root/'Work Home',root/'Second Home']
    for home in homes:home.mkdir();(home/'codexon-test-home').touch()
    index=root/'data/index.sqlite'
    arguments=['--index-path',str(index)]
    for home in homes:arguments+=['--codex-home',str(home)]
    processes=[];reports=[]
    try:
        for n,exe in enumerate((previous/original.name,original)):
            report=root/f'gui-{n}.json';reports.append(report)
            command=[str(exe),*arguments,'--verify-handoff',str(report)]
            if n:command.append('--replace-gui')
            process=subprocess.Popen(command,creationflags=subprocess.CREATE_NO_WINDOW);processes.append(process)
            value=wait_report(report,lambda d:d.get('ready'))
            assert value['homes']==list(map(str,homes))
            if n:
                assert processes[0].wait(timeout=25)==0
                assert value['pid']!=read_json(reports[0])['pid']
                assert process.poll() is None
        result=dict(passed=True,distinct_install_paths=True,previous_gui_exited=True,
                    multiple_custom_homes_preserved=True,login_startup_required=False)
        (root/'result.json').write_text(json.dumps(result,indent=2));print(json.dumps(result))
    finally:
        api=ctypes.WinDLL('user32');api.PostMessageW.argtypes=[W.HWND,W.UINT,W.WPARAM,W.LPARAM]
        for process,report in zip(processes,reports):
            if process.poll() is None:
                # Close through the same graceful handoff protocol by using Qt IPC.
                from PySide6.QtCore import QCoreApplication
                from PySide6.QtNetwork import QLocalSocket
                import hashlib
                app=QCoreApplication.instance() or QCoreApplication([])
                socket=QLocalSocket();socket.connectToServer('CodexonQA-'+hashlib.sha256(str(index.resolve()).encode()).hexdigest()[:24])
                if socket.waitForConnected(2000):
                    socket.write(b'update-exit');socket.waitForBytesWritten(2000);socket.waitForReadyRead(3000)
                process.wait(timeout=25)


if __name__=='__main__':main()
