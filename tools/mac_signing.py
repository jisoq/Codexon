"""Run one CI build with temporary Developer ID and notarization credentials.

Credentials arrive only in environment variables. P12 passwords go to OpenSSL
over stdin; private key contents stay in mode-0600 temporary files. The isolated
keychain has a non-secret empty password, a private filesystem path and an ACL
limited to codesign. No private value is placed in a process argument or log.
For local releases, prefer mac_build.py with an existing Keychain profile.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile

SECRET_NAMES=('CODEXON_DEVELOPER_ID_P12_BASE64','CODEXON_DEVELOPER_ID_P12_PASSWORD',
              'CODEXON_NOTARY_KEY_BASE64','CODEXON_NOTARY_KEY_ID','CODEXON_NOTARY_ISSUER_ID')


def command(arguments, *, env, input=None):
    result=subprocess.run(arguments,input=input,capture_output=True,env=env,timeout=120)
    if result.returncode:
        # Neither credentials nor tool diagnostics are emitted on failure.
        raise RuntimeError('The temporary signing credential operation failed: '+Path(arguments[0]).name)
    return result.stdout.decode('utf-8',errors='strict')


def private_file(path, data):
    with os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'wb') as file:
        file.write(data)


@contextmanager
def signing_environment(environment=None):
    values=dict(os.environ if environment is None else environment)
    missing=[name for name in SECRET_NAMES if name not in values
             or (name!='CODEXON_DEVELOPER_ID_P12_PASSWORD' and not values[name])]
    team=values.get('CODEXON_TEAM_ID','')
    if missing or not re.fullmatch('[A-Z0-9]{10}',team):
        raise ValueError('Configure the Developer ID, notarization API key and expected team before a signed build.')
    if '\n' in values[SECRET_NAMES[1]] or '\r' in values[SECRET_NAMES[1]]:
        raise ValueError('The P12 password must be a single line.')
    clean={key:value for key,value in values.items() if key not in SECRET_NAMES}
    prior=shlex.split(command(['/usr/bin/security','list-keychains','-d','user'],env=clean))
    with tempfile.TemporaryDirectory(prefix='codexon-signing-',dir=values.get('RUNNER_TEMP')) as folder:
        root=Path(folder)
        p12=root/'identity.p12';pem=root/'identity.pem';key=root/'notary.p8';keychain=root/'build.keychain-db'
        created=False;changed=False
        try:
            try:
                private_file(p12,base64.b64decode(values[SECRET_NAMES[0]],validate=True))
                private_file(key,base64.b64decode(values[SECRET_NAMES[2]],validate=True))
            except ValueError:
                raise ValueError('Signing credentials must use valid base64 encoding.') from None
            private_file(pem,b'')
            command(['/usr/bin/openssl','pkcs12','-in',str(p12),'-nodes','-passin','stdin','-out',str(pem)],
                    env=clean,input=(values[SECRET_NAMES[1]]+'\n').encode())
            command(['/usr/bin/security','create-keychain','-p','',str(keychain)],env=clean);created=True
            keychain.chmod(0o600)
            command(['/usr/bin/security','set-keychain-settings','-lut','21600',str(keychain)],env=clean)
            command(['/usr/bin/security','import',str(pem),'-f','pemseq','-k',str(keychain),
                     '-x','-T','/usr/bin/codesign'],env=clean)
            command(['/usr/bin/security','set-key-partition-list','-S','apple-tool:,apple:,codesign:',
                     '-s','-k','',str(keychain)],env=clean)
            command(['/usr/bin/security','list-keychains','-d','user','-s',str(keychain),*prior],env=clean);changed=True
            identities=command(['/usr/bin/security','find-identity','-v','-p','codesigning',str(keychain)],env=clean)
            matches=re.findall(r'\b([0-9A-F]{40}) "Developer ID Application: [^"\n]+ \('+team+r'\)"',identities)
            if len(matches)!=1:raise ValueError('Exactly one Developer ID Application identity from the expected team is required.')
            profile='codexon-build'
            command(['/usr/bin/xcrun','notarytool','store-credentials',profile,'--key',str(key),
                     '--key-id',values['CODEXON_NOTARY_KEY_ID'],'--issuer',values['CODEXON_NOTARY_ISSUER_ID'],
                     '--keychain',str(keychain)],env=clean)
            # Files containing exported private keys are no longer needed.
            for path in (p12,pem,key):path.unlink()
            yield {**clean,'CODEXON_SIGNING_IDENTITY':matches[0],
                   'CODEXON_NOTARY_PROFILE':profile,'CODEXON_NOTARY_KEYCHAIN':str(keychain)}
        finally:
            try:
                if changed:command(['/usr/bin/security','list-keychains','-d','user','-s',*prior],env=clean)
            finally:
                if created:command(['/usr/bin/security','delete-keychain',str(keychain)],env=clean)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',nargs=argparse.REMAINDER)
    args=parser.parse_args()
    selected=args.command[1:] if args.command[:1]==['--'] else args.command
    if sys.platform!='darwin' or not selected:parser.error('Use on macOS with -- followed by the build command.')
    try:
        with signing_environment() as env:
            return subprocess.run(selected,env=env).returncode
    except (ValueError,RuntimeError):
        print('Signed build failed; verify the CI signing credentials and Developer ID team.',file=sys.stderr)
        return 1


if __name__=='__main__':raise SystemExit(main())
