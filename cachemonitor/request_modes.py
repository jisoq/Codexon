"""Resolve historical request modes and explicit local parent inheritance."""
from bisect import bisect_right
from math import isfinite
import re

from .pricing import request_tier


def mode_parents(sessions):
    indexed={(s['home'],s['id']):s for s in sessions}
    parents={key:(key[0],s['parent_thread_id']) for key,s in indexed.items()
             if isinstance(s.get('parent_thread_id'),str) and (key[0],s['parent_thread_id']) in indexed}
    valid={}
    for key in parents:
        seen={key};ancestor=parents[key]
        while ancestor not in seen and ancestor in parents:
            seen.add(ancestor);ancestor=parents[ancestor]
        if ancestor not in seen:valid[key]=indexed[parents[key]]
    return valid


def mode_time(session,row):
    turn=session.get('turn_records',{}).get(row.get('turn'),{})
    return next((value for value in (row.get('request_observed_at'),turn.get('started_at'),row.get('ts'))
                 if type(value) in (int,float) and isfinite(value)),None)


class ParentModeTimeline:
    """Use the parent's mode at the call's start, never a later setting."""
    def __init__(self,session,modes,parent=None):
        events={}
        by_turn={}
        for row in session['history']:
            by_turn.setdefault(row.get('turn'),[]).append(row)
        for turn,record in session.get('turn_records',{}).items():
            at=record.get('started_at')
            if type(at) not in (int,float) or not isfinite(at):continue
            rows=by_turn.get(turn,[])
            mode=modes.get((session['home'],session['id'],turn),'미확인')
            # A call's wire observation begins at that call, not at the start of
            # an earlier turn. Settings evidence can cover the whole turn.
            settings={request_tier(r) for r in rows if r.get('service_tier_source') not in ('wire','parent')}
            if mode=='미확인' and len(settings)==1:mode=settings.pop()
            if mode=='미확인' and parent is not None and not any(r.get('mode_conflict') for r in rows):
                mode=parent.at(at)
            events[at]={mode}
        calls={}
        for row in session['history']:
            at=mode_time(session,row)
            if at is not None:
                calls.setdefault(at,set()).add(request_tier(row))
        events.update(calls)
        self.times=sorted(events)
        self.modes=[next(iter(events[at])) if len(events[at])==1 else '미확인' for at in self.times]

    def at(self,at):
        if at is None:return '미확인'
        index=bisect_right(self.times,at)-1
        return self.modes[index] if index>=0 else '미확인'

    def inherit(self,session,row):
        if request_tier(row)!='미확인' or row.get('mode_conflict') or row.get('service_tier_source')=='conflict':
            return row
        mode=self.at(mode_time(session,row))
        if mode not in ('Standard','Fast'):return row
        return dict(row,service_tier=mode,service_tier_source='parent',
                    request_mode_source='parent',request_mode_action='inherit',mode_evidence='부모 요청 모드 상속')


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
