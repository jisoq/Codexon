"""Extract the actual published installer for isolated migration checks; never install it."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import urllib.request
from zipfile import ZipFile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cachemonitor.app_update import release_asset

EXTRACTOR_URL='https://raw.githubusercontent.com/jrathlev/InnoUnpacker-Windows-GUI/6fb49264aacf512a093e7b4fc6fb3dd266dad31a/innounp-2/bin/innounp-267.zip'
EXTRACTOR_SHA256='ac1d98bba6588072ade06163781938df274f444bec7318069668608d1e5faae8'


def digest(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def download(url,path,maximum):
    request=urllib.request.Request(url,headers={'User-Agent':'Codexon-migration-verification'})
    with urllib.request.urlopen(request,timeout=30) as response,Path(path).open('xb') as output:
        size=0
        while chunk:=response.read(1024*1024):
            size+=len(chunk)
            if size>maximum:raise ValueError('Download exceeds declared size')
            output.write(chunk)


def unpack_tool(archive,output):
    if digest(archive)!=EXTRACTOR_SHA256:raise ValueError('Extractor checksum mismatch')
    with ZipFile(archive) as zipped:
        names=[n for n in zipped.namelist() if n.replace('\\','/').split('/')[-1].lower()=='innounp.exe']
        if len(names)!=1:raise ValueError('Extractor executable is ambiguous')
        # Extract only one named payload into a fixed path, never archive paths.
        Path(output).write_bytes(zipped.read(names[0]))


def product(root,version):
    candidates=[]
    for path in Path(root).rglob('build-manifest.json'):
        manifest=json.loads(path.read_text(encoding='utf-8-sig'))
        if manifest.get('product')=='Codexon' and manifest.get('version')==version:
            candidates.append((path,manifest))
    if len(candidates)!=1:raise ValueError('Published product is missing or ambiguous')
    path,manifest=candidates[0];executable=path.with_name('Codexon.exe')
    if digest(executable)!=manifest.get('sha256'):raise ValueError('Published executable checksum mismatch')
    return executable.resolve(),manifest


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tag',required=True)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--installer',type=Path,help='Use a local copy after checking it against the public release')
    args=parser.parse_args()
    if not re.fullmatch(r'v\d{4}\.\d{2}\.\d{2}\.\d+',args.tag):parser.error('An explicit release tag is required')
    root=args.output.resolve();root.mkdir(parents=True,exist_ok=False)
    metadata=root/'release.json'
    download('https://api.github.com/repos/jisoq/Codexon/releases/tags/'+args.tag,metadata,2_000_000)
    release=json.loads(metadata.read_text(encoding='utf-8'));asset,checksum=release_asset(release)
    checksum_path=root/'Codexon-Setup.exe.sha256'
    download(checksum['browser_download_url'],checksum_path,1024)
    match=re.fullmatch(r'([a-fA-F0-9]{64})\s+\*?Codexon-Setup\.exe',checksum_path.read_text(encoding='ascii').strip())
    if not match:raise ValueError('Published checksum is invalid')
    setup=root/'Codexon-Setup.exe'
    if args.installer:shutil.copyfile(args.installer,setup)
    else:download(asset['browser_download_url'],setup,asset['size'])
    sha=digest(setup)
    if (setup.stat().st_size!=asset['size'] or sha!=match[1].lower() or
            asset.get('digest') not in (None,'sha256:'+sha)):
        raise ValueError('Published installer checksum mismatch')
    archive=root/'extractor.zip';download(EXTRACTOR_URL,archive,10_000_000)
    extractor=root/'innounp.exe';unpack_tool(archive,extractor)
    extracted=root/'extracted'
    result=subprocess.run([str(extractor),'-x','-b','-q','-d'+str(extracted),str(setup)],
        capture_output=True,timeout=120,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    (root/'extraction.log').write_bytes(result.stdout+result.stderr)
    if result.returncode:raise RuntimeError('Published installer extraction failed')
    executable,manifest=product(extracted,args.tag[1:])
    report=dict(tag=args.tag,installer_sha256=sha,extractor_sha256=EXTRACTOR_SHA256,
        executable=str(executable),manifest=manifest,installer_executed=False)
    (root/'result.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report),flush=True)


if __name__=='__main__':main()
