"""Connect your agent: agent tokens (agents.py), the setup prompt (agent_setup.py), their routes and MCP at /mcp.

The server runs in-process with the same fakes as test_server.ServerCase."""
from __future__ import annotations

import json
import os
import stat
import unittest

from medialab import agent_setup, agents, mcp_routes, store
from tests.test_server import ServerCase

TOKEN = "owner-secret-token-0123456789"
OWNER = {"Authorization": "Bearer " + TOKEN}


class AgentsPure(unittest.TestCase):
    def test_deny_list(self):
        self.assertFalse(agents.allowed("POST", "/api/delete"))
        self.assertFalse(agents.allowed("POST", "/api/projects/p1/delete"))
        self.assertFalse(agents.allowed("GET", "/api/agents"))
        self.assertFalse(agents.allowed("POST", "/api/agents/ag-1/revoke"))
        self.assertFalse(agents.allowed("POST", "/api/google/disconnect"))
        self.assertTrue(agents.allowed("POST", "/api/generate"))
        self.assertTrue(agents.allowed("POST", "/api/projects/p1"))
        self.assertTrue(agents.allowed("GET", "/api/recent"))
        self.assertTrue(agents.allowed("POST", "/mcp"))

    def test_prompt_with_token_and_no_google(self):
        p = agent_setup.build_prompt("claude", {"base_url": "http://127.0.0.1:8190/", "token": "mla_x",
                                                "token_required": True, "agent_name": "Helper",
                                                "google": None, "columns": ["id", "when"]})
        self.assertIn("claude mcp add --transport http media-lab http://127.0.0.1:8190/mcp", p)
        self.assertIn('--header "Authorization: Bearer mla_x"', p)
        self.assertIn("Connect > Google", p)
        self.assertIn("columns: id, when", p)
        self.assertIn("never delete", p)
        self.assertIn("Never post", p)

    def test_prompt_without_token_says_local_only(self):
        p = agent_setup.build_prompt("generic", {"base_url": "http://127.0.0.1:8190", "token": None,
                                                 "token_required": False, "google": None, "columns": []})
        self.assertIn("none needed", p)
        self.assertNotIn("Bearer", p)

    def test_prompt_is_true_for_a_tailnet_install_without_a_token(self):
        # audit 2026-10-11 (high): published on the tailnet with no token, the prompt said the app "only listens
        # on this computer" and that agents cannot delete; in fact any tailnet client had the owner's access
        p = agent_setup.build_prompt("generic", {"base_url": "http://studio-box:8196", "token": None,
                                                 "token_required": False, "listens": "192.0.2.10",
                                                 "loopback_only": False, "google": None, "columns": []})
        self.assertNotIn("only on this computer", p)
        self.assertNotIn("refused for agent tokens", p)
        self.assertIn("listens on 192.0.2.10 without a token", p)
        self.assertIn("not only this computer, has the owner's full access, including delete", p)
        self.assertIn("nothing stops a delete here", p)
        p = agent_setup.build_prompt("generic", {"base_url": "http://box:8190", "token": None, "listens": "0.0.0.0",
                                                 "google": None, "columns": []})
        self.assertIn("every network address of its computer", p)

    def test_prompt_on_loopback_without_token_says_so_and_admits_full_access(self):
        p = agent_setup.build_prompt("generic", {"base_url": "http://127.0.0.1:8190", "token": None,
                                                 "token_required": False, "google": None, "columns": []})
        self.assertIn("listens only on this computer (127.0.0.1)", p)
        self.assertIn("full access, including delete", p)

    def test_prompt_with_token_says_agents_cannot_delete(self):
        p = agent_setup.build_prompt("generic", {"base_url": "http://studio-box:8196", "token": "mla_x",
                                                 "token_required": True, "listens": "192.0.2.10",
                                                 "loopback_only": False, "google": None, "columns": []})
        self.assertIn("scoped: it can make and schedule, not delete", p)
        self.assertIn("refused for agent tokens", p)

    def test_prompt_with_google_workspace(self):
        g = {"root_url": "https://drive.google.com/drive/folders/R", "sheet_url": "https://docs.google.com/s/S",
             "folders": {"Inbox": "https://drive.google.com/drive/folders/I"}}
        p = agent_setup.build_prompt("hermes", {"base_url": "http://h", "token": None, "google": g,
                                                "columns": ["id"]})
        self.assertIn("Content Plan Sheet: https://docs.google.com/s/S", p)
        self.assertIn("Inbox/: https://drive.google.com/drive/folders/I", p)
        self.assertNotIn("not connected yet", p)

    def test_bad_preset(self):
        with self.assertRaises(ValueError):
            agent_setup.build_prompt("nope", {})


class AgentTokenRoutes(ServerCase):
    def cfg_overrides(self):
        return {"token": TOKEN}

    def make_agent(self, name="Helper", preset="claude"):
        code, _, j = self.req("POST", "/api/agents", {"name": name, "preset": preset}, headers=OWNER)
        self.assertEqual(code, 200, j)
        tok = next(w for w in j["prompt"].split() if w.startswith("mla_"))
        return j, tok.strip('"')

    def test_create_shows_token_once_and_stores_only_hash(self):
        j, tok = self.make_agent()
        self.assertTrue(j["token_shown_once"])
        self.assertTrue(agents.TOKEN_RE.fullmatch(tok), tok)
        path = store.data_path("config", "agents.json")
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
        self.assertNotIn(tok, raw)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        code, _, lst = self.req("GET", "/api/agents", headers=OWNER)
        self.assertEqual(code, 200)
        self.assertNotIn("sha256", json.dumps(lst))

    def test_agent_token_can_make_but_not_delete_or_manage(self):
        _, tok = self.make_agent()
        ag = {"Authorization": "Bearer " + tok}
        code, _, j = self.req("POST", "/api/generate", {"prompt": "a cat"}, headers=ag)
        self.assertEqual(code, 200, j)
        self.own("gen", "img_00001_.png")
        code, _, j = self.req("POST", "/api/delete", {"ref": {"root": "gen", "name": "img_00001_.png"}},
                              headers=ag)
        self.assertEqual(code, 403, j)
        self.assertIn("agent tokens", j["error"])
        self.assertEqual(self.req("GET", "/api/agents", headers=ag)[0], 403)
        self.assertEqual(self.req("POST", "/api/agents", {"name": "x"}, headers=ag)[0], 403)

    def test_revoked_and_wrong_tokens_fail(self):
        j, tok = self.make_agent()
        code, _, _ = self.req("POST", f"/api/agents/{j['agent']['id']}/revoke", {}, headers=OWNER)
        self.assertEqual(code, 200)
        self.assertEqual(self.req("GET", "/api/recent", headers={"Authorization": "Bearer " + tok})[0], 401)
        self.assertEqual(self.req("GET", "/api/recent", headers={"Authorization": "Bearer mla_" + "A" * 43})[0], 401)

    def test_mcp_without_csrf_header_but_json_only(self):
        _, tok = self.make_agent()
        ag = {"Authorization": "Bearer " + tok}
        msg = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}
        code, _, j = self.req("POST", "/mcp", msg, headers=ag, csrf=False)
        self.assertEqual(code, 200, j)
        self.assertEqual(j["result"]["serverInfo"]["name"], "media-lab")
        code, _, _ = self.req("POST", "/mcp", raw=b"{}", headers=ag, csrf=False, ctype="text/plain")
        self.assertEqual(code, 403)
        code, _, _ = self.req("POST", "/mcp", msg, csrf=False)
        self.assertEqual(code, 401)
        code, _, _ = self.req("POST", "/mcp", msg, headers=dict(ag, Origin="http://evil.example"), csrf=False)
        self.assertEqual(code, 403)

    def test_mcp_tools_list_hides_absent_routes_and_calls_through(self):
        _, tok = self.make_agent()
        ag = {"Authorization": "Bearer " + tok}
        code, _, j = self.req("POST", "/mcp", {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=ag,
                              csrf=False)
        names = {t["name"] for t in j["result"]["tools"]}
        self.assertIn("generate_picture", names)
        self.assertIn("job_status", names)
        for name in names:
            self.assertIn(name, mcp_routes.BY_NAME)
        code, _, j = self.req("POST", "/mcp", {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "generate_picture", "arguments": {"prompt": "a lighthouse"}}}, headers=ag, csrf=False)
        res = j["result"]
        self.assertFalse(res["isError"], res)
        jid = res["structuredContent"]["job"]
        self.wait_job(jid)
        code, _, j = self.req("POST", "/mcp", {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
            "name": "job_status", "arguments": {"job": jid, "wait_seconds": 3}}}, headers=ag, csrf=False)
        self.assertEqual(j["result"]["structuredContent"]["state"], "done", j)

    def test_mcp_notification_and_unknown_method(self):
        _, tok = self.make_agent()
        ag = {"Authorization": "Bearer " + tok}
        code, _, _ = self.req("POST", "/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"},
                              headers=ag, csrf=False)
        self.assertEqual(code, 202)
        code, _, j = self.req("POST", "/mcp", {"jsonrpc": "2.0", "id": 9, "method": "resources/list"},
                              headers=ag, csrf=False)
        self.assertEqual(j["error"]["code"], -32601)


class NoTokenServer(ServerCase):
    def test_prompt_says_no_token_needed(self):
        code, _, j = self.req("POST", "/api/agents", {"name": "Local", "preset": "generic"})
        self.assertEqual(code, 200, j)
        self.assertFalse(j["token_shown_once"])
        self.assertNotIn("mla_", j["prompt"])
        self.assertIn("none needed", j["prompt"])



class AgentRoutesTailnetNoToken(ServerCase):
    """The preview's shape: published on a tailnet address, MEDIA_LAB_REQUIRE_TOKEN=0, no token."""

    def cfg_overrides(self):
        return {"published_host": "192.0.2.10", "require_token": "0"}

    def test_config_states_the_facts_and_the_prompt_matches(self):
        code, _, c = self.req("GET", "/api/config")
        self.assertEqual(code, 200)
        self.assertEqual((c["token_required"], c["listens"], c["loopback_only"]), (False, "192.0.2.10", False))
        code, _, j = self.req("POST", "/api/agents", {"name": "Helper", "preset": "generic"})
        self.assertEqual(code, 200, j)
        self.assertFalse(j["token_shown_once"])
        self.assertIn("listens on 192.0.2.10 without a token", j["prompt"])
        self.assertNotIn("only on this computer", j["prompt"])

if __name__ == "__main__":
    unittest.main()
