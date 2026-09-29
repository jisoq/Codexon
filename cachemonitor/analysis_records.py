"""Store each call fact once; restore display aliases at view boundaries."""


def aliases(row):
    return dict(call_id=row.get('key'),request_id=row.get('turn') or '',
                analysis_model=row.get('model'),price_model=row.get('model'),
                price_tier=row.get('service_tier'),price_assumed=False,long_context=False,
                cache_degradation=bool(row.get('cache_incident_id')))


def compact_record(row):
    """Retain differing prices and flags; remove only exact duplicate values."""
    redundant={key for key,value in aliases(row).items()
               if key in row and type(row[key]) is type(value) and row[key]==value}
    return {key:value for key,value in row.items() if key not in redundant} if redundant else row


def record_items(row):
    yield from row.items()
    if 'session_ordinal' in row and 'cost' in row and 'key' in row:
        for key,value in aliases(row).items():
            if key not in row:
                yield key,value


def public_record(row):
    result=dict(row)
    if 'session_ordinal' in row and 'cost' in row and 'key' in row:
        for key,value in aliases(row).items():
            result.setdefault(key,value)
    return result


def public_records(value):
    """Expand only records on the bounded view wire, preserving shared objects."""
    memo={}
    def expand(item):
        if not isinstance(item,(dict,list,tuple)):
            return item
        identity=id(item)
        if identity in memo:
            return memo[identity]
        if isinstance(item,dict):
            if 'session_ordinal' in item and 'cost' in item and 'key' in item:
                result=public_record(item)
            else:
                result=item
                for key,child in item.items():
                    expanded=expand(child)
                    if expanded is not child:
                        if result is item:result=dict(item)
                        result[key]=expanded
        else:
            values=[expand(child) for child in item]
            result=(tuple(values) if isinstance(item,tuple) else values) if any(a is not b for a,b in zip(values,item)) else item
        memo[identity]=result
        return result
    return expand(value)
