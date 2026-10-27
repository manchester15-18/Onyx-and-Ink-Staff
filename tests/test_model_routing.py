import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
import requests
from staff.model_routing import RoutedLLM,RoutingError,routes,routing_status,openrouter_effective_max_tokens,gemma_cooldown_seconds,gemini_models,enabled

class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.env=patch.dict('os.environ',{},clear=True);self.env.start();self.addCleanup(self.env.stop)
    def test_role_order_and_free_only_guards(self):
        cfg={'GROQ_API_KEY':'groq-test','CEREBRAS_API_KEY':'cerebras-test','OPENROUTER_API_KEY':'router-test','GEMINI_API_KEY':'gemini-test'}
        self.assertEqual(routes(cfg,'Avery'),['openrouter','cerebras','groq'])
        self.assertEqual(routes(cfg,'Cameron'),['openrouter','cerebras','groq'])
        cfg['GEMINI_FREE_TIER_CONFIRMED']='true'
        cfg['GEMINI_MODEL']='gemini-3.8-flash'
        self.assertEqual(routes(cfg,'Cameron')[0],'openrouter')
        self.assertNotIn('groq-test',str(routing_status(cfg)))
        self.assertNotIn('ollama',str(routing_status(cfg)))
    def test_rate_limited_primary_falls_back_once_and_cools_down(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg={'CEREBRAS_API_KEY':'c-key','GROQ_API_KEY':'g-key'}
            llm=RoutedLLM(folder,cfg,'Avery',max_tokens=20)
            limited=Mock(ok=False,status_code=429,headers={'Retry-After':'300'})
            success=Mock(ok=True);success.json.return_value={'choices':[{'message':{'content':'Answer'}}]}
            with patch('staff.model_routing.requests.post',side_effect=[limited,success,success]) as post:
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
            with patch('staff.model_routing.requests.post') as post:
                with self.assertRaises(RoutingError):llm.call([{'role':'user','content':'Hello'}])
                post.assert_not_called()
    def test_final_fallback_uses_zero_price_cloud_router(self):
        with tempfile.TemporaryDirectory() as folder:
            llm=RoutedLLM(folder,{'OPENROUTER_API_KEY':'router-key'},'Morgan',max_tokens=20)
            response=Mock(ok=True);response.json.return_value={'choices':[{'message':{'content':'Emergency answer'}}]}
            with patch('staff.model_routing.requests.post',return_value=response) as post:
                self.assertEqual(llm.call('Hello'),'Emergency answer')
                payload=post.call_args.kwargs['json']
                self.assertEqual(payload['model'],'openrouter/free')
                self.assertEqual(payload['provider']['max_price'],{'prompt':0,'completion':0,'request':0})
                self.assertTrue(post.call_args.args[0].startswith('https://openrouter.ai/'))
    def test_openrouter_free_ladder_tries_next_model_after_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            llm=RoutedLLM(folder,{'OPENROUTER_API_KEY':'router-key','OPENROUTER_MODELS':'google/gemma-4-31b-it:free,liquid/lfm-2.5-2.6b:free'},'Morgan',max_tokens=20)
            limited=Mock(ok=False,status_code=429,headers={'Retry-After':'60'})
            success=Mock(ok=True);success.json.return_value={'choices':[{'message':{'content':'Ladder answer'}}]}
            with patch('staff.model_routing.requests.post',side_effect=[limited,success]) as post:
                self.assertEqual(llm.call('Hello'),'Ladder answer')
                self.assertEqual(post.call_args.kwargs['json']['model'],'liquid/lfm-2.5-2.6b:free')
    def test_openrouter_ladder_rejects_paid_models_before_network(self):
        with tempfile.TemporaryDirectory() as folder:
            llm=RoutedLLM(folder,{'OPENROUTER_API_KEY':'router-key','OPENROUTER_MODELS':'paid/model'},'Morgan',max_tokens=20)
            with patch('staff.model_routing.requests.post') as post:
                with self.assertRaises(RoutingError):llm.call('Hello')
                post.assert_not_called()
    def test_failure_does_not_leak_credentials_or_response_body(self):
        with tempfile.TemporaryDirectory() as folder:
            llm=RoutedLLM(folder,{'GROQ_API_KEY':'private-test-key'},'Morgan',max_tokens=20)
            with patch('staff.model_routing.requests.post',side_effect=requests.ConnectionError('private-test-key')):
                with self.assertRaises(RoutingError) as error:llm.call('Hello')
                self.assertNotIn('private-test-key',str(error.exception))
    def test_cloudflare_returns_visible_answer_without_mutating_history(self):
        with tempfile.TemporaryDirectory() as folder:
            llm=RoutedLLM(folder,{'CLOUDFLARE_API_TOKEN':'test','CLOUDFLARE_ACCOUNT_ID':'test','CLOUDFLARE_FREE_PLAN_CONFIRMED':'true'},'Jordan')
            response=Mock(ok=True);response.json.return_value={'result':{'response':'<think>internal</think>{"ok":true}'}}
            messages=[{'role':'user','content':'Return JSON'}]
            with patch('staff.model_routing.requests.post',return_value=response) as post:
                self.assertEqual(llm.call(messages),'{"ok":true}')
                self.assertTrue(post.call_args.kwargs['json']['messages'][0]['content'].endswith('/no_think'))
                self.assertEqual(messages[0]['content'],'Return JSON')
    def test_multimodal_input_is_rejected_without_silent_text_conversion(self):
        with tempfile.TemporaryDirectory() as folder:
            llm=RoutedLLM(folder,{'GROQ_API_KEY':'test'},'Morgan')
            with patch('staff.model_routing.requests.post') as post:
                with self.assertRaisesRegex(RoutingError,'multimodal'):llm.call([{'role':'user','content':[{'type':'image_url'}]}])
                post.assert_not_called()
    def test_openrouter_per_model_ceiling_differs_by_model(self):
        # Per-model, not one global ceiling: Gemma caps at 32768, Super VL at 131072.
        self.assertEqual(openrouter_effective_max_tokens('google/gemma-4-31b-it:free',999999,100),32768)
        self.assertEqual(openrouter_effective_max_tokens('nvidia/nemotron-3.5-super-vl:free',999999,100),131072)
        self.assertEqual(openrouter_effective_max_tokens('nvidia/nemotron-3.5-lightning:free',999999,100),65536)
        self.assertEqual(openrouter_effective_max_tokens('openrouter/free',999999,100),32768)
        self.assertEqual(openrouter_effective_max_tokens('unknown/new-model:free',999999,100),16384)
    def test_openrouter_request_sends_per_model_ceiling_not_requested_value(self):
        with tempfile.TemporaryDirectory() as folder:
            llm=RoutedLLM(folder,{'OPENROUTER_API_KEY':'router-key'},'Morgan',max_tokens=999999)
            response=Mock(ok=True);response.json.return_value={'choices':[{'message':{'content':'Answer'}}]}
            with patch('staff.model_routing.requests.post',return_value=response) as post:
                self.assertEqual(llm.request('openrouter',[{'role':'user','content':'Hi'}],model='google/gemma-4-31b-it:free'),'Answer')
                self.assertEqual(post.call_args.kwargs['json']['max_tokens'],32768)
    def test_openrouter_reasoning_floor_lifts_tiny_budgets(self):
        # 20 tokens gave empty answers live on Nemotron reasoning models; 512 is the floor.
        self.assertEqual(openrouter_effective_max_tokens('nvidia/nemotron-3.5-super-vl:free',20,100),512)
    def test_openrouter_prompt_over_context_raises_instead_of_sending(self):
        with self.assertRaises(RoutingError):
            openrouter_effective_max_tokens('google/gemma-4-31b-it:free',1000,262144)
    def test_openrouter_per_model_cooldown_does_not_block_other_models(self):
        with tempfile.TemporaryDirectory() as folder:
            llm=RoutedLLM(folder,{'OPENROUTER_API_KEY':'router-key'},'Morgan',max_tokens=20)
            self.assertFalse(llm.is_model_cooling_down('google/gemma-4-31b-it:free'))
            llm.set_model_cooldown('google/gemma-4-31b-it:free',3600)
            self.assertTrue(llm.is_model_cooling_down('google/gemma-4-31b-it:free'))
            self.assertFalse(llm.is_model_cooling_down('liquid/lfm-2.5-2.6b:free'))
    def test_openrouter_ladder_has_at_least_five_free_models(self):
        ladder=(Path('.env.example').read_text() if Path('.env.example').exists() else '')
        import re as _re
        m=_re.search(r'^OPENROUTER_MODELS=(.*)$',ladder,flags=_re.MULTILINE)
        self.assertIsNotNone(m)
        models=[x.strip() for x in m.group(1).split(',') if x.strip()]
        self.assertGreaterEqual(len(models),5)
        self.assertTrue(all(x=='openrouter/free' or x.endswith(':free') for x in models))
        self.assertNotIn('qwen/qwen3.8-27b:free',models)
    def test_dots_model_has_limits_and_is_not_vision(self):
        from staff.model_routing import OPENROUTER_MODEL_LIMITS, OPENROUTER_VISION_MODELS
        self.assertEqual(OPENROUTER_MODEL_LIMITS.get('dots-studio/dots-3-note-preview:free'),(512000,460800))
        self.assertNotIn('dots-studio/dots-3-note-preview:free', OPENROUTER_VISION_MODELS)
    def test_gemini_models_include_gemma_by_default(self):
        self.assertEqual(gemini_models({'GEMINI_MODEL':'gemini-3.8-flash'}),['gemini-3.8-flash','gemma-4-31b-it','gemma-4-26b-a4b-it'])
        self.assertEqual(gemini_models({'GEMINI_MODELS':'gemini-3.8-flash,gemma-4-31b-it'}),['gemini-3.8-flash','gemma-4-31b-it'])
        self.assertTrue(enabled({'GEMINI_API_KEY':'x','GEMINI_FREE_TIER_CONFIRMED':'true','GEMINI_MODEL':'gemini-3.8-flash'},'gemini'))
    def test_gemini_flash_503_falls_back_to_gemma_and_strips_thought(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg={'OPENROUTER_API_KEY':'r','GEMINI_API_KEY':'g','GEMINI_FREE_TIER_CONFIRMED':'true','GEMINI_MODEL':'gemini-3.8-flash','GEMINI_MODELS':'gemini-3.8-flash,gemma-4-31b-it'}
            llm=RoutedLLM(folder,cfg,'Morgan',max_tokens=20)
            unavailable=Mock(ok=False,status_code=503,headers={})
            success=Mock(ok=True);success.json.return_value={'choices':[{'message':{'content':'<thought>thinking here</thought>Hello! How can I help you today?'}}]}
            # Force gemini route only by patching routes to ['gemini']
            with patch('staff.model_routing.routes',return_value=['gemini']):
                with patch('staff.model_routing.requests.post',side_effect=[unavailable,success]) as post:
                    self.assertEqual(llm.call([{'role':'user','content':'Hi'}]),'Hello! How can I help you today?')
                    self.assertEqual(llm.last_provider,'gemini')
                    self.assertEqual(post.call_args_list[1].kwargs['json']['model'],'gemma-4-31b-it')
    def test_gemma_cooldown_is_longer_than_other_models(self):
        self.assertEqual(gemma_cooldown_seconds({}),21600)
        self.assertEqual(gemma_cooldown_seconds({'OPENROUTER_GEMMA_COOLDOWN_SECONDS':'300'}),300)
        self.assertEqual(gemma_cooldown_seconds({'OPENROUTER_GEMMA_COOLDOWN_SECONDS':'999999'}),86400)
    def test_gemma_429_sets_long_model_cooldown_not_provider_cooldown(self):
        with tempfile.TemporaryDirectory() as folder:
            llm=RoutedLLM(folder,{'OPENROUTER_API_KEY':'router-key'},'Morgan',max_tokens=20)
            limited=Mock(ok=False,status_code=429,headers={'Retry-After':'60'})
            with patch('staff.model_routing.requests.post',return_value=limited):
                with self.assertRaises(RoutingError):
                    llm.request('openrouter',[{'role':'user','content':'Hi'}],model='google/gemma-4-31b-it:free')
            self.assertTrue(llm.is_model_cooling_down('google/gemma-4-31b-it:free'))
            self.assertFalse(llm.is_cooling_down('openrouter'))
    def test_autonomous_assignments_use_each_roles_router(self):
        import main
        with tempfile.TemporaryDirectory() as folder:
            llm=RoutedLLM(folder,{'GROQ_API_KEY':'test'},'Morgan')
            tasks=main.build_assignments(llm,'Continue approved work.',Path(folder))
            for name,task in tasks.items():self.assertEqual(task.agent.llm.agent,name)
