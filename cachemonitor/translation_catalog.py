"""Translation data and saved language, usable without the desktop Qt runtime."""
import json
import os
from pathlib import Path

CATALOG = json.loads((Path(__file__).parent/'assets/i18n/en.json').read_text(encoding='utf-8'))
CATALOG.update({
    '사용 기록 준비 중': 'Preparing usage records',
    '사용 기록 수집 중': 'Collecting usage records',
    '사용량 계산 중': 'Calculating usage',
    '사용량 반영 중': 'Applying usage results',
    '저장된 사용 기록을 확인한 뒤 사용량을 표시합니다.': 'Usage will appear after saved records are checked.',
    '저장된 Codex 사용 기록을 읽고 있습니다.': 'Reading saved Codex usage records.',
    '확인한 기록으로 사용량을 계산하고 있습니다.': 'Calculating usage from the collected records.',
    '확인한 사용량을 화면에 반영하고 있습니다.': 'Applying the calculated usage to the display.',
    '확인한 파일 {count}개': 'Files checked {count}',
    '처리한 세션 {count}개': 'Sessions processed {count}',
    '준비하는 동안 다른 탭을 이용할 수 있습니다.': 'You can use other tabs while preparation continues.',
})


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
