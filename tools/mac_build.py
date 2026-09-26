"""Build native, versioned macOS apps and a DMG without touching user state."""
from __future__ import annotations

import argparse
import datetime
import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import platform
import plistlib
import shutil
import struct
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from cachemonitor.version import VERSION, PROXY_VERSION


def run(command, **options):
    result=subprocess.run([str(p) for p in command],check=True,**options)
    return result


def notices(bundle):
    resources=bundle/'Contents/Resources'
    for name in ('LICENSE','THIRD-PARTY-NOTICES.md','SOURCE-OFFER.md'):
        shutil.copy2(ROOT/name,resources/name)
    shutil.copytree(ROOT/'LICENSES',resources/'LICENSES',dirs_exist_ok=True)
    rows=[]
    # Freeze the actual environment inventory, preserving upstream licenses.
    for dist in sorted(metadata.distributions(),key=lambda d:d.metadata['Name'].lower()):
        name=dist.metadata['Name']
        if name.lower() in ('pip','setuptools','pytest','pygments','iniconfig','pluggy'):continue
        licenses=[f for f in (dist.files or []) if any(mark in str(f).lower() for mark in ('license','copying','notice'))
                  and dist.locate_file(f).is_file()]
        for item in licenses:
            relative=Path(str(item))
            if '..' in relative.parts:continue
            target=resources/'LICENSES/python'/name/relative
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(dist.locate_file(item),target)
        rows.append(f'| {name} | {dist.version} |')
    (resources/'BUNDLED-PYTHON.md').write_text('# macOS build environment\n\n'
        'This inventory records the pinned build environment; optional packages may not be loaded by each independent helper. '
        'Included license files are in LICENSES/python.\n\n| Package | Version |\n| --- | --- |\n'+'\n'.join(rows)+'\n')
    frameworks=sorted({p.name for p in (bundle/'Contents/Frameworks').rglob('Qt*.framework')})
    if frameworks:
        (resources/'BUNDLED-QT.md').write_text('# Bundled Qt frameworks\n\n'+'\n'.join('- '+name for name in frameworks)+
            '\n\nSee THIRD-PARTY-NOTICES.md, SOURCE-OFFER.md and LICENSES for matching sources and license terms.\n')


def sign(bundle,identity,entitlements=None):
    # PyInstaller already signed the native descendants. Re-sign only the
    # enclosing product, preserving nested app entitlements and notary tickets.
    command=['/usr/bin/codesign','--force','--sign',identity]
    if identity!='-':command+=['--options','runtime','--timestamp']
    if entitlements:command+=['--entitlements',str(entitlements)]
    run(command+[str(bundle)])
    run(['/usr/bin/codesign','--verify','--deep','--strict',bundle])


def notarize(path,profile):
    command=['/usr/bin/xcrun','notarytool','submit',path,'--keychain-profile',profile,
             '--wait','--output-format','json']
    if os.environ.get('CODEXON_NOTARY_KEYCHAIN'):
        command+=['--keychain',os.environ['CODEXON_NOTARY_KEYCHAIN']]
    result=run(command,capture_output=True,text=True)
    status=json.loads(result.stdout)
    if status.get('status')!='Accepted':
        raise RuntimeError('Apple notarization was not accepted; inspect the private notary log before distributing.')


def notarize_app(bundle,work,profile):
    archive=work/(bundle.stem+'-notary.zip')
    run(['/usr/bin/ditto','-c','-k','--keepParent',bundle,archive])
    notarize(archive,profile)
    run(['/usr/bin/xcrun','stapler','staple',bundle])
    run(['/usr/bin/xcrun','stapler','validate',bundle])


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--work',type=Path,required=True)
    parser.add_argument('--architecture',choices=('arm64','x86_64','universal2'),default=platform.machine())
    parser.add_argument('--signing-identity',default=os.environ.get('CODEXON_SIGNING_IDENTITY'))
    parser.add_argument('--notary-profile',default=os.environ.get('CODEXON_NOTARY_PROFILE'),
                        help='An existing notarytool Keychain profile; never pass credentials')
    parser.add_argument('--allow-dirty',action='store_true',help='Local QA only; signed release builds require committed source')
    parser.add_argument('--skip-dmg',action='store_true')
    args=parser.parse_args()
    if sys.platform!='darwin':parser.error('Build on macOS')
    if args.output.exists():parser.error('Choose a new output directory')
    status=run(['git','status','--porcelain'],cwd=ROOT,capture_output=True,text=True).stdout
    if status and (not args.allow_dirty or args.signing_identity):parser.error('Commit source before release packaging; --allow-dirty is for local QA only')
    commit=run(['git','rev-parse','HEAD'],cwd=ROOT,capture_output=True,text=True).stdout.strip()
    identity=args.signing_identity or '-'
    if identity!='-' and not args.notary_profile:parser.error('Developer ID builds require a notary Keychain profile')
    output=args.output.resolve();work=args.work.resolve()
    output.mkdir(parents=True);work.mkdir(parents=True,exist_ok=True)
    env={**os.environ,'CODEXON_TARGET_ARCH':args.architecture,
         'PYINSTALLER_CONFIG_DIR':str(work/'pyinstaller-cache')}
    iconset=work/'Codexon.iconset';iconset.mkdir(exist_ok=True)
    iconpng=work/'Codexon.png'
    run(['/usr/bin/sips','-s','format','png',ROOT/'icons/Codexon.ico','--out',iconpng],capture_output=True)
    for size in (16,32,128,256,512):
        for scale in (1,2):
            name=f'icon_{size}x{size}'+('@2x' if scale==2 else '')+'.png'
            run(['/usr/bin/sips','-z',size*scale,size*scale,iconpng,'--out',iconset/name],capture_output=True)
    icon=work/'Codexon.icns'
    # PNG-backed ICNS elements are understood by macOS without image-service
    # processes, which are unavailable in some sandboxed build environments.
    elements=[]
    for code,name in (('ic07','icon_128x128.png'),('ic08','icon_256x256.png'),
                      ('ic09','icon_512x512.png'),('ic10','icon_512x512@2x.png')):
        data=(iconset/name).read_bytes()
        elements.append(code.encode('ascii')+struct.pack('>I',len(data)+8)+data)
    data=b''.join(elements)
    icon.write_bytes(b'icns'+struct.pack('>I',len(data)+8)+data)
    env['CODEXON_MAC_ICON']=str(icon)
    if identity!='-':env['CODEXON_SIGNING_IDENTITY']=identity
    run([sys.executable,'-m','PyInstaller','--noconfirm','--clean','--distpath',output/'raw',
         '--workpath',work,ROOT/'CodexonMac.spec'],cwd=ROOT,env=env)
    from mac_package import native_requirements,os_version
    native=native_requirements(output/'raw',args.architecture)
    if identity!='-' and os_version(native['minimum_macos'])>os_version('13.0'):
        raise ValueError('Public builds require Python and all native dependencies targeting macOS 13 or earlier. '
                         'This environment requires macOS '+native['minimum_macos']+'.')
    manifest=dict(product='Codexon',platform='darwin',version=VERSION,proxy_version=PROXY_VERSION,
                  commit=commit,architecture=args.architecture,minimum_macos=native['minimum_macos'],
                  local_build=identity=='-',source_dirty=bool(status))
    labels=('Codexon.app','Codexon Recovery.app','Install Codexon.app')
    for label in labels:
        bundle=output/'raw'/label
        resources=bundle/'Contents/Resources'
        resources.mkdir(parents=True,exist_ok=True)
        (resources/'build-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
        notices(bundle)
        plist=bundle/'Contents/Info.plist'
        info=plistlib.loads(plist.read_bytes())
        year,month,day,revision=map(int,VERSION.split('.'))
        info['CFBundleShortVersionString']=f'{year}.{month}.{day}'
        info['CFBundleVersion']=f'{(datetime.date(year,month,day)-datetime.date(2020,1,1)).days}.0.{revision}'
        info['LSMinimumSystemVersion']=manifest['minimum_macos']
        plist.write_bytes(plistlib.dumps(info))
        sign(bundle,identity,ROOT/'installer/macos.entitlements' if label=='Codexon.app' else None)
        if identity!='-' and label!='Install Codexon.app':
            notarize_app(bundle,work,args.notary_profile)
    installer=output/'raw'/'Install Codexon.app'
    payload=installer/'Contents/Resources/payload'
    payload.mkdir()
    for label in labels[:2]:shutil.copytree(output/'raw'/label,payload/label,symlinks=True)
    sign(installer,identity)
    if identity!='-':notarize_app(installer,work,args.notary_profile)
    package=output/'package';package.mkdir()
    shutil.copytree(installer,package/installer.name,symlinks=True)
    shutil.copytree(output/'raw'/'Codexon Recovery.app',package/'Codexon Recovery.app',symlinks=True)
    (package/'Read Me.txt').write_text('Open Install Codexon to install the app and independent recovery tool in your Applications folder.\n'
        'Existing Codex accounts and records are preserved. Remove managed connections in Codexon before removing its app links.\n'
        + ('This is a local ad-hoc build, not a notarized public release.\n' if identity=='-' else ''))
    dmg=output/f'Codexon-macOS-{args.architecture}.dmg'
    from mac_package import validate
    report=validate(package,allow_ad_hoc=identity=='-')
    report.update(source_dirty=bool(status),dmg=None)
    (output/'build-report.json').write_text(json.dumps(report,indent=2)+'\n')
    if not args.skip_dmg:
        run(['/usr/bin/hdiutil','create','-volname','Codexon','-srcfolder',package,'-format','UDZO',dmg])
        if identity!='-':
            run(['/usr/bin/codesign','--sign',identity,'--timestamp',dmg])
            notarize(dmg,args.notary_profile)
            run(['/usr/bin/xcrun','stapler','staple',dmg])
            run(['/usr/bin/xcrun','stapler','validate',dmg])
        digest=hashlib.sha256(dmg.read_bytes()).hexdigest()
        dmg.with_suffix('.dmg.sha256').write_text(digest+'  '+dmg.name+'\n')
    report.update(source_dirty=bool(status),dmg=str(dmg) if not args.skip_dmg else None,
                  notarized=identity!='-' and not args.skip_dmg,
                  stapled=[*labels,dmg.name] if identity!='-' and not args.skip_dmg else [])
    (output/'build-report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report))


if __name__=='__main__':main()
