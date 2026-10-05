"""单文件全文规则回归：全部离线，仅临时库，禁止真实模型请求。"""
import base64
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixtures import make_test_vault
from test_note_processor import FakeNoteModel, make_processor, make_suggestion
from test_server import ServerHarness
from pka_graph.content_fetcher import read_local_file, fetch_public_url
from pka_graph.discovery_model import DiscoveryBudget, HttpDiscoveryAnalyzer, SemanticModelError
from pka_graph.discovery import DiscoveryService
from pka_graph.inbox import InboxWatcher, read_pdf_text
from pka_graph.note_processor import NoteBudget, HttpNoteModel, NoteModelError, segment_material
from pka_graph.service import GraphService
from pka_graph.store import StateStore


class SingleFileFulltextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.vault = make_test_vault(self.root)
        self.skills = self.root / "skills"
        self.skills.mkdir()
        (self.skills / "2026-10-04-通用.md").write_text("依据原文，全文处理，不编造。",encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_and_legacy_env_cannot_restore_old_budgets(self):
        with patch.dict(os.environ,{"PKA_NOTE_DAILY_LIMIT":"48","PKA_NOTE_MAX_CALLS":"10",
                                  "PKA_DISCOVERY_MAX_INPUT_CHARS":"5000"}):
            budget = NoteBudget.from_env()
            discovery = DiscoveryBudget.from_env()
        self.assertEqual(budget.daily_model_calls_limit,0)
        self.assertEqual(budget.per_note_calls_cap,0)
        self.assertEqual(discovery.max_input_chars,0)
        self.assertEqual(discovery.daily_model_calls_limit,24)
        self.assertGreaterEqual(budget.max_retries,0)
        self.assertLessEqual(budget.max_retries,3)

    def test_entire_long_file_segments_are_kept(self):
        text = "# 原文\n" + "原始正文。" * 20000 + "末尾证据"
        segments, overflow = segment_material(text,4000)
        self.assertGreater(len(segments),9)
        self.assertEqual(overflow,[])
        self.assertEqual("".join(s["text"] for s in segments),text)

    def test_default_processor_handles_more_than_old_daily_limit(self):
        model = FakeNoteModel()
        processor = make_processor(model,self.skills,self.vault)
        text = "# 原文机制\n" + "系统通过记录状态与检查结果保持一致性。" * 12000 + "末尾完整证据。"
        result = processor.process(suggestion=make_suggestion(self.vault,full_text=text),vault_root=self.vault)
        self.assertTrue(result["complete"])
        self.assertTrue(result["publishable"])
        self.assertGreater(result["calls"],48)
        self.assertEqual(processor.used_today(),result["calls"])
        self.assertIn("末尾完整证据。", "".join(model.segment_texts))
        self.assertNotIn("**原文**", result["body"])
        self.assertEqual("".join(model.segment_texts),text)

    def test_multiple_readable_files_rejected_before_model(self):
        model = FakeNoteModel()
        processor = make_processor(model,self.skills,self.vault)
        suggestion = make_suggestion(self.vault)
        suggestion["sources"].append({"path":"second.md","read_status":"readable"})
        result = processor.process(suggestion=suggestion,vault_root=self.vault)
        self.assertFalse(result["ok"])
        self.assertIn("一个来源文件",result["reason"])
        self.assertEqual(model.calls,[])

    def test_single_section_note_over_8000_is_not_cut(self):
        model = HttpNoteModel("https://offline.invalid/v1","synthetic","offline",NoteBudget())
        note = "我记录原文的机制与边界。" * 1000 + "尾部论证"
        output = "标题：完整笔记\n结论：完整结论\n[小标题：s1]\n原理\n[笔记：s1]\n"+note
        with patch.object(model,"_chat",return_value=output):
            parsed = model.compose_note("规范","原题",[],{},[])
        self.assertGreater(len(note),8000)
        self.assertEqual(parsed["notes"]["s1"],note)

    def test_pdf_all_pages_and_all_characters_by_default(self):
        import fitz
        path = self.root / "long.pdf"
        with fitz.open() as doc:
            for i in range(45):
                page = doc.new_page()
                page.insert_textbox(fitz.Rect(40,40,550,790),
                                    f"PAGE {i+1}\n"+"Original complete text for evidence and verification.\n"*50,fontsize=7)
            doc.save(path)
        text,error,meta = read_pdf_text(path)
        self.assertIsNone(error)
        self.assertEqual(meta["read_pages"],45)
        self.assertFalse(meta["truncated"])
        self.assertGreater(len(text),20000)
        self.assertIn("PAGE 45",text)

    def test_explicit_limited_pdf_read_remains_honest(self):
        import fitz
        path = self.root / "limited.pdf"
        with fitz.open() as doc:
            for i in range(3):
                doc.new_page().insert_text((40,50),"Long original sentence "*10)
            doc.save(path)
        text,error,meta = read_pdf_text(path,max_pages=1,max_chars=50)
        self.assertIsNone(error)
        self.assertTrue(meta["truncated"])
        self.assertLessEqual(len(text),50)

    def test_local_file_over_500kb_read_in_full(self):
        path = self.root / "full.txt"
        original = "evidence "*70000 + "FINAL_EVIDENCE"
        path.write_text(original,encoding="utf-8")
        result = read_local_file(str(path),self.root)
        self.assertEqual(result["text"],original)

    def test_public_fetch_over_old_limits_read_in_full_offline(self):
        original = "Original source body.\n"*30000 + "FINAL_EVIDENCE"
        response = io.BytesIO(original.encode())
        response.headers = {"Content-Type":"text/plain"}
        response.url = "https://public.invalid/paper"
        with patch("urllib.request.build_opener") as opener:
            opener.return_value.open.return_value = response
            result = fetch_public_url(response.url)
        self.assertEqual(result["status"],"ok")
        self.assertEqual(result["text"],original)
        self.assertFalse(result["truncated"])

    def test_explicit_fetch_limit_fails_instead_of_partial_ok(self):
        response = io.BytesIO(b"body over limit")
        response.headers = {"Content-Type":"text/plain"}
        response.url = "https://public.invalid/paper"
        with patch("urllib.request.build_opener") as opener:
            opener.return_value.open.return_value = response
            result = fetch_public_url(response.url,max_chars=3)
        self.assertEqual(result["status"],"too_large")
        self.assertEqual(result["text"],"")

    def test_discovery_request_contains_complete_tail_offline(self):
        original = "完整公开正文。"*10000 + "FINAL_SOURCE_EVIDENCE"
        response = io.BytesIO(json.dumps({"choices":[{"finish_reason":"stop","message":{"content":'{"classification":"new_node"}'}}]}).encode())
        model = HttpDiscoveryAnalyzer("https://offline.invalid/v1","synthetic","offline",DiscoveryBudget())
        with patch("urllib.request.urlopen",return_value=response) as request:
            model.analyze({"title":"论文","text":original},[],[],[])
        body = json.loads(request.call_args.args[0].data)
        self.assertIn(original,body["messages"][1]["content"])
        self.assertNotIn("已截断",body["messages"][1]["content"])

    def test_explicit_discovery_limit_fails_before_request(self):
        model = HttpDiscoveryAnalyzer("https://offline.invalid/v1","synthetic","offline",DiscoveryBudget(max_input_chars=5))
        with patch("urllib.request.urlopen") as request:
            with self.assertRaises(SemanticModelError) as error:
                model.analyze({"text":"more than five characters"},[],[],[])
        self.assertEqual(error.exception.category,"input_limit")
        request.assert_not_called()

    def test_discovery_truncated_provider_response_fails(self):
        response = io.BytesIO(json.dumps({"choices":[{"finish_reason":"length","message":{"content":'{"classification":"new_node"}'}}]}).encode())
        model = HttpDiscoveryAnalyzer("https://offline.invalid/v1","synthetic","offline",DiscoveryBudget())
        with patch("urllib.request.urlopen",return_value=response):
            with self.assertRaises(SemanticModelError) as error:
                model.analyze({"text":"original"},[],[],[])
        self.assertEqual(error.exception.category,"context_limit")

    def test_note_truncated_provider_response_fails_without_loop(self):
        model = HttpNoteModel("https://offline.invalid/v1","synthetic","offline",NoteBudget())
        with patch.object(model,"_send",return_value={"choices":[{"finish_reason":"length","message":{"content":"partial"}}]}) as request:
            with self.assertRaises(NoteModelError) as error:
                model._chat("system","public source")
        self.assertEqual(error.exception.category,"context_limit")
        self.assertEqual(request.call_count,1)

    def test_upload_over_old_20mb_limit_is_queued(self):
        gs = GraphService(StateStore(self.vault))
        inbox = InboxWatcher(gs)
        raw = b"public text\n" * 2000000
        self.assertGreater(len(raw),20*1024*1024)
        with patch.object(inbox,"_quick_scan"):
            result = inbox.ingest_bytes("one.txt",base64.b64encode(raw).decode())
        self.assertEqual((inbox.inbox_dir/result["queued"]).read_bytes(),raw)

    def test_upload_arrays_are_rejected(self):
        from pka_graph.models import ValidationError
        inbox = InboxWatcher(GraphService(StateStore(self.vault)))
        with self.assertRaises(ValidationError):
            inbox.ingest_bytes(["first.txt","second.txt"],"eA==")

    def test_api_rejects_multifile_payload(self):
        with ServerHarness() as server:
            code,body = server.post("/api/inbox/ingest",{"files":[{"name":"a.txt"},{"name":"b.txt"}]})
        self.assertEqual(code,400)
        self.assertIn("一个文件",body["error"])

    def test_note_jobs_are_serial(self):
        processor = make_processor(FakeNoteModel(),self.skills,self.vault)
        first_entered = threading.Event()
        second_entered = threading.Event()
        release = threading.Event()
        def one(**kwargs):
            if kwargs["suggestion"]["name"] == "first":
                first_entered.set()
                release.wait(2)
            else:
                second_entered.set()
            return {"ok":True}
        with patch.object(processor,"_process_one",side_effect=one):
            first = threading.Thread(target=lambda:processor.process(suggestion={"name":"first"},vault_root=self.vault))
            second = threading.Thread(target=lambda:processor.process(suggestion={"name":"second"},vault_root=self.vault))
            first.start()
            self.assertTrue(first_entered.wait(1))
            second.start()
            try:
                self.assertFalse(second_entered.wait(0.1))
            finally:
                release.set()
                first.join(2)
                second.join(2)
            self.assertTrue(second_entered.is_set())

    def test_note_and_discovery_share_serial_boundary(self):
        gs = GraphService(StateStore(self.vault))
        disc = DiscoveryService(gs,upstream=[])
        processor = make_processor(FakeNoteModel(),self.skills,self.vault)
        processor._process_lock = gs.material_lock
        note_entered = threading.Event()
        check_entered = threading.Event()
        release = threading.Event()
        def process(**kwargs):
            note_entered.set()
            release.wait(2)
            return {"ok":True}
        def check(*args):
            check_entered.set()
            return {}
        with patch.object(processor,"_process_one",side_effect=process),patch.object(disc,"_check_inner",side_effect=check):
            first = threading.Thread(target=lambda:processor.process(suggestion={},vault_root=self.vault))
            second = threading.Thread(target=disc.check)
            first.start()
            self.assertTrue(note_entered.wait(1))
            second.start()
            try:
                self.assertFalse(check_entered.wait(0.1))
            finally:
                release.set()
                first.join(2)
                second.join(2)
            self.assertTrue(check_entered.is_set())


if __name__ == "__main__":
    unittest.main()
