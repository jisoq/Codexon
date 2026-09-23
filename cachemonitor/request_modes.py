"""Extract explicit request-tier overrides, never guess from speed or current settings."""
import re


def request_mode_observation(body):
    strings = []
    def replace(match):
        strings.append(match.group()[1:-1])
        return f'@{len(strings)-1}@'
    # Quoted user/tool content is removed before recognizing structural fields.
    shape = re.sub(r'"(?:\\.|[^"\\])*"', replace, body or '')
    submission = re.search(r'Submission sub=Submission\s*\{\s*id:\s*@(\d+)@,\s*op:\s*TurnInput', shape)
    if not submission:
        return None
    turn = strings[int(submission[1])]
    if not re.fullmatch(r'[a-zA-Z0-9-]{16,64}', turn):
        return None
    result = {'turn':turn,'action':'unchanged','mode':None,'source':'unchanged','configured_service_tier':None}
    settings_update=None
    for structure in ('ThreadSettingsOverrides', 'TurnStartOptions'):
        start = shape.find(structure + ' {', submission.end())
        if start < 0: continue
        start = shape.find('{', start) + 1
        depth, end = 1, start
        while end < len(shape) and depth:
            depth += (shape[end] == '{') - (shape[end] == '}')
            end += 1
        content = shape[start:end-1]
        for match in re.finditer(r'\bservice_tier:\s*(Some\(None\)|Some\((?:Some\()?@(\d+)@\)\)?|None)', content):
            prefix = content[:match.start()]
            if prefix.count('{') != prefix.count('}'): continue
            if match[1]=='None':continue
            if match[1]=='Some(None)':
                result=dict(turn=turn,action='clear',mode='Standard',source='settings_override' if structure=='ThreadSettingsOverrides' else 'turn_override',configured_service_tier=None)
                if structure=='ThreadSettingsOverrides':settings_update={k:result[k] for k in ('action','mode','configured_service_tier')}
                continue
            value = strings[int(match[2])]
            mode = {'priority':'Fast','fast':'Fast','default':'Standard','standard':'Standard'}.get(value,value)
            result=dict(turn=turn,action='set',mode=mode,source='settings_override' if structure=='ThreadSettingsOverrides' else 'turn_override',configured_service_tier=value)
            if structure=='ThreadSettingsOverrides':settings_update={k:result[k] for k in ('action','mode','configured_service_tier')}
    result['settings_update']=settings_update
    return result


def request_mode(body):
    observation=request_mode_observation(body)
    return (observation['turn'],observation['mode']) if observation and observation['action']!='unchanged' else None
