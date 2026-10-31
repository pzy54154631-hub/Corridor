"""Offline HTTP acceptance using the installed product and recorded model replies."""
import ipaddress,json,logging,os,socket,sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT.parent))
for key in list(os.environ):
    if key.startswith(('LLM_','CORRIDOR_LIVE','LIVE_')): os.environ.pop(key,None)
os.environ['CORRIDOR_LOCALE']='zh-CN'
from corridor.agent.llm import LLMClient
from corridor.bench.recording import network_disabled
from corridor.ui import live_config
from acceptance.flows import Browser,serve,instrument,run_flow,semantic_hash,safe_calls,ScenarioFailure

def verify():
    logging.disable(logging.CRITICAL)
    real_connect=socket.socket.connect
    real_connect_ex=socket.socket.connect_ex
    real_dns=socket.getaddrinfo
    real_complete=LLMClient.complete
    provider_attempts=[]
    def denied(*args,**kwargs):
        provider_attempts.append(True)
        raise AssertionError('provider access forbidden')
    def local(host):
        if host=='localhost':return True
        try:return ipaddress.ip_address(host).is_loopback
        except ValueError:return False
    def connect(self,address):
        assert isinstance(address,tuple) and local(address[0]),'external network forbidden'
        return real_connect(self,address)
    def connect_ex(self,address):
        assert isinstance(address,tuple) and local(address[0]),'external network forbidden'
        return real_connect_ex(self,address)
    def dns(host,*args,**kwargs):
        assert local(host),'external DNS forbidden'
        return real_dns(host,*args,**kwargs)
    def complete(self,*args,**kwargs):
        assert self.mode in {'strict','scripted'},'record/live mode forbidden'
        with network_disabled():return real_complete(self,*args,**kwargs)
    total=0;failures=0;recorded_hashes=0
    with ExitStack() as stack:
        for target,value in [('socket.socket.connect',connect),('socket.socket.connect_ex',connect_ex),('socket.getaddrinfo',dns)]:
            stack.enter_context(patch(target,value))
        stack.enter_context(patch.object(LLMClient,'complete',complete))
        for method in ('_live','_responses'):
            stack.enter_context(patch.object(LLMClient,method,denied))
        for entry in json.loads((ROOT/'index.json').read_text(encoding='utf-8')):
            folder=ROOT/entry['directory']
            source=json.loads((folder/'live.json').read_text(encoding='utf-8'))
            previous=json.loads((folder/'recorded-replay.json').read_text(encoding='utf-8'))
            item=live_config.deployment(entry['deployment'])
            with serve() as app:
                for index,old in enumerate(source['cases']):
                    prior=previous['cases'][index]
                    page=Browser(app,view='judge',locale='zh-CN');instrument(page);page.login()
                    journal=(folder/'journal'/Path(old['journal_dir'])).resolve()
                    assert journal.is_relative_to(folder.resolve()/'journal'),'invalid recording path'
                    client=LLMClient(mode='strict',replay_dir=journal,model=item.model,api=item.api,
                        provider=item.provider,api_base=item.api_base,assumed_price_in=item.price_in,
                        assumed_price_out=item.price_out,reasoning_effort=item.reasoning_effort,
                        max_output_tokens=item.max_output_tokens,env_file=None)
                    def attach():
                        page.ws.controller.set_client(client);page.ws.session.client=client
                    app.settings['served'].call(attach)
                    before=page.ws.controller.gateway.bank.execute_count
                    failure=None;semantic=None
                    try:semantic=run_flow(page,index)
                    except Exception as exc:failure=type(exc).__name__
                    assert client.api_calls==0 and not provider_attempts,'provider calls must be zero'
                    calls=safe_calls(client)
                    expected=prior['planner_calls']
                    assert len(calls)==len(expected),f'recorded call count mismatch: case {index}; got {len(calls)} expected {len(expected)}; failure {failure}; sources {[x.get("source") for x in calls]}'
                    for a,b in zip(calls,expected):
                        for key in ('request_hash','output_sha256','source','failed','error_type'):
                            assert a.get(key)==b.get(key),'strict call mismatch: '+key
                        recorded_hashes+=int(bool(a.get('output_sha256')))
                    if prior['passed']:
                        assert failure is None and semantic is not None,'expected successful business flow'
                        assert semantic_hash(semantic)==old['business_semantic_hash'],'business hash mismatch'
                        assert semantic['receipts']==old['business']['receipts'],'receipt mismatch'
                        assert semantic['audit_valid'],'audit mismatch'
                    else:
                        assert failure==prior['failure_type'],'failure classification mismatch'
                        obs=prior['failure_observation']
                        assert page.ws.controller.gateway.bank.execute_count-before==obs['bank_execute_delta']
                        assert len(page.ws.controller.state.receipts)==obs['receipt_count']
                        assert page.ws.controller.gateway.verify_log()==obs['audit_valid']
                        failures+=1
                    total+=1
            print(f"Acceptance {entry['deployment']}: 6/6 recorded outcomes reproduced; business success {previous['passed_count']}/6; provider calls 0")
    assert total>=30,'missing deployments'
    print(f'Acceptance: {total} outcomes; {failures} preserved failures; {recorded_hashes} output hashes; provider calls 0')
    return total
if __name__=='__main__':verify()
