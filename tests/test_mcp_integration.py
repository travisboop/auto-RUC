"""An official SDK client talks to the actual stdio subprocess; no school calls."""
import importlib.util
from pathlib import Path
import sys
import unittest


@unittest.skipUnless(importlib.util.find_spec('mcp'), 'MCP optional dependency is not installed')
class MCPIntegration(unittest.TestCase):
    def test_captcha_is_a_native_image_without_submitting_credentials(self):
        import anyio
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        root = str(Path(__file__).resolve().parents[1] / 'src')
        program = '''import sys, asyncio
sys.path.insert(0, sys.argv[1])
from ruc_agent.server import mcp_stdio
from ruc_agent.errors import QueryError
class Fixture:
    def call(self, name, args):
        raise QueryError('CAPTCHA_REQUIRED', 'fixture', {'imageDataUrl':'data:image/png;base64,Zml4dHVyZQ=='})
    def close(self): pass
asyncio.run(mcp_stdio(Fixture()))
'''
        async def run():
            async with stdio_client(StdioServerParameters(command=sys.executable, args=['-c',program,root])) as (r,w):
                async with ClientSession(r,w) as session:
                    await session.initialize()
                    result = await session.call_tool('ruc_authenticate', {'realm':'legacy'})
                    self.assertTrue(result.is_error)
                    images = [item for item in result.content if item.type == 'image']
                    self.assertEqual(len(images), 1)
                    self.assertEqual(images[0].mime_type, 'image/png')
                    self.assertEqual(result.structured_content['code'], 'CAPTCHA_REQUIRED')
        anyio.run(run)

    def test_handshake_discovery_validation_and_task_reset(self):
        import anyio
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        async def run():
            script = str(Path(__file__).resolve().parents[1] / 'scripts/serve.py')
            async with stdio_client(StdioServerParameters(command=sys.executable, args=[script,'--transport','mcp'])) as (r,w):
                async with ClientSession(r,w) as session:
                    init = await session.initialize()
                    tools = await session.list_tools()
                    self.assertEqual(init.server_info.name, 'ruc-campus')
                    self.assertIn('ruc_browser_open', [t.name for t in tools.tools])
                    result = await session.call_tool('ruc_capabilities', {'keyword':'booths.'})
                    self.assertFalse(result.is_error)
                    self.assertGreaterEqual(len(result.structured_content['capabilities']), 8)
                    bad = await session.call_tool('ruc_query', {'capability':'not-registered'})
                    self.assertTrue(bad.is_error)
                    reset = await session.call_tool('ruc_reset_session', {})
                    self.assertFalse(reset.is_error)
        anyio.run(run)
