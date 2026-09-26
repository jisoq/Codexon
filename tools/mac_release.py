"""Gate macOS publication on immutable commit, two architectures and evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
ARCHITECTURES=('arm64','x86_64')


def digest(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def run(arguments):
    result=subprocess.run(arguments,capture_output=True,text=True,check=True)
    return result.stdout.strip()


def metadata(path):return json.loads(Path(path).read_text())


def verify_report(report, *, commit, version, architecture, team):
    from tools.mac_package import os_version
    if (report.get('validated') is not True or report.get('commit')!=commit
            or report.get('version')!=version or report.get('architecture')!=architecture
            or report.get('signing')!='Developer ID' or report.get('team')!=team
            or report.get('source_dirty') is not False or report.get('notarized') is not True):
        raise ValueError('The signed build does not match the verified release commit, version, architecture or team.')
    required={'Codexon.app','Codexon Recovery.app','Install Codexon.app',f'Codexon-macOS-{architecture}.dmg'}
    if set(report.get('stapled',[]))!=required:raise ValueError('Every shipped app and disk image must carry its notarization ticket.')
    if os_version(report.get('minimum_macos'))>os_version('13.0'):
        raise ValueError('A public release cannot raise the supported minimum macOS version.')


def verify_checksum(image):
    sidecar=image.with_suffix(image.suffix+'.sha256')
    match=re.fullmatch(r'([a-f0-9]{64})  '+re.escape(image.name)+r'\n?',sidecar.read_text())
    if not match or match[1]!=digest(image):raise ValueError('Release asset checksum mismatch.')
    return sidecar


def stage(build, native, installation, handoff, output, *, commit, version, architecture, team):
    report=metadata(build/'build-report.json')
    verify_report(report,commit=commit,version=version,architecture=architecture,team=team)
    desktop=metadata(native);install=metadata(installation);gui=metadata(handoff)
    if (desktop.get('passed') is not True or desktop.get('platform')!='cocoa'
            or install.get('passed') is not True or gui.get('passed') is not True):
        raise ValueError('Native desktop, installation and GUI handoff verification must all pass.')
    image=build/f'Codexon-macOS-{architecture}.dmg';sidecar=verify_checksum(image)
    output.mkdir(parents=True,exist_ok=False)
    for path in (image,sidecar):shutil.copy2(path,output/path.name)
    evidence={**report,'source_checks_passed':True,'native_desktop':desktop,'installation':install,'handoff':gui}
    (output/'verification.json').write_text(json.dumps(evidence,indent=2)+'\n')


def release_files(folder, *, commit, version, team):
    files=[]
    for architecture in ARCHITECTURES:
        root=folder/architecture
        evidence=metadata(root/'verification.json')
        verify_report(evidence,commit=commit,version=version,architecture=architecture,team=team)
        if (evidence.get('source_checks_passed') is not True
                or evidence.get('native_desktop',{}).get('passed') is not True
                or evidence.get('native_desktop',{}).get('platform')!='cocoa'
                or evidence.get('installation',{}).get('passed') is not True
                or evidence.get('handoff',{}).get('passed') is not True):
            raise ValueError('Both architectures require passing source, native and installation checks.')
        image=root/f'Codexon-macOS-{architecture}.dmg'
        files.extend((image,verify_checksum(image)))
    return files


def api(path, *, missing=False):
    result=subprocess.run(['gh','api',path],capture_output=True,text=True)
    if result.returncode:
        if missing and 'HTTP 404' in result.stderr:return None
        raise RuntimeError('GitHub release state could not be verified.')
    return json.loads(result.stdout)


def remote_tag(repository, tag):
    ref=api(f'repos/{repository}/git/ref/tags/{tag}',missing=True)
    if ref is None:return None
    obj=ref['object']
    for _ in range(5):
        if obj.get('type')=='commit':return obj['sha']
        if obj.get('type')!='tag':break
        obj=api(f'repos/{repository}/git/tags/{obj["sha"]}')['object']
    raise ValueError('Release tag does not resolve to a commit.')


def select(tag=None):
    from cachemonitor.version import VERSION
    commit=run(['git','rev-parse','HEAD'])
    expected='v'+VERSION
    if tag and tag!=expected:raise ValueError('The selected tag must match VERSION in the dispatched commit.')
    if not re.fullmatch(r'v\d{4}\.\d{2}\.\d{2}\.\d+',expected):raise ValueError('Invalid release version.')
    exists=subprocess.run(['git','show-ref','--verify','--quiet','refs/tags/'+expected],capture_output=True)
    if exists.returncode not in (0,1):raise RuntimeError('Cannot inspect the existing tag.')
    if exists.returncode==0 and run(['git','rev-parse',expected+'^{commit}'])!=commit:
        raise ValueError('An existing release tag points to another commit. Dispatch that commit or increment VERSION.')
    return dict(tag=expected,version=VERSION,commit=commit)


def publish(folder, *, commit, tag, team, repository):
    if not re.fullmatch(r'[0-9a-f]{40}',commit) or not re.fullmatch(r'v\d{4}\.\d{2}\.\d{2}\.\d+',tag):
        raise ValueError('Invalid release identity.')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repository):raise ValueError('Invalid repository.')
    files=release_files(folder,commit=commit,version=tag[1:],team=team)
    existing_tag=remote_tag(repository,tag)
    if existing_tag is not None and existing_tag!=commit:
        raise ValueError('The release tag changed or belongs to a different commit. No assets were published.')
    release=api(f'repos/{repository}/releases/tags/{tag}',missing=True)
    if release and (release.get('draft') or release.get('prerelease')):
        raise ValueError('Do not attach production macOS builds to a draft or prerelease.')
    if release and existing_tag!=commit:raise ValueError('The existing release has no matching immutable tag.')
    # Matching assets make retries safe; differing files are never overwritten.
    existing={asset['name']:asset for asset in (release or {}).get('assets',[])}
    from tools.prepare_sources import VERSION as qt_version
    sources=folder/'sources'/f'Codexon-third-party-source-{qt_version}.zip'
    sources_checksum=verify_checksum(sources)
    source_names={sources.name,sources_checksum.name}
    if source_names & existing.keys():
        if not source_names <= existing.keys():raise ValueError('An existing third-party source archive is incomplete.')
        # An existing Windows release from this exact commit already includes
        # the same pinned upstream sources; ZIP timestamps can differ by build.
    else:files.extend((sources,sources_checksum))
    pending=[]
    for path in files:
        if path.name in existing:
            if existing[path.name].get('digest')!='sha256:'+digest(path):
                raise ValueError('A different asset already has this name. Existing releases are never overwritten.')
        else:pending.append(path)
    if release:
        if pending:run(['gh','release','upload',tag,*map(str,pending),'--repo',repository])
        return dict(published=True,attached=True,tag=tag,commit=commit)
    # A Mac-only release must not become /latest for the Windows updater.
    notes=folder/'release-notes.txt'
    notes.write_text(f'Source: {commit}. Apple Silicon and Intel source, native desktop, installation and notarization checks passed.\n')
    run(['gh','release','create',tag,*map(str,files),'--repo',repository,'--target',commit,
         '--title','Codexon '+tag[1:]+' for macOS','--draft','--latest=false','--notes-file',str(notes)])
    if remote_tag(repository,tag)!=commit:
        raise ValueError('The created tag does not match the verified commit. The release remains a draft.')
    run(['gh','release','edit',tag,'--repo',repository,'--draft=false','--latest=false'])
    return dict(published=True,attached=False,tag=tag,commit=commit)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='action',required=True)
    selected=sub.add_parser('select');selected.add_argument('--tag')
    staged=sub.add_parser('stage')
    for flag in ('build','native','installation','handoff','output'):staged.add_argument('--'+flag,type=Path,required=True)
    staged.add_argument('--architecture',choices=ARCHITECTURES,required=True)
    staged.add_argument('--version',required=True)
    published=sub.add_parser('publish');published.add_argument('--folder',type=Path,required=True)
    published.add_argument('--tag',required=True);published.add_argument('--repository',required=True)
    for item in (staged,published):
        item.add_argument('--commit',required=True);item.add_argument('--team',required=True)
    args=parser.parse_args();options=vars(args);action=options.pop('action')
    if action=='select':
        result=select(**options)
        if os.environ.get('GITHUB_OUTPUT'):
            with Path(os.environ['GITHUB_OUTPUT']).open('a') as file:
                for key,value in result.items():file.write(f'{key}={value}\n')
    elif action=='stage':stage(**options);result={'staged':True}
    else:result=publish(**options)
    print(json.dumps(result))


if __name__=='__main__':main()
