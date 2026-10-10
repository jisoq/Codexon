"""Fail early when source/test imports or installed versions differ from the CI lock."""
from __future__ import annotations

import ast
import importlib.metadata as metadata
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]


def normalized(name):
    return re.sub(r'[-_.]+', '-', name).lower()


def check_environment(root=ROOT, *, distributions=None, installed_version=None):
    root=Path(root)
    distributions=metadata.packages_distributions() if distributions is None else distributions
    installed_version=installed_version or metadata.version
    lock={}
    for line in (root/'requirements-release.lock').read_text(encoding='utf-8-sig').splitlines():
        line=line.strip()
        if not line or line.startswith('#'):continue
        name, version=line.split('==',1)
        lock[normalized(name)]=(name,version)
    errors=[]
    for name,wanted in lock.values():
        try:actual=installed_version(name)
        except metadata.PackageNotFoundError:actual=None
        if actual!=wanted:
            errors.append(dict(kind='missing_distribution' if actual is None else 'version_mismatch',
                               package=name,expected=wanted,actual=actual))
    files=[p for folder in ('cachemonitor','tests','tools') for p in (root/folder).rglob('*.py')]
    local={'cachemonitor','tests','tools'}|{p.stem for p in files}
    for path in files:
        relative=path.relative_to(root).as_posix()
        try:tree=ast.parse(path.read_text(encoding='utf-8-sig'),filename=relative)
        except SyntaxError as error:
            errors.append(dict(kind='syntax_error',file=relative,line=error.lineno,message=error.msg));continue
        # Walk function bodies too: a local-only dependency can otherwise fail late in CI.
        for node in ast.walk(tree):
            if isinstance(node,ast.Import):names=[alias.name.split('.')[0] for alias in node.names]
            elif isinstance(node,ast.ImportFrom) and node.level==0:names=[(node.module or '').split('.')[0]]
            else:continue
            for name in names:
                if not name or name in local or name in sys.stdlib_module_names:continue
                owners=distributions.get(name,())
                if not any(normalized(owner) in lock for owner in owners):
                    errors.append(dict(kind='unlocked_import',module=name,file=relative,line=node.lineno))
    return dict(python=sys.version.split()[0],executable=sys.executable,
                lock=str(root/'requirements-release.lock'),checked_files=len(files),errors=errors)


def main():
    result=check_environment()
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 1 if result['errors'] else 0


if __name__=='__main__':raise SystemExit(main())
