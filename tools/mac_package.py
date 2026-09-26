"""Validate actual macOS payloads, including signed framework symlinks."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import plistlib
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cachemonitor.macos_installation import validate_source, verify_bundle, INSTALLER_ID


def os_version(value):
    if not isinstance(value,str) or not re.fullmatch(r'\d+\.\d+(?:\.\d+)?',value):
        raise ValueError('Invalid minimum macOS version')
    parts=tuple(map(int,value.split('.')))
    return (*parts,*(0 for _ in range(3-len(parts))))


def native_requirements(folder, architecture):
    """Read every Mach-O load command instead of trusting Info.plist claims."""
    required={'arm64','x86_64'} if architecture=='universal2' else {architecture}
    if not required <= {'arm64','x86_64'}:raise ValueError('Unsupported macOS architecture')
    magic={b'\xfe\xed\xfa\xce',b'\xce\xfa\xed\xfe',b'\xfe\xed\xfa\xcf',b'\xcf\xfa\xed\xfe',
           b'\xca\xfe\xba\xbe',b'\xbe\xba\xfe\xca',b'\xca\xfe\xba\xbf',b'\xbf\xba\xfe\xca'}
    minimum='13.0';count=0
    for path in Path(folder).rglob('*'):
        if not path.is_file() or path.is_symlink() or path.suffix=='.a':continue
        with path.open('rb') as stream:
            if stream.read(4) not in magic:continue
        arch=subprocess.run(['/usr/bin/lipo','-archs',str(path)],capture_output=True,text=True,check=True).stdout.split()
        if not required <= set(arch):raise ValueError('A native component does not support the declared architecture: '+path.name)
        output=subprocess.run(['/usr/bin/vtool','-show-build',str(path)],capture_output=True,text=True,check=True).stdout
        platforms=re.findall(r'^\s*platform\s+(\S+)',output,re.MULTILINE)
        if any(platform!='MACOS' for platform in platforms):raise ValueError('A bundled native component targets a different Apple platform')
        versions=re.findall(r'cmd LC_BUILD_VERSION\b(?:(?!Load command).)*?\bminos\s+(\d+\.\d+(?:\.\d+)?)',output,re.DOTALL)
        versions+=re.findall(r'cmd LC_VERSION_MIN_MACOSX\b(?:(?!Load command).)*?\bversion\s+(\d+\.\d+(?:\.\d+)?)',output,re.DOTALL)
        if not versions:raise ValueError('A native component has no declared minimum macOS: '+path.name)
        for version in versions:
            if os_version(version)>os_version(minimum):minimum=version
        count+=1
    if not count:raise ValueError('No native macOS runtime was found')
    return dict(minimum_macos=minimum,native_binaries=count)


def validate(folder, *, allow_ad_hoc=False):
    folder = Path(folder).resolve(strict=True)
    installer = folder/'Install Codexon.app'
    source = installer/'Contents/Resources/payload' if installer.is_dir() else folder
    manifest, identities = validate_source(source, allow_ad_hoc=allow_ad_hoc)
    if installer.is_dir():
        verify_bundle(installer, INSTALLER_ID, allow_ad_hoc=allow_ad_hoc,
                      expected_team=identities['Codexon.app']['team'])
    native=native_requirements(folder,manifest['architecture'])
    declared=manifest.get('minimum_macos')
    if os_version(declared)<os_version(native['minimum_macos']):
        raise ValueError('The package requires a newer macOS than its manifest declares')
    for bundle in folder.rglob('*.app'):
        info=plistlib.loads((bundle/'Contents/Info.plist').read_bytes())
        if info.get('LSMinimumSystemVersion')!=declared:
            raise ValueError('An app Info.plist disagrees with the minimum macOS manifest')
    forbidden = {'auth.json', 'config.toml', '.env'}
    count = 0
    for bundle in source.glob('*.app'):
        for path in bundle.rglob('*'):
            if path.is_symlink():
                if not path.resolve().is_relative_to(bundle.resolve()) or not path.exists():
                    raise ValueError('Bundle symlink escapes its product or is broken')
            if path.is_file():
                count += 1
                if path.name in forbidden or path.suffix.lower() in ('.sqlite', '.db', '.jsonl', '.log'):
                    raise ValueError('Private records found in bundle')
        resources = bundle/'Contents/Resources'
        required = {'LICENSE','THIRD-PARTY-NOTICES.md','SOURCE-OFFER.md','BUNDLED-PYTHON.md','build-manifest.json'}
        if not required <= {p.name for p in resources.iterdir()}:
            raise ValueError('Required distribution notices are missing')
    return dict(validated=True, files=count, version=manifest['version'], commit=manifest['commit'],
                architecture=manifest['architecture'], team=identities['Codexon.app']['team'],
                signing='ad-hoc' if identities['Codexon.app']['ad_hoc'] else 'Developer ID',**native)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--product',type=Path,required=True)
    parser.add_argument('--allow-ad-hoc',action='store_true')
    args=parser.parse_args()
    print(json.dumps(validate(args.product,allow_ad_hoc=args.allow_ad_hoc)))


if __name__ == '__main__':main()
