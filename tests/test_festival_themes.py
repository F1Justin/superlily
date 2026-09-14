from datetime import datetime, timedelta, timezone
from hashlib import sha256
from io import BytesIO
import ast
from pathlib import Path
import os
import xml.etree.ElementTree as ET

import httpx
import pytest
from PIL import Image
from superlily_contracts import RenderDocument
from superlily_contracts.festival_themes import CST, SCHEDULE, PALETTES, theme_window
from superlily_core import render_service
from superlily_core.app import create_app
from superlily_core.settings import Settings
from superlily_core.models import BotInstance
from superlily_core.document_renderer_client import DocumentRendererClient, RenderedDocument
from superlily_latex_provider.worker import document_latex
from superlily_latex_provider.festival_renderer import ASSETS, decorate_document

@pytest.mark.parametrize('date,theme_id', SCHEDULE)
def test_exact_24_hour_window(date, theme_id):
    start=datetime.fromisoformat(date).replace(tzinfo=CST)
    end=start+timedelta(days=1)
    assert theme_window(start-timedelta(microseconds=1)).theme_id != theme_id
    for now in [start, start+timedelta(hours=12), end-timedelta(microseconds=1)]:
        chosen=theme_window(now.astimezone(timezone.utc))
        assert chosen.theme_id == theme_id
        assert chosen.valid_until == end
    assert (end-start).total_seconds() == 86400
    assert theme_window(end).theme_id != theme_id


def test_calendar_is_bounded_and_switch_can_disable_it():
    assert theme_window(datetime(2027,2,6,tzinfo=CST)).theme_id == 'new_years_day'
    assert theme_window(datetime(2027,2,6,tzinfo=CST),enabled=False).theme_id == 'default'
    assert theme_window(datetime(2027,9,25,tzinfo=CST)).theme_id == 'default'
    assert theme_window(datetime(2027,2,21,tzinfo=CST)).valid_until is None
    with pytest.raises(ValueError): theme_window(datetime(2027,2,6))


def doc():
    return RenderDocument(instance_id='nekro-agent',conversation_key='onebot_v11-group_123',title='标题',blocks=[
        {'kind':'heading','node_id':'h','text':'节日正文','level':1},
        {'kind':'text','node_id':'p','text':'正常文字 **加粗**'},
        {'kind':'math','node_id':'m','latex':r'\frac{1}{2}'},
    ])

@pytest.mark.parametrize('theme_id',list(PALETTES)[1:])
def test_theme_colors_structural_headings_and_keeps_math(theme_id):
    tex=document_latex(doc(),theme_id=theme_id)
    assert tex.count(r'\color{festivalHeading}') == 2
    assert r'\frac{1}{2}' in tex
    assert r'\pagecolor{festivalBackground}\color{festivalBody}' in tex
    assert PALETTES[theme_id][0] in tex
    stream=BytesIO();Image.new('RGB',(2048,2048),'#'+PALETTES[theme_id][0]).save(stream,format='PNG')
    result=Image.open(BytesIO(decorate_document(stream.getvalue(),theme_id)))
    assert max(result.size)<=2048
    assert result.getpixel((result.width//2,result.height//2)) == tuple(bytes.fromhex(PALETTES[theme_id][0]))
    for edge in ['top','bottom']:
        svg=ET.fromstring((ASSETS/f'{theme_id}-{edge}.svg').read_bytes())
        assert all(not (el.text or '').strip() for el in svg.iter())


def test_default_is_byte_preserving_and_uncolored():
    assert decorate_document(b'untouched','default')==b'untouched'
    assert 'festival' not in document_latex(doc())


@pytest.mark.asyncio
@pytest.mark.parametrize('cross_during_render',[False,True])
async def test_core_renews_cross_midnight_and_fences_old_delivery(tmp_path,monkeypatch,cross_during_render):
    clock=[datetime(2027,2,5,23,59,59,tzinfo=CST)]
    monkeypatch.setattr(render_service,'utc_now',lambda:clock[0])
    themes=[]
    async def render(self,document,*,timeout_seconds):
        themes.append(self.theme_id)
        stream=BytesIO();Image.new('RGB',(8,8),'#'+PALETTES[self.theme_id][0]).save(stream,format='PNG')
        body=stream.getvalue()
        if cross_during_render and len(themes)==1:clock[0]+=timedelta(seconds=1)
        return RenderedDocument(body,sha256(body).hexdigest(),8,8)
    monkeypatch.setattr(DocumentRendererClient,'render_document',render)
    settings=Settings(database_url=os.getenv('SUPERLILY_TEST_DATABASE_URL',f'sqlite+aiosqlite:///{tmp_path}/core.db'),ingest_tokens={'nekro-agent':'secret'},artifact_root=str(tmp_path/'artifacts'),artifact_secret_pepper='p'*32,render_mode='all',render_backend_url='http://renderer:8000',render_backend_token='r'*32,render_implementation_hash='1'*64,render_festival_enabled=True)
    app=create_app(settings);await app.state.database.create_schema()
    try:
        async with app.state.database.sessions() as session:
            session.add(BotInstance(id='nekro-agent',platform='qq',adapter='onebot_v11',bot_id='123',role='talk',metadata_json={'capabilities':{'profile':'onebot_v11.qq.v1','supported':['send_image'],'limits':{}}}));await session.commit()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            headers={'Authorization':'Bearer secret','Idempotency-Key':'same-document'}
            first=await client.post('/v1/render-documents',json=doc().model_dump(mode='json'),headers=headers)
            assert first.status_code==201,first.text
            if cross_during_render:
                assert themes==['new_years_eve','new_years_day']
            else:
                assert datetime.fromisoformat(first.json()['expires_at'])==datetime(2027,2,6,tzinfo=CST)
                clock[0]+=timedelta(seconds=1)
                expired=await client.post(f"/v1/render-artifacts/{first.json()['artifact_id']}/delivery-intents",json={'instance_id':'nekro-agent','delivery_plan_id':first.json()['delivery_plan_id'],'idempotency_key':'never-sent'},headers=headers)
                assert expired.headers['X-Render-Error-Code']=='artifact_expired'
            again=await client.post('/v1/render-documents',json=doc().model_dump(mode='json'),headers=headers)
            assert again.status_code in {200,201},again.text
            assert themes==['new_years_eve','new_years_day']
            if not cross_during_render:assert first.json()['artifact_id']!=again.json()['artifact_id']
    finally:
        await app.state.database.drop_schema();await app.state.database.dispose()


def test_bridge_deadline_is_exclusive_and_rejects_ambiguous_timestamps():
    path=Path('bridges/nekro/superlily_bridge/__init__.py')
    tree=ast.parse(path.read_text());node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_render_receipt_expired')
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):return datetime(2027,2,6,tzinfo=CST)
    env={'datetime':Clock,'timezone':timezone,'Any':object}
    exec(compile(ast.Module(body=[node],type_ignores=[]),str(path),'exec'),env)
    expired=env['_render_receipt_expired']
    assert expired({'expires_at':'2027-02-06T00:00:00+08:00'})
    assert not expired({'expires_at':'2027-02-06T00:00:00.000001+08:00'})
    assert expired({'expires_at':'2027-02-06T00:00:00'})
    assert expired({'expires_at':'bad'})

@pytest.mark.asyncio
async def test_bridge_renews_only_a_definitely_unsent_expired_image():
    import asyncio
    from contextvars import ContextVar
    from types import SimpleNamespace
    path=Path('bridges/nekro/superlily_bridge/__init__.py')
    nodes=[n for n in ast.parse(path.read_text()).body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name in {'_render_receipt_expired','_deliver_render_request'}]
    clock=[datetime(2027,2,5,23,59,59,tzinfo=CST)]
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):return clock[0]
    context=ContextVar('receipt')
    body=b'\x89PNG\r\n\x1a\ncontent'
    calls=[];completions=[];sent=[];intent_keys=[]
    class Client:
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def post(self,url,*,json,headers):
            if url=='/v1/markdown-documents':
                calls.append(url);number=len(calls)
                deadline=clock[0]+timedelta(seconds=1 if number==1 else 3600)
                data={'artifact_id':str(number),'content_sha256':sha256(body).hexdigest(),'content_path':f'/v1/render-artifacts/{number}/content','delivery_plan_id':str(number),'delivery_plan':{'selected_family':'image'},'expires_at':deadline.isoformat()}
            elif url.endswith('/delivery-intents'):
                intent_keys.append(json['idempotency_key']);data={'intent_id':str(len(calls)),'should_send':True}
            elif url.endswith('/complete'):
                completions.append(json);data={}
            else:raise AssertionError(url)
            return httpx.Response(200,json=data,request=httpx.Request('POST','http://test'+url))
        async def get(self,url,**kwargs):return httpx.Response(200,content=body,request=httpx.Request('GET','http://test'+url))
    async def stage(content,**kwargs):
        if len(calls)==1:clock[0]+=timedelta(seconds=1)
        return 'staged-image'
    async def send(image):
        sent.append(image);context.get().set_result({'outcome':'succeeded','platform_message_id':'42','safe_error_code':None})
    env={'datetime':Clock,'timezone':timezone,'Any':object,'AgentCtx':object,'json':__import__('json'),'hashlib':__import__('hashlib'),'asyncio':asyncio,'httpx':SimpleNamespace(AsyncClient=lambda **kw:Client(),Timeout=httpx.Timeout,HTTPError=httpx.HTTPError,HTTPStatusError=httpx.HTTPStatusError),'config':SimpleNamespace(CORE_TOKEN='secret',CORE_URL='http://test',INSTANCE_ID='nekro-agent',RENDER_TIMEOUT_SECONDS=40),'stable_key':lambda *args:'stable','_render_send_receipt':context,'RENDER_SUPPRESSED':'suppressed','RenderRetryRequired':RuntimeError,'unavailable_instruction':lambda:'unavailable','logger':SimpleNamespace(exception=lambda *a:None,warning=lambda *a:None)}
    exec(compile(ast.Module(body=nodes,type_ignores=[]),str(path),'exec'),env)
    result=await env['_deliver_render_request'](SimpleNamespace(chat_key='qq',fs=SimpleNamespace(mixed_forward_file=stage),send_image=send),endpoint='/v1/markdown-documents',payload={},request_context='source')
    assert 'INTERNAL_RENDER_DELIVERED' in result
    assert len(calls)==2 and len(sent)==1
    assert [c['outcome'] for c in completions]==['failed','succeeded']
    assert completions[0]['safe_error_code']=='theme_window_expired'
    assert intent_keys==['nekro-delivery:stable','nekro-delivery:stable:theme-renewal']


def test_pdf_rounding_does_not_leave_a_white_rule_on_dark_paper():
    image=Image.new("RGB",(100,70),"#B33328")
    image.paste("white",(0,69,100,70))
    buffer=BytesIO();image.save(buffer,format="PNG")
    result=Image.open(BytesIO(decorate_document(buffer.getvalue(),"new_years_day")))
    x=result.width//2
    # No text or decoration occupies the central vertical strip.
    assert all(result.getpixel((x,y))==(179,51,40) for y in range(result.height))
