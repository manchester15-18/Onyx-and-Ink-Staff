import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from web_research import WebResearchError, search, verify


class TavilyResearchTests(unittest.TestCase):
    def test_search_returns_bounded_citation_ready_results(self):
        response=Mock(status_code=200,ok=True)
        response.json.return_value={'results':[{'title':'Current source','url':'https://example.com/current','content':'Useful current details.','raw_content':'ignored'}]}
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'.env').write_text('TAVILY_API_KEY=private-test-key-123456789\n')
            with patch('web_research.requests.post',return_value=response) as post:
                result=json.loads(search(root,'custom gift trends',5))
        self.assertEqual(result['results'][0]['url'],'https://example.com/current')
        self.assertNotIn('raw_content',result['results'][0])
        self.assertEqual(post.call_args.kwargs['headers']['Authorization'],'Bearer private-test-key-123456789')
        self.assertNotIn('private-test-key',json.dumps(result))

    def test_rejected_key_has_clear_safe_error(self):
        response=Mock(status_code=401,ok=False)
        with patch('web_research.requests.post',return_value=response):
            with self.assertRaisesRegex(WebResearchError,'rejected the API key'):
                verify('private-test-key-123456789')


if __name__=='__main__':unittest.main()
