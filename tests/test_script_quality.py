import importlib.util
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

try:
    import requests
except ModuleNotFoundError:
    class _RequestsStub:
        def post(self, *args, **kwargs):
            raise RuntimeError("requests is not installed")
    requests = _RequestsStub()
    sys.modules["requests"] = requests
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DEV_SRC = REPO_ROOT / "dev" / "src"
os.environ.setdefault("WORK_DIR", tempfile.mkdtemp(prefix="script_quality_import_"))
sys.path.insert(0, str(DEV_SRC))

spec = importlib.util.spec_from_file_location("script0", DEV_SRC / "0_script.py")
script0 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(script0)


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = json.dumps(body, ensure_ascii=False)

    def json(self):
        return self._body


def issue_codes(report):
    return {issue["code"] for issue in report["errors"]}


def warning_codes(report):
    return {issue["code"] for issue in report["warnings"]}


def comparison_strategy(evidence_status="sufficient", targets=None):
    return {
        "topic": "소주와 맥주 비교",
        "intent_type": "comparison",
        "viewer_question": "소주와 맥주 중 무엇이 나을까요?",
        "answer_requirement": "조건별 선택 기준을 답할 것",
        "comparison_targets": targets if targets is not None else ["소주", "맥주"],
        "comparison_criteria": ["알코올 양", "혈당", "수면"],
        "evidence_status": evidence_status,
        "required_beats": ["두 대상을 같은 기준으로 설명", "최종 답 제시"],
        "title_promise": "소주와 맥주 선택 기준",
    }


def complete_result():
    return {
        "title": "소주와 맥주 선택 기준",
        "summary": "조건별로 덜 해로운 선택 기준을 설명합니다.",
        "description": "소주와 맥주의 차이를 차분히 설명합니다.",
        "final_answer": "소주와 맥주는 모두 줄이는 것이 우선이고, 혈당과 수면이 걱정되면 양과 빈도를 기준으로 선택해야 합니다.",
        "promise_fulfilled": True,
        "evidence_limit": "개인 질환과 복용 약에 따라 달라질 수 있습니다.",
        "scenes": [
            {"text": "소주와 맥주 중 무엇이 나은지 궁금하셨죠", "visual_query": "senior table conversation"},
            {"text": "소주는 적은 양에도 알코올이 빨리 늘 수 있습니다", "visual_query": "small glass table"},
            {"text": "맥주는 양이 늘기 쉬워 혈당과 수면을 함께 봐야 합니다", "visual_query": "elderly evening kitchen"},
        ],
    }


class StrategyResponse:
    def __init__(self, stop_reason="max_tokens", text='{"partial": "value'):
        self.status_code = 200
        self.headers = {}
        self._data = {
            "id": "msg_test",
            "stop_reason": stop_reason,
            "usage": {"input_tokens": 100, "output_tokens": 2000},
            "content": [{"type": "text", "text": text}],
        }
        self.text = text

    def json(self):
        return dict(self._data)


class ScriptQualityTests(unittest.TestCase):
    def test_stage1_uses_each_model_once_then_continues_with_local_strategy(self):
        calls = []
        old_post = requests.post
        old_models = (
            script0.CLAUDE_STRATEGY_MODEL,
            script0.CLAUDE_STRATEGY_FALLBACK_MODELS,
            script0.CLAUDE_SCRIPT_MODEL,
            script0.CLAUDE_STRATEGY_MAX_TOKENS,
            script0.STRATEGY_PATH,
            script0.CLAUDE_USAGE_PATH,
            os.environ.get("ANTHROPIC_API_KEY"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            try:
                os.environ["ANTHROPIC_API_KEY"] = "test-key"
                script0.CLAUDE_STRATEGY_MODEL = "claude-haiku-4-5-20251001"
                script0.CLAUDE_STRATEGY_FALLBACK_MODELS = ("claude-sonnet-4-5-20250929",)
                script0.CLAUDE_SCRIPT_MODEL = "claude-sonnet-4-6"
                script0.CLAUDE_STRATEGY_MAX_TOKENS = 2000
                script0.STRATEGY_PATH = str(Path(tmp, "strategy.json"))
                script0.CLAUDE_USAGE_PATH = str(Path(tmp, "usage.jsonl"))

                def fake_post(url, headers, json, timeout):
                    calls.append(dict(json))
                    return StrategyResponse()

                requests.post = fake_post
                with contextlib.redirect_stdout(io.StringIO()):
                    strategy = script0.plan_strategy("숙취 다음날 세 가지 습관")

                self.assertEqual(
                    [call["model"] for call in calls],
                    [
                        "claude-haiku-4-5-20251001",
                        "claude-sonnet-4-5-20250929",
                        "claude-sonnet-4-6",
                    ],
                )
                self.assertTrue(all(call["max_tokens"] == 2000 for call in calls))
                self.assertTrue(all(call["output_config"]["format"]["type"] == "json_schema" for call in calls))
                self.assertEqual(strategy["strategy_source"], "local_fallback")
                self.assertTrue(Path(script0.STRATEGY_PATH).exists())
            finally:
                requests.post = old_post
                (
                    script0.CLAUDE_STRATEGY_MODEL,
                    script0.CLAUDE_STRATEGY_FALLBACK_MODELS,
                    script0.CLAUDE_SCRIPT_MODEL,
                    script0.CLAUDE_STRATEGY_MAX_TOKENS,
                    script0.STRATEGY_PATH,
                    script0.CLAUDE_USAGE_PATH,
                    old_api_key,
                ) = old_models
                if old_api_key is None:
                    os.environ.pop("ANTHROPIC_API_KEY", None)
                else:
                    os.environ["ANTHROPIC_API_KEY"] = old_api_key

    def test_stage2_uses_evidence_brief_for_claude_strategy(self):
        strategy = comparison_strategy()
        strategy.update({"strategy_source": "claude", "evidence_brief": [{"claim": "핵심 결과", "source": "PubMed", "year": "2025", "caveat": "표본 제한"}]})
        context = script0.stage2_research_context(strategy, "원문" * 1000, "검색" * 1000)
        self.assertIn("핵심 결과", context)
        self.assertNotIn("원문", context)
        self.assertNotIn("검색", context)

    def test_stage2_falls_back_to_raw_research_for_old_or_local_strategy(self):
        context = script0.stage2_research_context(comparison_strategy(), "PubMed 원문", "웹 근거")
        self.assertIn("PubMed 원문", context)
        self.assertIn("웹 근거", context)

    def test_usage_log_keeps_cache_and_web_search_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_path = script0.CLAUDE_USAGE_PATH
            try:
                script0.CLAUDE_USAGE_PATH = str(Path(tmp, "usage.jsonl"))
                script0.record_claude_usage("research", "haiku", {"usage": {"input_tokens": 10, "output_tokens": 5, "cache_creation_input_tokens": 3, "cache_read_input_tokens": 7, "server_tool_use": {"web_search_requests": 2}}})
                entry = json.loads(Path(script0.CLAUDE_USAGE_PATH).read_text(encoding="utf-8"))
                self.assertEqual(entry["cache_read_input_tokens"], 7)
                self.assertEqual(entry["web_search_requests"], 2)
            finally:
                script0.CLAUDE_USAGE_PATH = old_path
    def test_source_mix_defaults_to_empty_when_absent(self):
        strategy = script0.normalize_strategy_contract({}, "야간뇨 원인")
        self.assertEqual(
            strategy["source_mix"],
            {"case_summary": "", "case_source": "", "stat_value": "", "stat_source": ""},
        )

    def test_build_prompt_omits_source_mix_instruction_when_empty(self):
        strategy = script0.normalize_strategy_contract(script0.local_strategy_fallback("야간뇨"), "야간뇨")
        prompt = script0.build_prompt(strategy, abstracts="", trend_context=None, web_research="", feedback_insights="")
        no_mix_strategy = dict(strategy)
        no_mix_strategy.pop("source_mix", None)
        prompt_without_source_mix_key = script0.build_prompt(
            no_mix_strategy, abstracts="", trend_context=None, web_research="", feedback_insights=""
        )
        self.assertNotIn("실사례·통계 활용 지침", prompt)
        self.assertEqual(prompt, prompt_without_source_mix_key)

    def test_build_prompt_includes_source_mix_instruction_when_present(self):
        strategy = comparison_strategy()
        strategy["source_mix"] = {
            "case_summary": "50대 직장인이 야간뇨로 밤잠을 설쳤다는 사례",
            "case_source": "하이닥",
            "stat_value": "50대 야간뇨 유병률은 29퍼센트다",
            "stat_source": "국내 배뇨장애 연구",
        }
        prompt = script0.build_prompt(strategy, abstracts="", trend_context=None, web_research="", feedback_insights="")
        self.assertIn("실사례·통계 활용 지침", prompt)
        self.assertIn("하이닥", prompt)

    def test_case_research_fetch_returns_empty_on_none_found(self):
        old_loop = script0._call_claude_loop
        try:
            script0._call_claude_loop = lambda *a, **k: {
                "content": [{"type": "text", "text": "NONE_FOUND"}],
                "usage": {},
            }
            with contextlib.redirect_stdout(io.StringIO()):
                result = script0.fetch_case_and_stat_research("존재하지않는주제", "nonexistent topic")
            self.assertEqual(result, "")
        finally:
            script0._call_claude_loop = old_loop

    def test_case_research_fetch_returns_empty_on_tool_error(self):
        old_loop = script0._call_claude_loop
        try:
            script0._call_claude_loop = lambda *a, **k: {
                "content": [{
                    "type": "web_search_tool_result",
                    "content": {"type": "web_search_tool_result_error", "error_code": "rate_limited"},
                }],
                "usage": {},
            }
            with contextlib.redirect_stdout(io.StringIO()):
                result = script0.fetch_case_and_stat_research("주제", "topic")
            self.assertEqual(result, "")
        finally:
            script0._call_claude_loop = old_loop

    def test_case_research_fetch_continues_on_exception(self):
        old_loop = script0._call_claude_loop
        try:
            def raise_exc(*a, **k):
                raise RuntimeError("network down")
            script0._call_claude_loop = raise_exc
            with contextlib.redirect_stdout(io.StringIO()):
                result = script0.fetch_case_and_stat_research("주제", "topic")
            self.assertEqual(result, "")
        finally:
            script0._call_claude_loop = old_loop

    def test_invalid_model_error_detects_404_model_not_found(self):
        response = FakeResponse(404, {"error": {"message": "model claude-test not found"}})
        self.assertTrue(script0.is_invalid_model_error(response))

    def test_validate_script_requires_final_answer(self):
        result = complete_result()
        result["final_answer"] = ""
        report = script0.validate_script(result, comparison_strategy())
        self.assertFalse(report["ok"])
        self.assertIn("missing_final_answer", issue_codes(report))

    def test_validate_script_warns_for_fewer_than_two_comparison_targets(self):
        report = script0.validate_script(complete_result(), comparison_strategy(targets=["소주"]))
        self.assertTrue(report["ok"])
        self.assertIn("comparison_requires_two_targets", warning_codes(report))

    def test_validate_script_warns_for_generic_alcohol_answer(self):
        result = complete_result()
        result["final_answer"] = "술 종류에 따라 다릅니다."
        report = script0.validate_script(result, comparison_strategy(evidence_status="limited"))
        self.assertTrue(report["ok"])
        self.assertIn("generic_final_answer", warning_codes(report))

    def test_validate_script_complete_passes(self):
        report = script0.validate_script(complete_result(), comparison_strategy())
        self.assertTrue(report["ok"], report)

    def test_validate_script_warns_for_unsupported_winner_with_insufficient_evidence(self):
        result = complete_result()
        result["final_answer"] = "소주가 더 좋습니다."
        report = script0.validate_script(result, comparison_strategy(evidence_status="insufficient"))
        self.assertTrue(report["ok"])
        self.assertIn("unsupported_winner_claim", warning_codes(report))

    def test_over_target_length_is_error_and_blocks(self):
        old_total = script0.total_chars
        try:
            script0.total_chars = 10
            report = script0.validate_script(complete_result(), comparison_strategy())
            self.assertFalse(report["ok"])
            self.assertIn("over_target_length", issue_codes(report))
            self.assertGreater(report["metrics"]["length_ratio"], 1.40)
        finally:
            script0.total_chars = old_total

    def test_missing_promise_confirmation_is_warning(self):
        result = complete_result()
        result["promise_fulfilled"] = False
        report = script0.validate_script(result, comparison_strategy())
        self.assertTrue(report["ok"])
        self.assertIn("promise_not_fulfilled", warning_codes(report))

    def test_over_length_trim_does_not_delete_scenes(self):
        old_total = script0.total_chars
        old_min = script0.min_scenes_estimate
        try:
            script0.total_chars = 10
            script0.min_scenes_estimate = 3
            scenes = [
                {"text": "가" * 20, "visual_query": "one"},
                {"text": "나" * 20, "visual_query": "two"},
                {"text": "다" * 20, "visual_query": "three"},
            ]
            with contextlib.redirect_stdout(io.StringIO()):
                trimmed = script0.trim_scenes(scenes)
            self.assertEqual([scene["text"] for scene in trimmed], [scene["text"] for scene in scenes])
        finally:
            script0.total_chars = old_total
            script0.min_scenes_estimate = old_min

    def test_validation_failure_prevents_output_files_without_claude_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_work_dir = script0.WORK_DIR
            old_call = script0.call_claude
            try:
                script0.WORK_DIR = tmp
                script0.call_claude = lambda prompt: (_ for _ in ()).throw(AssertionError("Claude must not be called"))
                result = complete_result()
                result["final_answer"] = ""
                with self.assertRaises(RuntimeError):
                    with contextlib.redirect_stdout(io.StringIO()):
                        script0.enforce_quality_without_revision(result, comparison_strategy())
                self.assertFalse(Path(tmp, "script.txt").exists())
                self.assertTrue(Path(tmp, "script_quality.json").exists())
            finally:
                script0.WORK_DIR = old_work_dir
                script0.call_claude = old_call

    def test_quality_report_written_for_valid_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_work_dir = script0.WORK_DIR
            try:
                script0.WORK_DIR = tmp
                with contextlib.redirect_stdout(io.StringIO()):
                    script0.enforce_quality_without_revision(complete_result(), comparison_strategy())
                report_path = Path(tmp, "script_quality.json")
                self.assertTrue(report_path.exists())
                report = json.loads(report_path.read_text(encoding="utf-8"))
                self.assertTrue(report["ok"])
            finally:
                script0.WORK_DIR = old_work_dir


if __name__ == "__main__":
    unittest.main()
