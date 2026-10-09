"""Join release requests to one verified main-branch artifact for the exact commit."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.prepare_sources import describe, digest, ensure_hosted, load_lock
from tools.release_github import GitHub

WORKFLOW = '.github/workflows/windows.yml'
FILES = ('Codexon-Setup.exe', 'Codexon-Setup.exe.sha256', 'build-manifest.json', 'RELEASE_NOTES.md')


def version(root=ROOT):
    text = (root/'cachemonitor/version.py').read_text(encoding='utf-8')
    return re.search(r"^VERSION\s*=\s*['\"](\d{4}\.\d{2}\.\d{2}\.\d+)['\"]", text, re.M)[1]


def trusted(run, sha, repository):
    return (run.get('head_sha') == sha and run.get('head_branch') == 'main'
            and run.get('event') in ('push', 'workflow_dispatch') and run.get('path') == WORKFLOW
            and run.get('head_repository', {}).get('full_name') == repository)


def verification_passed(github, run_id):
    jobs = github.api(f'actions/runs/{run_id}/jobs')['jobs']
    return any(j['name'] == 'verify' and j['conclusion'] == 'success' for j in jobs)


def reusable_run(github, sha, current_run):
    runs = github.api(f'actions/workflows/windows.yml/runs?head_sha={sha}&per_page=100')['workflow_runs']
    for run in sorted(runs, key=lambda r: r['id'], reverse=True):
        if (str(run['id']) == str(current_run) or not trusted(run, sha, github.repository)
                or run.get('status') != 'completed'):
            continue
        artifacts = github.api(f'actions/runs/{run["id"]}/artifacts')['artifacts']
        if (any(a['name'] == f'verified-release-{sha}' and not a['expired'] for a in artifacts)
                and verification_passed(github, run['id'])):
            return str(run['id'])
    return ''


def verified_base(github, sha, root=ROOT):
    # A failed or cancelled push is not a verification baseline for its follow-up.
    runs = github.api('actions/workflows/windows.yml/runs?branch=main&status=success&per_page=100')['workflow_runs']
    for run in sorted(runs, key=lambda r: r['id'], reverse=True):
        base = run.get('head_sha', '')
        if (base == sha or not re.fullmatch(r'[0-9a-f]{40}', base)
                or not trusted(run, base, github.repository) or run.get('status') != 'completed'
                or run.get('conclusion') != 'success'):
            continue
        result = subprocess.run(['git', 'merge-base', '--is-ancestor', base, sha], cwd=root, capture_output=True)
        if result.returncode == 0:
            return base
        if result.returncode not in (1, 128):
            raise RuntimeError('Cannot establish the last verified ancestor')
    return ''


def tag_commit(github, tag):
    value = github.api('git/ref/tags/' + tag, missing=True)
    if value is None:
        return None
    obj = value['object']
    if obj['type'] == 'tag':
        obj = github.api('git/tags/' + obj['sha'])['object']
    if obj['type'] != 'commit':
        raise ValueError('Release tag does not resolve to a commit')
    return obj['sha']


def plan(github, sha, current_run, event, ref, publish, root=ROOT):
    if not re.fullmatch(r'[0-9a-f]{40}', sha):
        raise ValueError('Invalid release commit')
    on_main = event in ('push', 'workflow_dispatch') and ref == 'refs/heads/main'
    if publish and not on_main:
        raise ValueError('Publish releases only from main')
    current = version(root)
    notes = root/f'docs/releases/{current}.md'
    has_notes = notes.is_file() and bool(notes.read_text(encoding='utf-8').strip())
    tag = 'v' + current
    existing = tag_commit(github, tag) if on_main else None
    release = github.release(tag) if publish and existing else None
    if publish and (not has_notes or (existing and existing != sha)):
        raise ValueError('A new version and nonempty matching release notes are required')
    published = bool(release and not release['draft'] and existing == sha)
    candidate = on_main and has_notes and (existing is None or bool(release and release['draft']))
    reuse = reusable_run(github, sha, current_run) if on_main and not published else ''
    return dict(version=current, release_tag=tag, candidate=candidate, published=published,
                reuse_run=reuse, verify=not (reuse or published),
                base=verified_base(github, sha, root) if on_main and not (reuse or published) else '')


def check_installer(folder):
    name = 'Codexon-Setup.exe'
    checksum = (folder/(name + '.sha256')).read_text(encoding='utf-8-sig').strip()
    expected = digest(folder/name)
    if checksum != f'{expected}  {name}':
        raise ValueError('Installer checksum mismatch')
    return expected


def seal(folder, product, sha, run_id, root=ROOT):
    manifest = json.loads((product/'build-manifest.json').read_text(encoding='utf-8-sig'))
    if manifest['commit'] != sha or manifest['version'] != version(root):
        raise ValueError('Verified product does not match release commit/version')
    if digest(product/'Codexon.exe') != manifest['sha256']:
        raise ValueError('Verified product executable changed')
    if digest(product/'CodexonRecovery.exe') != manifest['recovery_sha256']:
        raise ValueError('Verified recovery executable changed')
    reference = describe(load_lock(root))
    if reference['url'] not in (product/'SOURCE-OFFER.md').read_text(encoding='utf-8'):
        raise ValueError('Packaged corresponding source link does not match the locked sources')
    check_installer(folder)
    shutil.copyfile(product/'build-manifest.json', folder/'build-manifest.json')
    shutil.copyfile(root/f'docs/releases/{manifest["version"]}.md', folder/'RELEASE_NOTES.md')
    receipt = dict(schema=1, commit=sha, version=manifest['version'], run_id=str(run_id),
                   coverage='full', source_key=reference['key'],
                   files={name: digest(folder/name) for name in FILES})
    (folder/'verification.json').write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
    return receipt


def validate_artifact(folder, sha, run_id, root=ROOT):
    receipt = json.loads((folder/'verification.json').read_text(encoding='utf-8'))
    if (receipt.get('schema') != 1 or receipt.get('commit') != sha
            or receipt.get('version') != version(root) or receipt.get('run_id') != str(run_id)
            or receipt.get('coverage') != 'full'
            or receipt.get('source_key') != describe(load_lock(root))['key']):
        raise ValueError('Artifact verification receipt does not match this release')
    if set(receipt['files']) != set(FILES):
        raise ValueError('Incomplete release artifact')
    for name in FILES:
        if digest(folder/name) != receipt['files'][name]:
            raise ValueError('Release artifact changed: ' + name)
    manifest = json.loads((folder/'build-manifest.json').read_text(encoding='utf-8-sig'))
    if manifest['commit'] != sha or manifest['version'] != version(root):
        raise ValueError('Release manifest does not match this commit')
    if ((folder/'RELEASE_NOTES.md').read_text(encoding='utf-8-sig')
            != (root/f'docs/releases/{version(root)}.md').read_text(encoding='utf-8-sig')):
        raise ValueError('Release notes differ from the checked-out commit')
    check_installer(folder)
    return receipt


def publish(github, sha, verified_run, current_run, folder, root=ROOT):
    run = github.api(f'actions/runs/{verified_run}')
    if not trusted(run, sha, github.repository):
        raise ValueError('Untrusted verification run')
    if str(verified_run) == str(current_run):
        if not verification_passed(github, verified_run):
            raise ValueError('Current verification job has not passed')
    elif run.get('status') != 'completed' or not verification_passed(github, verified_run):
        raise ValueError('Previous verification run did not pass')
    github.command('run', 'download', verified_run, '--name', f'verified-release-{sha}', '--dir', folder)
    receipt = validate_artifact(folder, sha, verified_run, root)
    tag = 'v' + receipt['version']
    existing = tag_commit(github, tag)
    if existing and existing != sha:
        raise ValueError('Release tag belongs to another commit')
    reference = ensure_hosted(load_lock(root), github, folder.parent/'third-party-release')
    notes = (folder/'RELEASE_NOTES.md').read_text(encoding='utf-8')
    notes += (f'\n\nThird-party source / 서드파티 소스: [Qt/PySide6 {reference["version"]}]({reference["url"]})'
              f' ([SHA-256]({reference["url"]}.sha256)).\n\n'
              f'Source: {sha}. Full verification: https://github.com/{github.repository}/actions/runs/{verified_run}\n')
    notes_path = folder/'publication-notes.md'
    notes_path.write_text(notes, encoding='utf-8')
    assets = [folder/'Codexon-Setup.exe', folder/'Codexon-Setup.exe.sha256']
    release = github.release(tag)
    if release is None:
        github.command('release', 'create', tag, *assets, '--target', sha, '--title',
                       f'Codexon {receipt["version"]}', '--draft', '--notes-file', notes_path)
        release = github.release(tag)
    hosted = {a['name']: a for a in release['assets']}
    missing = []
    for path in assets:
        if path.name not in hosted:
            missing.append(path)
        elif hosted[path.name].get('digest') != 'sha256:' + digest(path):
            raise ValueError('Existing release asset differs from verified artifact')
    if missing:
        if not release['draft']:
            raise ValueError('Published release is incomplete; refusing to replace its assets')
        github.command('release', 'upload', tag, *missing)
        hosted = {a['name']: a for a in github.release(tag)['assets']}
        if any(hosted.get(path.name, {}).get('digest') != 'sha256:' + digest(path) for path in assets):
            raise ValueError('Uploaded release asset checksum mismatch')
    if release['draft']:
        github.command('release', 'edit', tag, '--notes-file', notes_path, '--draft=false', '--latest')
    return dict(tag=tag, commit=sha, verified_run=verified_run, source=reference['url'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('plan', 'seal', 'publish'))
    parser.add_argument('--repository', default=os.environ.get('GITHUB_REPOSITORY', 'jisoq/Codexon'))
    parser.add_argument('--sha', default=os.environ.get('GITHUB_SHA', ''))
    parser.add_argument('--run-id', default=os.environ.get('GITHUB_RUN_ID', ''))
    parser.add_argument('--event', default=os.environ.get('GITHUB_EVENT_NAME', ''))
    parser.add_argument('--ref', default=os.environ.get('GITHUB_REF', ''))
    parser.add_argument('--publish', choices=('true', 'false'), default='false')
    parser.add_argument('--verified-run')
    parser.add_argument('--folder', type=Path, default=ROOT/'artifacts/release')
    parser.add_argument('--product', type=Path)
    args = parser.parse_args()
    github = GitHub(args.repository)
    if args.action == 'plan':
        result = plan(github, args.sha, args.run_id, args.event, args.ref, args.publish == 'true')
        if os.environ.get('GITHUB_OUTPUT'):
            with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as output:
                for key, value in result.items():
                    output.write(f'{key}={str(value).lower() if isinstance(value, bool) else value}\n')
    elif args.action == 'seal':
        result = seal(args.folder.resolve(), args.product.resolve(), args.sha, args.run_id)
    else:
        result = publish(github, args.sha, args.verified_run, args.run_id, args.folder.resolve())
    print(json.dumps(result))


if __name__ == '__main__':
    main()
