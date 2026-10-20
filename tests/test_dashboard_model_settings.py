import ast
import unittest
from pathlib import Path


class DashboardModelSettingsTests(unittest.TestCase):
    def test_post_handler_does_not_shadow_global_regex_module(self):
        tree=ast.parse(Path('dashboard.py').read_text(encoding='utf-8'))
        handler=next(node for node in ast.walk(tree) if isinstance(node,ast.ClassDef) and node.name=='Handler')
        post=next(node for node in handler.body if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and node.name=='do_POST')
        imports=[node for node in ast.walk(post) if isinstance(node,(ast.Import,ast.ImportFrom))]
        self.assertFalse(any(any(alias.name=='re' or alias.asname=='re' for alias in node.names) for node in imports))

