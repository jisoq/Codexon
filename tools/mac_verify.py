"""Exercise built macOS apps with isolated records, installs and launch links."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tomllib
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from cachemonitor import macos_installation as mac
from cachemonitor.observer_state import read_json


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--product',type=Path,required=True,help='Folder containing the frozen app bundles')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--allow-ad-hoc',action='store_true')
    args=parser.parse_args()
    if sys.platform!='darwin':parser.error('Run on macOS')
    root=args.output.resolve();root.mkdir(parents=True,exist_ok=False)
    source=args.product.resolve()
    if (source/'Install Codexon.app').is_dir():
        source=source/'Install Codexon.app/Contents/Resources/payload'
    os.environ['CODEXON_DATA_DIR']=str(root/'application-data')
    os.environ['CODEXON_SERVICE_TEST_ROOT']=str(root/'service-qa')
    home=root/'codex home';home.mkdir();(home/'codexon-test-home').touch()
    # The runtime diagnostic imports Qt but starts no UI, collector or proxy.
    options=dict(root=root/'installation',applications=root/'Applications',
                 isolated=True,launch=False,allow_ad_hoc=args.allow_ad_hoc)
    first=mac.install_source(source,**options)
    second=mac.install_source(source,**options)
    assert second['previous']==first['product'] and Path(first['AppPath']).is_file()
    assert mac.installed(options['root'])['AppPath']==second['AppPath']
    # Inject one failed receipt commit and exercise the real journal rollback.
    original=mac.atomic_write;failed=[]
    def failing_write(path,data):
        if path==options['root']/'installation.json' and not failed:
            failed.append(True);raise OSError('synthetic receipt failure')
        original(path,data)
    with patch.object(mac,'atomic_write',failing_write):
        try:mac.install_source(source,**options)
        except OSError:pass
        else:raise AssertionError('Receipt failure did not stop activation')
    assert mac.installed(options['root'])['AppPath']==second['AppPath']
    assert (options['applications']/'Codexon.app').resolve()==mac.bundle_for(second['AppPath'])
    # Copy ONLY the independent recovery bundle. It has no Qt or desktop runtime.
    recovery=root/'independent'/'Codexon Recovery.app'
    shutil.copytree(source/'Codexon Recovery.app',recovery,symlinks=True)
    assert not any('PySide6' in str(p) or p.name.startswith('QtCore') for p in recovery.rglob('*'))
    binary=mac.executable(recovery)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    assert port!=8768
    url=f'http://127.0.0.1:{port}'
    config=home/'config.toml';config.write_text(f'# preserve\nmodel="synthetic"\nopenai_base_url="{url}"\n')
    auth=home/'auth.json';auth.write_text('{"synthetic":"preserve"}')
    report=root/'recovery.json'
    command=[str(binary),'--codex-home',str(home),'--data-dir',str(root/'recovery-data'),
             '--proxy-url',url,'--report',str(report)]
    subprocess.run(command+['--status'],check=True,timeout=45,capture_output=True)
    assert read_json(report)['code']=='refused'
    subprocess.run(command+['--restore'],check=True,timeout=45,capture_output=True)
    assert read_json(report)['code']=='restored'
    assert tomllib.loads(config.read_text())=={'model':'synthetic'} and '# preserve' in config.read_text()
    assert auth.read_text()=='{"synthetic":"preserve"}'
    config.write_text(f'# preserve\nmodel="synthetic"\nopenai_base_url="{url}"\n')
    native_report=root/'native-recovery.json'
    subprocess.run(command+['--ui-smoke',str(native_report)],check=True,timeout=45,capture_output=True)
    native=read_json(native_report)
    assert native.get('passed') and native.get('actual_button_activation') and native.get('before')=='refused'
    assert tomllib.loads(config.read_text())=={'model':'synthetic'} and auth.read_text()=='{"synthetic":"preserve"}'
    # The installation is isolated and never registered a launchd job. Verify
    # removal state without touching any existing user's service definitions.
    with patch('cachemonitor.macos_services.remove_installation_tasks',return_value={'removed':[]}):
        removed=mac.prepare_uninstall(options['root'])
    assert removed['ready'] and not (options['applications']/'Codexon.app').exists()
    assert Path(first['AppPath']).is_file() and auth.is_file()
    result=dict(passed=True,installation=True,reinstallation=True,receipt_rollback=True,
                independent_recovery=True,native_recovery_button=True,config_restored=True,auth_preserved=True,
                removal=True,records_preserved=True,model_requests=0,live_quota_requests=0,
                production_services_changed=False)
    (root/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result))


if __name__=='__main__':main()
