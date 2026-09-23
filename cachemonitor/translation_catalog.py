"""Translation data and saved language, usable without the desktop Qt runtime."""
import json
import os
from pathlib import Path

CATALOG = json.loads((Path(__file__).parent/'assets/i18n/en.json').read_text(encoding='utf-8'))


def saved_language():
    value = os.environ.get('CODEXON_LANGUAGE')
    if value is None and os.name=='nt':
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\CacheMonitor\CacheMonitor\ui') as key:
                value = winreg.QueryValueEx(key, 'language')[0]
        except OSError:pass
    return 'en' if value=='en' else 'ko'


def translate(value, language=None):
    if (language or saved_language())!='en':return value
    if value in CATALOG:return CATALOG[value]
    for source in sorted(CATALOG,key=len,reverse=True):
        if source in value:value=value.replace(source,CATALOG[source])
    return value
