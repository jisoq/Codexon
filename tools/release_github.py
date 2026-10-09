"""GitHub CLI boundary shared by release preparation and publication."""
import json
import re
import subprocess


class GitHub:
    def __init__(self, repository):
        if not re.fullmatch(r'[\w.-]+/[\w.-]+', repository):
            raise ValueError('Invalid GitHub repository')
        self.repository = repository

    def api(self, path, *, missing=False):
        result = subprocess.run(['gh', 'api', f'repos/{self.repository}/{path}'],
                                capture_output=True, text=True, encoding='utf-8')
        if result.returncode:
            if missing and 'HTTP 404' in result.stderr:
                return None
            raise RuntimeError(result.stderr.strip())
        return json.loads(result.stdout)

    def release(self, tag):
        return self.api('releases/tags/' + tag, missing=True)

    def asset_text(self, asset):
        if not 0 < asset['size'] <= 32768:
            raise ValueError('Release metadata asset is too large')
        return subprocess.check_output(['gh', 'api', '-H', 'Accept: application/octet-stream',
                                        asset['url']], text=True, encoding='utf-8')

    def command(self, *args):
        subprocess.run(['gh', *map(str, args), '--repo', self.repository], check=True)
