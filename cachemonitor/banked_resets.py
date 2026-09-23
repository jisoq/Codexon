"""Read-only earned reset information; the server count is authoritative."""
import math
from datetime import datetime


def timestamp(value):
    return value if type(value) in (int,float) and math.isfinite(value) and 0<value<32503680000 else None


def normalize_reset_credits(value):
    if not isinstance(value,dict):return None
    count=value.get('availableCount')
    if type(count) is not int or count<0:return None
    rows=value.get('credits')
    details=None
    if isinstance(rows,list):
        details=[];seen=set()
        for row in rows:
            if not isinstance(row,dict):continue
            identity=row.get('id')
            if isinstance(identity,str) and identity:
                if identity in seen:continue
                seen.add(identity)
            details.append({
                'title':row.get('title') if isinstance(row.get('title'),str) else None,
                'description':row.get('description') if isinstance(row.get('description'),str) else None,
                'status':row.get('status') if isinstance(row.get('status'),str) else None,
                'reset_type':row.get('resetType') if isinstance(row.get('resetType'),str) else None,
                'granted_at':timestamp(row.get('grantedAt')),'expires_at':timestamp(row.get('expiresAt'))})
    return {'available_count':count,'credits':details}


def reset_credit_display(quota,now):
    info=(quota or {}).get('reset_credits')
    if not info:
        return {'count':'—','note':'계정에서 정보를 제공하지 않았습니다' if (quota or {}).get('source')=='live' else '사용 초기화권 조회 대기','sections':[]}
    observed=(quota or {}).get('observed_at',0)
    count=info['available_count']
    if not observed or not 0<=now-observed<90:
        return {'count':'—','note':f'마지막 확인 {count:,}개 · 조회 갱신 대기','sections':[]}
    rows=info.get('credits')
    available=[r for r in rows or [] if r.get('status')=='available']
    deadlines=[r['expires_at'] for r in available if r.get('expires_at')]
    def date(value):return datetime.fromtimestamp(value).strftime('%Y/%m/%d %H:%M')
    if count==0:note='보유한 사용 초기화권이 없습니다'
    elif deadlines:
        earliest=min(deadlines)
        label='가장 빠른 만료 ' if len(deadlines)==count else '확인된 만료 '
        note=label+date(earliest) if earliest>now else '만료 시각 경과 · 새 조회 확인 중'
    else:note='상세 정보 미제공' if not rows else '만료일 미제공'
    sections=[]
    for i,row in enumerate(rows or [],1):
        status={'available':'사용 가능','redeemed':'사용 완료','consumed':'사용 완료','expired':'만료됨'}.get(row.get('status'),row.get('status') or '상태 미제공')
        lines=[f'상태 · {status}', '지급 · '+(date(row['granted_at']) if row.get('granted_at') else '정보 미제공'),
               '만료 · '+(date(row['expires_at']) if row.get('expires_at') else '정보 미제공')]
        if row.get('reset_type')=='codexRateLimits':lines.append('대상 · Codex 사용량 한도')
        if row.get('description'):lines.append(row['description'])
        sections.append((row.get('title') or f'사용 초기화권 {i}','\n'.join(lines)))
    if rows is not None and len(available)<count:
        note+=f'\n보유 {count:,}개 중 상세 {len(available):,}개 제공'
    return {'count':f'{count:,}개','note':note,'sections':sections}
