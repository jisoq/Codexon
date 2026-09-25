VERSION = '2026.09.25.13'

# The independently running proxy changes only when its own implementation or
# compatibility contract changes. Earlier releases since 2026.09.21.9 contain
# the same proxy implementation but reported the desktop app version.
PROXY_VERSION = '2026.09.25.11'
_LEGACY_PROXY_MIN = (2026, 9, 21, 9)
_LEGACY_PROXY_MAX = (2026, 9, 23, 2)


def proxy_compatible(version):
    if version in (PROXY_VERSION, '2026.09.25.10', '2026.09.25.9', '2026.09.25.8', '2026.09.25.7', '2026.09.25.6', '2026.09.25.5', '2026.09.25.4', '2026.09.25.3', '2026.09.24.6', '2026.09.23.3', '2026.09.23.7', '2026.09.23.9'):
        return True
    if not isinstance(version, str):
        return False
    parts = version.split('.')
    if len(parts) != 4 or not all(part.isdigit() for part in parts):
        return False
    return _LEGACY_PROXY_MIN <= tuple(map(int, parts)) <= _LEGACY_PROXY_MAX
