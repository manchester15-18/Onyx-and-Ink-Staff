import contextlib
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
import threading
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('CREWAI_TELEMETRY_DISABLED', 'true')
os.environ.setdefault('OTEL_SDK_DISABLED', 'true')
import main
import httpx
from groq_llm import GroqLLM, RequestBudget
from openai import OpenAI
from staff_email import StaffMail, STAFF


class GroqTests(unittest.TestCase):
    def test_quota_failure_does_not_launch_queued_departments(self):
        with tempfile.TemporaryDirectory() as directory:
            started=[];release=threading.Event()
            class Assignment:
                def __init__(self,name):self.name=name;self.output_file=Path(directory)/(name+'.md')
                def execute_sync(self):
                    started.append(self.name)
                    if self.name=='Morgan':release.set();raise RuntimeError('quota')
                    release.wait(1);return '# '+self.name+'\n\nComplete.'
            tasks={name:Assignment(name) for name in ('Morgan','Avery','Jordan','Cameron')}
            with self.assertRaisesRegex(RuntimeError,'quota'):main.run_assignments(tasks)
            self.assertIn('Morgan',started);self.assertTrue(set(started).issubset({'Morgan','Avery'}))

    def test_provider_retry_time_is_parsed_and_buffered(self):
        class Limited(Exception):
            response=type('Response',(),{'headers':{'x-ratelimit-reset-tokens':'6m54.72s','x-ratelimit-reset-requests':'1h0m0s'}})()
        wait=main.quota_retry_seconds(Limited('Please try again in 3m18.72s'))
        self.assertEqual(wait,444)

    def test_content_blocks_model_prefix_and_tool_metadata(self):
        llm=GroqLLM('offline-test-key',model='groq/openai/gpt-oss-120b')
        try:
            payload=llm._prepare_completion_params([
                {'role':'user','content':[{'text':'First'},{'type':'text','text':'Second'}]},
                {'role':'tool','content':{'text':'Result'},'tool_call_id':'call_123'},
            ])
            self.assertEqual(payload['model'],'openai/gpt-oss-120b')
            self.assertEqual(payload['messages'][0]['content'],'First\nSecond')
            self.assertEqual(payload['messages'][1]['tool_call_id'],'call_123')
            self.assertEqual(payload['messages'][1]['content'],'Result')
            self.assertEqual(payload['extra_body'], {'include_reasoning': False})
            with self.assertRaises(ValueError):llm._prepare_completion_params([{'role':'user','content':[{'type':'image_url','image_url':{}}]}])
        finally:llm.close()

    def test_key_errors_do_not_echo_secret(self):
        with patch.dict(os.environ, {'GROQ_API_KEY': 'secret\u201ckey'}):
            with self.assertRaises(ValueError) as error:
                main.credential('GROQ_API_KEY')
            self.assertNotIn('secret', str(error.exception))

    def test_invalid_tavily_key_disables_search_without_stopping_run(self):
        with patch('main.verify_tavily',side_effect=main.WebResearchError('Tavily rejected the API key.')),contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertIsNone(main.verified_search_key('private-test-key'))
        self.assertIn('Tavily rejected',output.getvalue())
        self.assertNotIn('private-test-key',output.getvalue())

    def test_inventory(self):
        with contextlib.redirect_stdout(io.StringIO()):
            result = main.check_blank_stock.run(item_names=['shirts', 'tumblers', 'missing'])
        self.assertIn('150 units', result)
        self.assertIn('240 units', result)
        self.assertIn('SAMPLE', result)
        self.assertIn('Not found', result)

    def test_department_ownership_is_immutable_and_mislabeled_reports_are_fixed(self):
        value = "\n".join(
            (f"**Legal ({'Avery'}):** policy", f"**Marketing ({'Jordan'}):** campaign", f"**Web Dev ({'Cameron'}):** implementation")
        )
        fixed=main.enforce_department_ownership(value)
        self.assertIn('HR / Legal (Cameron)',fixed);self.assertIn('Marketing (Avery)',fixed);self.assertIn('Web Development / IT (Jordan)',fixed)
        llm=GroqLLM('offline-test-key')
        try:
            with tempfile.TemporaryDirectory() as directory:
                tasks=main.build_assignments(llm,'Continue approved work.',directory)
                self.assertIn('COO coordination only',tasks['Morgan'].description)
                self.assertIn('permanent department is Marketing',tasks['Avery'].description)
                self.assertIn('permanent department is Web Development and IT',tasks['Jordan'].description)
                self.assertIn('permanent department is HR and Legal',tasks['Cameron'].description)
        finally:llm.close()

    def test_unfinished_model_drafting_is_not_published_as_a_report(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'morgan-operations.md';path.write_text('Thought: I should decide what to do next.')
            self.assertFalse(main.finalize_assignment_report(path,'Morgan'))
            saved=path.read_text()
            self.assertNotIn('I should decide',saved)
            self.assertIn('Assignment Needs Retry',saved)

    def test_request_and_token_pacing(self):
        budget = RequestBudget(2, 500)
        clock = [100.0]
        def sleep(delay): clock[0] += delay
        request = httpx.Request('POST', 'https://api.groq.com/openai/v1/chat/completions', json={'messages': [{'role':'user','content':'hi'}], 'max_completion_tokens':100})
        with patch('time.monotonic', lambda:clock[0]), patch('time.sleep',sleep), contextlib.redirect_stdout(io.StringIO()):
            budget.before_request(request)
            budget.before_request(request)
            budget.before_request(request)
        self.assertGreaterEqual(clock[0], 161)
        huge = httpx.Request('POST','https://api.groq.com',json={'messages':[{'content':'x'*2000}]})
        oversized = RequestBudget(2, 200)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            oversized.before_request(huge)
        self.assertEqual(oversized.entries[-1][1], 200)
        self.assertIn('isolated large request', output.getvalue())

    def test_payload_and_full_crew_reports(self):
        calls = []
        def respond(request):
            self.assertEqual(request.url.host, 'api.groq.com')
            payload = json.loads(request.content)
            self.assertEqual(payload['model'], 'qwen/qwen3.8-27b')
            self.assertEqual(payload['reasoning_effort'], 'none')
            self.assertNotIn('stop', payload)
            calls.append(payload)
            content=f'Final Answer: Department report {len(calls)}. Sample data only.'
            return httpx.Response(200, json={'id':'mock','object':'chat.completion','created':0,'model':payload['model'],'choices':[{'index':0,'message':{'role':'assistant','content':content},'finish_reason':'stop'}],'usage':{'prompt_tokens':50,'completion_tokens':20,'total_tokens':70}})
        llm = GroqLLM('offline-test-key')
        llm.client.close()
        llm.client = OpenAI(api_key='offline-test-key',base_url='https://api.groq.com/openai/v1',http_client=httpx.Client(transport=httpx.MockTransport(respond)),max_retries=0)
        try:
            with tempfile.TemporaryDirectory() as directory, patch.object(socket.socket,'connect',side_effect=AssertionError('Network forbidden')),contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                addresses={name:f'{name.lower()}@example.com' for name in (*STAFF,'Owner')}
                mail=StaffMail(directory,'draft',addresses)
                tasks=main.build_assignments(llm,'Test gift launch',directory,mail=mail)
                self.assertTrue(all(any('Email from' in tool.name for tool in task.agent.tools) for task in tasks.values()))
                self.assertTrue(any(tool.name=='Check sample blank inventory' for tool in tasks['Avery'].agent.tools))
                result = main.run_assignments(tasks)
                self.assertEqual(mail.count,3)  # Handoffs only until the run finishes.
                mail.finish()
                self.assertEqual(mail.count,3)  # Routine reports stay in the dashboard.
                self.assertEqual(len(list(mail.outbox.glob('*.eml'))),3)
                self.assertEqual(len(calls),4)
                for name in ['morgan-operations.md','avery-marketing.md','jordan-web-it.md','cameron-hr-legal.md']:
                    self.assertTrue((Path(directory)/name).is_file(), name)
                self.assertEqual(set(result),{'Morgan','Avery','Jordan','Cameron'})
                self.assertTrue(all('Test gift launch' in task.description for task in tasks.values()))
        finally: llm.close()

    def test_http_retry_is_bounded_and_paced(self):
        llm=GroqLLM('offline-test-key')
        llm.client.close()
        calls=[]
        def respond(request):
            calls.append(request)
            if len(calls)==1: return httpx.Response(429,headers={'retry-after':'0.01'},json={'error':{'message':'temporary rate limit','type':'rate_limit_error'}})
            return httpx.Response(200,json={'id':'mock','object':'chat.completion','created':0,'model':llm.model,'choices':[{'index':0,'message':{'role':'assistant','content':'Recovered'},'finish_reason':'stop'}]})
        client=httpx.Client(transport=httpx.MockTransport(respond),event_hooks={'request':[llm.budget.before_request]})
        llm.client=OpenAI(api_key='offline-test-key',base_url='https://api.groq.com/openai/v1',http_client=client,max_retries=1)
        clock=[100.0]
        def sleep(delay): clock[0]+=delay
        try:
            with patch('time.monotonic',lambda:clock[0]),patch('time.sleep',sleep),contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(llm.call('test'),'Recovered')
            self.assertEqual(len(calls),2)
            self.assertEqual(len(llm.budget.entries),2)
        finally: llm.close()

    def test_empty_visible_response_is_retried_with_explicit_instruction(self):
        llm=GroqLLM('offline-test-key')
        llm.client.close()
        calls=[]
        def respond(request):
            payload=json.loads(request.content);calls.append(payload)
            content='' if len(calls)==1 else 'Visible answer'
            return httpx.Response(200,json={'id':'mock','object':'chat.completion','created':0,'model':llm.model,'choices':[{'index':0,'message':{'role':'assistant','content':content},'finish_reason':'stop'}]})
        llm.client=OpenAI(api_key='offline-test-key',base_url='https://api.groq.com/openai/v1',http_client=httpx.Client(transport=httpx.MockTransport(respond)),max_retries=0)
        try:
            self.assertEqual(llm.call([{'role':'user','content':'Prepare a report.'}]),'Visible answer')
            self.assertEqual(len(calls),2)
            self.assertIn('no visible answer',calls[1]['messages'][-1]['content'])
        finally:llm.close()

    def test_repeated_empty_visible_responses_raise_clear_error(self):
        llm=GroqLLM('offline-test-key')
        llm.client.close()
        calls=[]
        def respond(request):
            calls.append(request)
            return httpx.Response(200,json={'id':'mock','object':'chat.completion','created':0,'model':llm.model,'choices':[{'index':0,'message':{'role':'assistant','content':''},'finish_reason':'stop'}]})
        llm.client=OpenAI(api_key='offline-test-key',base_url='https://api.groq.com/openai/v1',http_client=httpx.Client(transport=httpx.MockTransport(respond)),max_retries=0)
        try:
            with self.assertRaisesRegex(ValueError,'three empty visible responses'):
                llm.call('Prepare a report.')
            self.assertEqual(len(calls),3)
        finally:llm.close()


if __name__ == '__main__': unittest.main()
