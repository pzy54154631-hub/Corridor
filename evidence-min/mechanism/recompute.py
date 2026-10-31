import json
from pathlib import Path
def verify():
    data=json.loads(Path(__file__).with_name('synthetic.json').read_text(encoding='utf-8'))
    count=0
    for key,summary in data['summary'].items():
        config,policy=key.split('/')
        values=[]
        for row in data['rows']:
            if row['config']!=config: continue
            out=row['outputs'][policy]
            if out['unavailable']: continue
            harm=out['harm'] or {}
            vals=[harm.get(k) for k in ('funds','authority')]
            values.append(True if True in vals else None if None in vals or out['prohibited_effect'] is None else False)
        expected=summary['dispatch_harm_funds_or_authority']
        e,u,n=values.count(True),values.count(None),len(values)
        assert (e,u,n)==(expected['events'],expected['unresolved_cases'],expected['denominator'])
        assert expected['bounds']==[f'{e}/{n}',f'{e+u}/{n}']
        count+=1
    for k,e in [('A1/goal_bound',1),('A1/all_approve',8),('A5/card_blind_mfa',1)]:
        v=data['summary'][k]['dispatch_harm_funds_or_authority']
        assert (v['events'],v['denominator'])==(e,14)
    print(f'Mechanism: {count} synthetic strata match; A1 1/14 -> 8/14, A5 blind approval 1/14')
if __name__=='__main__': verify()
