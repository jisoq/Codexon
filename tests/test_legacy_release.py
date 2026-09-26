import json
from zipfile import ZipFile

import pytest

from tools import prepare_legacy_release as legacy


def test_public_payload_requires_exact_manifest_and_executable(tmp_path):
    path=tmp_path/'app';path.mkdir();exe=path/'Codexon.exe';exe.write_bytes(b'published')
    manifest=dict(product='Codexon',version='2026.09.25.6',sha256=legacy.digest(exe))
    (path/'build-manifest.json').write_text(json.dumps(manifest))
    assert legacy.product(tmp_path,'2026.09.25.6')[0]==exe
    exe.write_bytes(b'rebuilt same version')
    with pytest.raises(ValueError,match='checksum'):legacy.product(tmp_path,'2026.09.25.6')


def test_extractor_is_pinned_and_archive_paths_are_not_used(tmp_path,monkeypatch):
    archive=tmp_path/'tool.zip';output=tmp_path/'verified.exe'
    with ZipFile(archive,'w') as zipped:
        zipped.writestr('innounp.exe',b'verified tool')
        zipped.writestr('../must-not-extract.txt',b'not extracted')
    with pytest.raises(ValueError,match='checksum'):legacy.unpack_tool(archive,output)
    assert not output.exists()
    monkeypatch.setattr(legacy,'EXTRACTOR_SHA256',legacy.digest(archive))
    legacy.unpack_tool(archive,output)
    assert output.read_bytes()==b'verified tool'
    assert not (tmp_path.parent/'must-not-extract.txt').exists()
