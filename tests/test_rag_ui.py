import shutil
import subprocess
import unittest
from pathlib import Path

from pydantic import ValidationError

from backend.schemas import (
    KnowledgeSearchRequest,
    KnowledgeSourceResponse,
    KnowledgeSourcesResponse,
    RAGEnabledRequest,
)


ROOT = Path(__file__).resolve().parents[1]


class RAGSchemaTest(unittest.TestCase):
    def test_switch_and_search_requests_are_strict_and_bounded(self):
        self.assertTrue(RAGEnabledRequest(enabled=True).enabled)
        self.assertEqual(KnowledgeSearchRequest(query="  部署说明  ").query, "部署说明")
        self.assertEqual(KnowledgeSearchRequest(query="query").top_k, 6)
        for payload in ({"enabled": 1}, {"enabled": "true"}, {"enabled": True, "extra": 1}):
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                RAGEnabledRequest.model_validate(payload)
        for payload in (
            {"query": "   "},
            {"query": "q", "top_k": True},
            {"query": "q", "top_k": 0},
            {"query": "q", "top_k": 11},
            {"query": "q", "extra": 1},
        ):
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                KnowledgeSearchRequest.model_validate(payload)

    def test_source_projection_has_no_body_or_path_field(self):
        response = KnowledgeSourcesResponse(
            run_id="run-1",
            project_id="project-1",
            sources=[KnowledgeSourceResponse(
                citation_id="K1",
                chunk_id="chunk-1",
                title="guide.md",
                locator={"line_start": 4, "line_end": 8},
                available=False,
                unavailable_reason="deleted",
            )],
        ).model_dump()
        serialized = str(response)
        self.assertNotIn("content", serialized)
        self.assertNotIn("stored_path", serialized)


class RAGStaticUiContractTest(unittest.TestCase):
    def test_knowledge_controls_and_routes_are_present(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        for element_id in (
            "ragCapability", "ragEnabledToggle", "knowledgeFileInput",
            "knowledgeDocumentList", "knowledgeSearchForm", "knowledgeSearchResults",
            "runKnowledgeSources", "citationDetail",
        ):
            self.assertIn(f'id="{element_id}"', html)
        for route in (
            '/knowledge"', '/rag-enabled"', '/knowledge/search"', '/knowledge-sources"',
        ):
            self.assertIn(route, js)
        self.assertIn(".txt,.md,.json,.log", html)
        self.assertIn("unknown", js)
        self.assertIn("unavailable", js)

    def test_rag_renderers_use_text_content(self):
        js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        names = (
            "renderKnowledgeDocuments", "renderKnowledgeSearchResults",
            "renderRunSources", "showCitationDetail", "appendCitationMap",
        )
        for name in names:
            start = js.index(f"function {name}(")
            end = js.find("\n\nfunction ", start + 1)
            block = js[start:end if end >= 0 else None]
            with self.subTest(function=name):
                self.assertIn("textContent", block)
                self.assertNotIn("innerHTML", block)


class RAGUiBehaviorTest(unittest.TestCase):
    def test_sources_are_run_scoped_and_stale_detail_cannot_overwrite(self):
        if not shutil.which("node"):
            self.skipTest("Node.js is unavailable")
        script = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const nodes = new Map();
function node(tag='div') {
  return {tag, children: [], textContent: '', className: '', title: '', value: '', checked: false,
    hidden: false, disabled: false, style: {}, listeners: {},
    classList: {add() {}, remove() {}, toggle() {}, contains() {return false;}},
    addEventListener(name, callback) {this.listeners[name] = callback;},
    appendChild(child) {this.children.push(child); return child;},
    replaceChildren() {this.children = [];},
    remove() {this.removed = true;},
    querySelector(selector) {return this.children.find((child) =>
      selector.startsWith('.') && child.className === selector.slice(1)) || null;},
    querySelectorAll() {return [];}};
}
const context = {
  document: {getElementById(id) {if (!nodes.has(id)) nodes.set(id, node()); return nodes.get(id);},
    createElement: node, querySelectorAll() {return []; }},
  window: {prompt() {return null;}, confirm() {return true;}},
  localStorage: {getItem() {return null;}, setItem() {}, removeItem() {}},
  fetch: async () => ({ok:false, status:401, json:async()=>({})}),
  console: {log() {}, error(...args) {process.stderr.write(args.join(' ') + '\n');}},
  setTimeout() {}, clearTimeout() {}, FormData: class {},
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
const render = vm.runInContext('renderRunSources', context);
const detail = vm.runInContext('showCitationDetail', context);
const append = vm.runInContext('appendBubble', context);
const clear = vm.runInContext('clearRunSources', context);
(async () => {
  vm.runInContext(`
    activeRunId = 'run-new';
    runSourceMap.set('K1', {citation_id:'K1', title:'wrong-current.md', available:true});
    runSourcesByRun.set('run-old', new Map([['K1',
      {citation_id:'K1', title:'old.md', locator:{line_start:4}, available:true}]]));
    runSourcesByRun.set('run-new', new Map([['K1',
      {citation_id:'K1', title:'new.md', locator:{line_start:9}, available:true}]]));
  `, context);
  const history = append('assistant', 'historic answer [K1]', 'run-old');
  const citationMap = history.body.children.find((child) => child.className === 'citation-map');
  assert.strictEqual(citationMap.children[0].title, 'old.md');

  render({sources:[
    {citation_id:'K1', chunk_id:'c1', title:'new.md', locator:{line_start:9}, available:true},
    {citation_id:'K2', chunk_id:'c2', title:'gone.md', locator:{line_start:10}, available:false,
     unavailable_reason:'deleted'},
  ]}, 'run-new');
  assert.strictEqual(nodes.get('runKnowledgeSources').children.length, 2);

  let resolveOld;
  const requested = [];
  context.fetch = async (url) => {
    requested.push(url);
    if (url.includes('/run-old/')) return new Promise((resolve) => { resolveOld = resolve; });
    return {ok:true, status:200, json:async()=>({citation_id:'K1', title:'new.md',
      locator:{line_start:9}, available:true, preview:'new preview'})};
  };
  const oldRequest = detail('run-old', 'K1');
  clear();
  await detail('run-new', 'K1');
  assert(nodes.get('citationDetail').textContent.includes('new preview'));
  resolveOld({ok:true, status:200, json:async()=>({citation_id:'K1', title:'old.md',
    locator:{line_start:4}, available:true, preview:'stale preview'})});
  await oldRequest;
  assert(nodes.get('citationDetail').textContent.includes('new preview'));
  assert(!nodes.get('citationDetail').textContent.includes('stale preview'));
  assert(requested.includes('/api/runs/run-old/knowledge-sources/K1'));
  assert(requested.includes('/api/runs/run-new/knowledge-sources/K1'));
  await detail('run-new', 'K9');
  assert(nodes.get('citationDetail').textContent.includes('unknown'));
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
        result = subprocess.run(
            ["node", "-e", script, str(ROOT / "static" / "app.js")],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
