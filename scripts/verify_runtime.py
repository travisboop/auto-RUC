#!/usr/bin/env python3
"""Read-only acceptance through the public MCP interface; save counts, never values."""
import argparse
import base64
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from ruc_agent.data import BEIJING


async def verify(args):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    results = []
    report = {'checkedAt': datetime.now(BEIJING).isoformat(timespec='seconds'),
              'transport': 'official_mcp_client_to_stdio_server', 'writeEnabled': False,
              'personalValuesSaved': False, 'results': results}
    start = perf_counter()
    try:
        parameters = StdioServerParameters(command=sys.executable, args=[str(ROOT/'scripts/serve.py'),
                                           '--transport','mcp','--credentials-file',args.credentials_file])
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                report['toolCount'] = len((await session.list_tools()).tools)

                async def call(name, values):
                    answer = await session.call_tool(name, values)
                    data = answer.structured_content
                    if data.get('code') == 'CAPTCHA_REQUIRED' and args.challenge_dir:
                        # The driver keeps the official MCP session alive while
                        # a human or an image-capable agent reads this challenge.
                        images = [item for item in answer.content if item.type == 'image']
                        if len(images) != 1:
                            raise RuntimeError('CAPTCHA_IMAGE_MISSING')
                        directory = Path(args.challenge_dir).expanduser()
                        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                        challenge = directory/'legacy-captcha.png'
                        fd = os.open(challenge, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                        with os.fdopen(fd,'wb') as stream:
                            stream.write(base64.b64decode(images[0].data, validate=True))
                        print(json.dumps({'challengeImage':str(challenge.absolute()),'realm':'legacy'}), flush=True)
                        import anyio
                        code = (await anyio.to_thread.run_sync(sys.stdin.readline)).strip()
                        challenge.unlink()
                        if not code:
                            raise RuntimeError('CAPTCHA_INPUT_REQUIRED')
                        authenticated = await session.call_tool('ruc_authenticate', {'realm':'legacy','verification_code':code})
                        if authenticated.is_error:
                            raise RuntimeError(authenticated.structured_content.get('code','AUTH_FAILED'))
                        answer = await session.call_tool(name, values)
                        data = answer.structured_content
                    row = {'tool': name, 'status': data['status']}
                    if 'capability' in values:
                        row['capability'] = values['capability']
                    for key in ['code','totalItems','pageItems','complete','schemaFingerprint','credentialSubmissions']:
                        if key in data:
                            row[key] = data[key]
                    if isinstance(data.get('data'), list):
                        row['items'] = len(data['data'])
                    if isinstance(data.get('data'), dict):
                        row['dataFields'] = sorted(data['data'])
                    results.append(row)
                    print(json.dumps(row, ensure_ascii=False), flush=True)
                    if answer.is_error:
                        raise RuntimeError(data.get('code', 'MCP_TOOL_ERROR'))
                    return data

                for capability in ['booths.notices', 'booths.usage_types', 'booths.my_reservations']:
                    await call('ruc_query', {'capability': capability})
                options = await call('ruc_query', {'capability': 'academic_options'})
                term = next((t['id'] for t in options['terms'] if str(t.get('sfdqxqcode')) == '1'), options['terms'][-1]['id'])
                await call('ruc_query', {'capability':'academic.calendar','params':{'term':term}})
                api = await call('ruc_discover', {'platform':'academic','keyword':'WeekCalendar'})
                results[-1]['matchedApiCount'] = len(api['operations'])
                day = (datetime.now(BEIJING).date() + timedelta(days=1)).isoformat()
                booths = await call('ruc_query', {'capability':'quiet_booths','params':{'date':day,'campus':'中关村','type':'单人','minutes':60}})
                room_ids = [r['id'] for group in booths.get('groups',[]) for r in group.get('rooms',[])]
                if room_ids:
                    await call('ruc_query', {'capability':'booths.required_fields','params':{'roomId':room_ids[0]}})
                else:
                    results.append({'capability':'booths.required_fields','status':'not_run','reason':'no_available_room_in_current_query'})
                # A runtime-defined alias demonstrates that MCP has no fixed capability enum.
                caps = await call('ruc_capabilities', {'keyword':'booths.usage_types'})
                alias = next(s for s in caps['capabilities'] if s['id']=='booths.usage_types')
                alias['id'] = 'acceptance.dynamic_usage_types'
                await call('ruc_register_contract', {'document': {'capabilities':[alias]}})
                await call('ruc_query', {'capability':alias['id']})
                if args.extended:
                    await call('ruc_platforms', {'live':True})
                    # Every entry is a registered read, using only its default
                    # filters or values returned in this new task.
                    for capability in [
                        'personal.identity','personal.categories','personal.overview',
                        'services.identity','services.applications','services.todo',
                        'forms.identity','forms.my_submissions','forms.todo',
                        'ai.profile','ai.applications','resources.applications',
                        'mobile.cards','mobile.card_history_types',
                        'office.navigation','office.home','office.urgent_tasks','office.login_history',
                        'software.catalog','eresources.catalog','eresources.tags',
                        'eoffice.identity','eoffice.tables','warm.identity','warm.navigation',
                        'canteen.summary','canteen.overview','canteen.options',
                        'opac.identity','opac.loans','opac.loan_history','opac.loan_rules',
                        'study.identity','study.catalog',
                        'legacy.roles','course.profile','course.terms','course.notices',
                        'business.home','business.profile','business.activities',
                        'career.profile','career.jobs','career.messages','career.guiding_activities',
                        'business_career.news','business_career.consultations',
                        'cloud.profile','cloud.storage_quotas','cloud.application_specs',
                        'innovation.catalog','innovation.my_research','innovation.my_teams',
                        'media.years','media.profile','media.activities',
                        'museum.profile','museum.areas','museum.reservations',
                        'party.profile','party.categories','party.study_collections','party.training',
                        'graduate.profile','graduate.applications','red.member_page',
                        'datasets.featured','datasets.fieldsets','datasets.search',
                        'affairs.activity_types','affairs.todo','affairs.score_summary',
                        'affairs.appointment_types','affairs.appointments',
                        'teaching.profile','teaching.terms','teaching.notices',
                        'virtual_labs.colleges',
                        'visitor.systems','visitor.records','visitor.purposes',
                        'future.profile','future.terms','future.current_term',
                        'future.courses','future.notices','future.meetings',
                        'philosophy.rooms','philosophy.my_reservations',
                    ]:
                        await call('ruc_query', {'capability':capability})
                    await call('ruc_query', {'capability':'mobile.card_history',
                                           'params':{'month':datetime.now(BEIJING).strftime('%Y-%m')}})
                    await call('ruc_query', {'capability':'opac.loan_history','all_pages':True,'max_pages':10})
                    for capability in ['affairs.my_activities','affairs.my_appointments','affairs.scores',
                                       'virtual_labs.projects','future.homework']:
                        await call('ruc_query', {'capability':capability,'all_pages':True,'max_pages':10})
                if args.browser:
                    browser = await call('ruc_browser_open', {'url':'https://jyc.ruc.edu.cn/space/'})
                    report['browser'] = {'frameCount':len(browser['frames']),
                                         'elementCount':sum(len(f.get('elements',[])) for f in browser['frames']),
                                         'textLengths':[len(f.get('text','')) for f in browser['frames']],
                                         'blockedRequests':browser['blockedRequests'],
                                         'url':browser['url']}
                report.update(await call('ruc_reset_session', {}))
    except Exception as error:
        report['failureType'] = type(error).__name__
        # Do not serialize exception messages from external libraries or tool bodies.
        print(json.dumps({'status':'failed','type':type(error).__name__}), flush=True)
    finally:
        report['elapsedSeconds'] = round(perf_counter()-start,3)
        report['finishedAt'] = datetime.now(BEIJING).isoformat(timespec='seconds')
        Path(args.output).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return 1 if 'failureType' in report else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--credentials-file',required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--browser',action='store_true')
    parser.add_argument('--extended',action='store_true',help='验证新增平台的本人信息和目录')
    parser.add_argument('--challenge-dir',help='将本次验证码保存到此临时目录；从 stdin 读取识别结果')
    args = parser.parse_args()
    import anyio
    return anyio.run(verify,args)


if __name__ == '__main__':
    sys.exit(main())
