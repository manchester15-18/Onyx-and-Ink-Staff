import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
import requests
from model_routing import RoutedLLM,RoutingError,routes,routing_status

class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.env=patch.dict('os.environ',{},clear=True);self.env.start();self.addCleanup(self.env.stop)
    def test_role_order_and_free_only_guards(self):
        cfg={'GROQ_API_KEY':'groq-test','CEREBRAS_API_KEY':'cerebras-test','OPENROUTER_API_KEY':'router-test','GEMINI_API_KEY':'gemini-test'}
        self.assertEqual(routes(cfg,'Avery'),['cerebras','openrouter','groq'])
        self.assertEqual(routes(cfg,'Cameron'),['cerebras','openrouter','groq'])
        cfg['GEMINI_FREE_TIER_CONFIRMED']='true'
        cfg['GEMINI_MODEL']='gemini-3.8-flash'
        self.assertEqual(routes(cfg,'Cameron')[0],'gemini')
        self.assertNotIn('groq-test',str(routing_status(cfg)))
        self.assertNotIn('ollama',str(routing_status(cfg)))
    def test_rate_limited_primary_falls_back_once_and_cools_down(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg={'CEREBRAS_API_KEY':'c-key','GROQ_API_KEY':'g-key'}
            llm=RoutedLLM(folder,cfg,'Avery',max_tokens=20)
            limited=Mock(ok=False,status_code=429,headers={'Retry-After':'300'})
            success=Mock(ok=True);success.json.return_value={'choices':[{'message':{'content':'Answer'}}]}
            with patch('model_routing.requests.post',side_effect=[limited,success,success]) as post:
                self.assertEqual(llm.call([{'role':'user','content':'Hello'}]),'Answer')
                self.assertEqual(llm.call([{'role':'user','content':'Next'}]),'Answer')
                self.assertEqual(post.call_count,3)
                self.assertIn('cerebras.ai',post.call_args_list[0].args[0])
                self.assertTrue(all('api.groq.com' in call.args[0] for call in post.call_args_list[1:]))
                self.assertEqual(llm.last_provider,'groq')
    def test_shared_budget_reservation_across_instances(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg={'GROQ_API_KEY':'test','GROQ_RPM':'1'}
            first=RoutedLLM(folder,cfg,'Morgan',max_tokens=20);second=RoutedLLM(folder,cfg,'Avery',max_tokens=20)
            first.reserve('groq',[{'role':'user','content':'Hello'}])
            with self.assertRaisesRegex(RoutingError,'budget'):second.reserve('groq',[{'role':'user','content':'Hello'}])
    def test_background_work_reserves_half_minute_budget_for_interactive_chat(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg={'GROQ_API_KEY':'test','GROQ_TPM':'1000','GROQ_RPM':'10'}
            background=RoutedLLM(folder,cfg,'Morgan',max_tokens=300,priority='background')
            interactive=RoutedLLM(folder,cfg,'Morgan',max_tokens=300)
            background.reserve('groq',[{'role':'user','content':'x'*200}])
            interactive.reserve('groq',[{'role':'user','content':'x'*200}])
    def test_legacy_background_usage_does_not_block_an_interactive_chat(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg={'GROQ_API_KEY':'test','GROQ_TPD':'1000'}
            background=RoutedLLM(folder,cfg,'Morgan',max_tokens=100,priority='background')
            interactive=RoutedLLM(folder,cfg,'Morgan',max_tokens=100)
            message=[{'role':'user','content':'x'*100}]
            for _ in range(4):
                try:background.reserve('groq',message)
                except RoutingError:break
            interactive.reserve('groq',message)
    def test_cloud_final_fallback_rejects_paid_models_before_network(self):
        with tempfile.TemporaryDirectory() as folder:
            llm=RoutedLLM(folder,{'OPENROUTER_API_KEY':'router-key','OPENROUTER_MODEL':'paid/model'},'Morgan',max_tokens=20)
            with patch('model_routing.requests.post') as post:
                with self.assertRaises(RoutingError):llm.call([{'role':'user','content':'Hello'}])
                post.assert_not_called()
    def test_final_fallback_uses_zero_price_cloud_router(self):
        with tempfile.TemporaryDirectory() as folder:
            llm=RoutedLLM(folder,{'OPENROUTER_API_KEY':'router-key'},'Morgan',max_tokens=20)
            response=Mock(ok=True);response.json.return_value={'choices':[{'message':{'content':'Emergency answer'}}]}
            with patch('model_routing.requests.post',return_value=response) as post:
                self.assertEqual(llm.call('Hello'),'Emergency answer')
                payload=post.call_args.kwargs['json']
                self.assertEqual(payload['model'],'openrouter/free')
                self.assertEqual(payload['provider']['max_price'],{'prompt':0,'completion':0,'request':0})
                self.assertTrue(post.call_args.args[0].startswith('https://openrouter.ai/'))
    def test_failure_does_not_leak_credentials_or_response_body(self):
        with tempfile.TemporaryDirectory() as folder:
            llm=RoutedLLM(folder,{'GROQ_API_KEY':'private-test-key'},'Morgan',max_tokens=20)
            with patch('model_routing.requests.post',side_effect=requests.ConnectionError('private-test-key')):
                with self.assertRaises(RoutingError) as error:llm.call('Hello')
                self.assertNotIn('private-test-key',str(error.exception))
    def test_cloudflare_returns_visible_answer_without_mutating_history(self):
        with tempfile.TemporaryDirectory() as folder:
            llm=RoutedLLM(folder,{'CLOUDFLARE_API_TOKEN':'test','CLOUDFLARE_ACCOUNT_ID':'test','CLOUDFLARE_FREE_PLAN_CONFIRMED':'true'},'Jordan')
            response=Mock(ok=True);response.json.return_value={'result':{'response':'<think>internal</think>{"ok":true}'}}
            messages=[{'role':'user','content':'Return JSON'}]
            with patch('model_routing.requests.post',return_value=response) as post:
                self.assertEqual(llm.call(messages),'{"ok":true}')
                self.assertTrue(post.call_args.kwargs['json']['messages'][0]['content'].endswith('/no_think'))
                self.assertEqual(messages[0]['content'],'Return JSON')
    def test_multimodal_input_is_rejected_without_silent_text_conversion(self):
        with tempfile.TemporaryDirectory() as folder:
            llm=RoutedLLM(folder,{'GROQ_API_KEY':'test'},'Morgan')
            with patch('model_routing.requests.post') as post:
                with self.assertRaisesRegex(RoutingError,'multimodal'):llm.call([{'role':'user','content':[{'type':'image_url'}]}])
                post.assert_not_called()
    def test_autonomous_assignments_use_each_roles_router(self):
        import main
        with tempfile.TemporaryDirectory() as folder:
            llm=RoutedLLM(folder,{'GROQ_API_KEY':'test'},'Morgan')
            tasks=main.build_assignments(llm,'Continue approved work.',Path(folder))
            for name,task in tasks.items():self.assertEqual(task.agent.llm.agent,name)
