import contextlib
from datetime import datetime, timezone
import io
import json
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
# URI 특수문자 경로 처리 검증용. 윈도우는 파일명에 '?'를 못 쓰므로 '#%'로 대신한다.
URI_CHARS = '#%' if os.name == 'nt' else '?#'


@contextlib.contextmanager
def module_override(name, value):
    """sys.modules 의 키 하나만 바꿨다 되돌린다.
    patch.dict(sys.modules, ...) 는 종료 시 도중에 import 된 모듈을 전부 지워서, 뒤 테스트가
    PyO3 확장(chromadb 의존성)을 다시 import 하다 'may only be initialized once' 로 실패한다."""
    missing = object()
    old = sys.modules.get(name, missing)
    sys.modules[name] = value
    try:
        yield
    finally:
        if old is missing:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = old

try:
    import chromadb
except ImportError:
    raise unittest.SkipTest('chromadb not installed')

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
import sb_config
import sb_lock
import sb_search as searcher


class SearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name).resolve() / ("observations" + URI_CHARS + ".db")
        self.chroma = Path(self.temp.name).resolve() / "missing-chroma"
        self.snapshot = Path(self.temp.name).resolve() / "chroma-snapshot"
        env = patch.dict(os.environ, {
            "SB_CLAUDE_MEM_DB": str(self.db), "SB_CHROMA_PATH": str(self.chroma),
            "SB_KO_INDEX": str(self.db),
            "SB_HOME": str(Path(self.temp.name).resolve() / "sb-home"),
            "SB_CLAUDE_MEM_DIR": str(Path(self.temp.name).resolve() / "claude-mem"),
            "SB_CHROMA_SNAPSHOT": str(self.snapshot), "SB_CHROMA_SNAPSHOT_TTL": "600",
            "HF_HUB_OFFLINE": "1", "PYTHONDONTWRITEBYTECODE": "1",
        })
        env.start()
        self.addCleanup(env.stop)
        ko = module_override("sb_fts_ko", None)
        ko.__enter__()
        self.addCleanup(ko.__exit__, None, None, None)
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.executescript("""
                CREATE TABLE observations (
                    id INTEGER PRIMARY KEY, project TEXT, type TEXT, title TEXT,
                    subtitle TEXT, narrative TEXT, text TEXT, facts TEXT, concepts TEXT,
                    created_at TEXT, created_at_epoch INTEGER, metadata TEXT);
                CREATE VIRTUAL TABLE observations_fts USING fts5(
                    title, subtitle, narrative, text, facts, concepts,
                    content='observations', content_rowid='id');
            """)
            for ident, project, title, epoch, metadata in [
                (1, "alpha", "apple apple", 1_000, {}),
                (2, "beta", "banana", 2_000, {}),
                (3, "alpha", "apple banana", 3_000, {}),
                (4, "alpha", "apple summary", 9_000,
                 {"kind": "consolidation", "source_ids": [1]}),
            ]:
                conn.execute("INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                             (ident, project, "discovery", title, "", title * 30, "",
                              "[]", "[]", datetime.fromtimestamp(epoch / 1000, timezone.utc).isoformat(), epoch,
                              json.dumps(metadata)))
            conn.execute("INSERT INTO observations_fts(rowid,title,subtitle,narrative,text,facts,concepts) "
                         "SELECT id,title,subtitle,narrative,text,facts,concepts FROM observations")
            conn.commit()

    def test_fts_or_scope_unique_and_escaped_tokens(self) -> None:
        before = self.db.read_bytes()
        global_hits = searcher.fts_search("apple banana", searcher.Scope("global", []), 20, str(self.db))
        self.assertEqual({h.obs_id for h in global_hits}, {1, 2, 3, 4})
        self.assertEqual(len(global_hits), 4)
        project_hits = searcher.fts_search("apple banana", searcher.Scope("project", ["alpha"]), 20, str(self.db))
        self.assertEqual({h.obs_id for h in project_hits}, {1, 3, 4})
        multi_hits = searcher.fts_search("apple banana", searcher.Scope("multi", ["alpha", "beta"]), 20, str(self.db))
        self.assertEqual({h.obs_id for h in multi_hits}, {1, 2, 3, 4})
        for query in ['apple" banana', "apple OR (banana", '"', ""]:
            searcher.fts_search(query, searcher.Scope("global", []), 20, str(self.db))
        self.assertEqual(self.db.read_bytes(), before)

    def test_rrf_intersection_and_duplicate_vote(self) -> None:
        scores = searcher.rrf([[1, 2], [2, 3]])
        self.assertEqual(max(scores, key=scores.get), 2)
        self.assertAlmostEqual(scores[2], 1 / 62 + 1 / 61)
        self.assertAlmostEqual(scores[1], 1 / 61)
        self.assertEqual(searcher.rrf([]), {})
        self.assertAlmostEqual(searcher.rrf([[1, 1]])[1], 1 / 61)

    def test_time_decay_monotonic_and_future_clamped(self) -> None:
        now = 10 * 86_400_000
        recent = searcher.time_decay(1.0, now, now, 0.02)
        old = searcher.time_decay(1.0, 0, now, 0.02)
        self.assertLess(old, recent)
        self.assertAlmostEqual(old, math.exp(-0.2))
        self.assertEqual(searcher.time_decay(1.0, 0, now, 0), 1.0)
        self.assertEqual(searcher.time_decay(1.0, now + 1, now, 0.02), 1.0)

    def test_hydrate_metadata_missing_ids(self) -> None:
        rows = searcher.hydrate([4, 1, 999], str(self.db))
        self.assertEqual(set(rows), {1, 4})
        self.assertEqual(rows[4]["metadata"]["source_ids"], [1])
        self.assertEqual(searcher.hydrate([], str(self.db)), {})

    def test_vector_failure_retains_fts_without_creating_path(self) -> None:
        result = searcher.search("apple", searcher.Scope("global", []), db_path=str(self.db), chroma_path=str(self.chroma))
        self.assertFalse(result["meta"]["vector_ok"])
        self.assertTrue(result["meta"]["vector_error"])
        self.assertEqual(result["meta"]["fts_backend"], "fts5")
        self.assertEqual({row["obs_id"] for row in result["results"]}, {1, 3, 4})
        self.assertFalse(self.chroma.exists())
        scores = [row["score"] for row in result["results"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        summary = next(row for row in result["results"] if row["obs_id"] == 4)
        self.assertEqual(summary["kind"], "consolidation")
        self.assertEqual(summary["derived_from"], [1])
        self.assertLessEqual(len(summary["snippet"]), 200)

    def test_vector_lock_retries_three_times_then_reports_failure(self) -> None:
        with patch.object(searcher, "_vector_snapshot", side_effect=sqlite3.OperationalError("database is locked")) as snapshot:
            with patch.object(searcher.time, "sleep") as sleep:
                hits, meta = searcher.vector_search("apple", searcher.Scope("global", []), 5, str(self.chroma))
        self.assertEqual(hits, [])
        self.assertFalse(meta["ok"])
        self.assertIn("locked", meta["error"])
        self.assertEqual(snapshot.call_count, 4)
        self.assertEqual([call.args for call in sleep.call_args_list], [(0.3,)] * 3)

    def test_vector_best_chunk_score_and_query_contract(self) -> None:
        with patch.object(searcher, "_vector_snapshot", side_effect=[sqlite3.OperationalError("database is locked"), contextlib.nullcontext(self.temp.name)]):
            with patch.object(chromadb, "PersistentClient") as factory:
                collection = factory.return_value.get_collection.return_value
                collection.query.return_value = {
                    "metadatas": [[{"sqlite_id": ident, "doc_type": kind} for ident, kind in
                                   [(1, "observation"), (1, "observation"), (2, "observation"), (9, "summary")]]],
                    "distances": [[0.7, 0.1, 0.4, 0.0]],
                }
                with patch.object(searcher.time, "sleep") as sleep:
                    hits, meta = searcher.vector_search("apple", searcher.Scope("multi", ["alpha", "beta"]), 2, str(self.chroma))
                    sleep.assert_called_once_with(0.3)
                collection.query.assert_called_once_with(
                    query_texts=["apple"], n_results=18,
                    where={"project": {"$in": ["alpha", "beta"]}}, include=["metadatas", "distances"])
        self.assertTrue(meta["ok"], meta["error"])
        self.assertEqual([hit.obs_id for hit in hits], [1, 2])
        self.assertAlmostEqual(hits[0].score, 0.9)
        self.assertAlmostEqual(hits[1].score, 0.6)

    def test_vector_search_restores_local_embedding_roles_without_prior_registration(self) -> None:
        import importlib
        sb_embedding = importlib.import_module("sb_embedding")
        from chromadb.api.client import Client
        from chromadb.config import Settings
        from chromadb.utils import embedding_functions

        calls = []

        class Encoder:
            def encode(self, texts, **kwargs):
                calls.append((list(texts), kwargs["prompt"]))
                return [[1.0, 0.0] if kwargs["prompt"] == "<Q>" or text == "apple"
                        else [0.0, 1.0] for text in texts]

        config = {
            "schema": "sb-embedding-1", "model_id": "test/local",
            "revision": "a" * 40, "local_path": self.temp.name,
            "device": "cpu", "dtype": "float32", "query_prompt": "<Q>",
            "document_prompt": "", "max_chars": 6000, "max_tokens": 32,
            "normalize": True, "trust_remote_code": False, "dimension": 2,
        }
        with patch.dict(embedding_functions.known_embedding_functions), \
                patch.object(sb_embedding.LocalEmbeddingFunction, "_load", return_value=Encoder()):
            sb_embedding.register()
            client = Client(settings=Settings(
                is_persistent=True, persist_directory=str(self.chroma),
                anonymized_telemetry=False))
            try:
                collection = client.create_collection(
                    "cm__claude-mem", embedding_function=sb_embedding.make_local_ef(config))
                collection.add(
                    ids=["one", "two"], documents=["apple", "banana"],
                    metadatas=[{"sqlite_id": ident, "project": "alpha",
                                "doc_type": "observation"} for ident in (1, 2)])
            finally:
                client.close()
            embedding_functions.known_embedding_functions.pop("secondbrain_local")
            hits, meta = searcher.vector_search(
                "unseen recall", searcher.Scope("project", ["alpha"]), 1, str(self.chroma))
        self.assertTrue(meta["ok"], meta["error"])
        self.assertEqual([hit.obs_id for hit in hits], [1])
        self.assertIn((["apple", "banana"], ""), calls)
        self.assertIn((["unseen recall"], "<Q>"), calls)

    def test_ko_preferred_and_missing_index_falls_back(self) -> None:
        module = types.ModuleType("sb_fts_ko")
        with patch.object(module, "search_ko", create=True, return_value=[(3, 4.0)]) as ko, \
                patch.object(module, "ensure_current", create=True) as ensure:
            with module_override("sb_fts_ko", module):
                result = searcher.search("apple", searcher.Scope("project", ["alpha"]), db_path=str(self.db))
                self.assertEqual(result["meta"]["fts_backend"], "ko")
                self.assertEqual(result["results"][0]["obs_id"], 3)
                self.assertTrue(ko.called)
                ensure.assert_called_once_with(index_path=str(self.db), db_path=str(self.db))
                ko.side_effect = FileNotFoundError("temporary Korean index missing")
                fallback = searcher.search("apple", searcher.Scope("global", []), db_path=str(self.db))
                self.assertEqual(fallback["meta"]["fts_backend"], "fts5")
                self.assertTrue(fallback["results"])

    def test_fusion_intersection_sources_and_rerank_fallback(self) -> None:
        vectors = [searcher.Hit(3, 0.9, "vector"), searcher.Hit(2, 0.8, "vector")]
        with patch.object(searcher, "vector_search", return_value=(vectors, {"ok": True, "error": None, "chunks": 2})):
            result = searcher.search("apple", searcher.Scope("global", []), db_path=str(self.db))
            self.assertEqual(result["results"][0]["obs_id"], 3)
            self.assertEqual(result["results"][0]["sources"], ["fts", "vector"])
            self.assertEqual(result["results"][0]["rank_vector"], 1)
            with patch.object(searcher, "rerank", return_value=[searcher.Hit(2, 0.99, "rerank")]):
                reranked = searcher.search("apple", searcher.Scope("global", []), use_rerank=True, db_path=str(self.db))
            self.assertEqual(reranked["results"][0]["obs_id"], 2)
            self.assertIn("rerank", reranked["results"][0]["sources"])
            with patch.object(searcher, "rerank", side_effect=OSError("offline model missing")):
                fallback = searcher.search("apple", searcher.Scope("global", []), use_rerank=True, db_path=str(self.db))
            self.assertEqual(fallback["results"], result["results"])
            self.assertFalse(fallback["meta"]["rerank_ok"])
            self.assertIn("offline", fallback["meta"]["rerank_error"])

    def test_nested_provenance_multiple_sources_uses_earliest(self) -> None:
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute("UPDATE observations SET metadata=? WHERE id=4",
                         (json.dumps({"prov": {"kind": "consolidation", "source_ids": [2, 1, 999]}}),))
            conn.commit()
        with patch.object(searcher.time, "time", return_value=10):
            result = searcher.search("apple", searcher.Scope("global", []), decay_rate=1, db_path=str(self.db))
        row = next(row for row in result["results"] if row["obs_id"] == 4)
        self.assertEqual(row["kind"], "consolidation")
        self.assertEqual(row["derived_from"], [2, 1, 999])
        self.assertAlmostEqual(row["score"], searcher.time_decay(1 / (60 + row["rank_fts"]), 1_000, 10_000, 1))

    def test_provenance_decay_uses_oldest_source_without_penalizing_original(self) -> None:
        with patch.object(searcher, "fts_search", return_value=[searcher.Hit(4, 1, "fts"), searcher.Hit(1, 1, "fts")]):
            with patch.object(searcher.time, "time", return_value=10):
                result = searcher.search("apple", searcher.Scope("global", []), decay_rate=1, db_path=str(self.db))
        rows = {row["obs_id"]: row for row in result["results"]}
        self.assertAlmostEqual(rows[4]["score"], searcher.time_decay(1 / 61, 1_000, 10_000, 1))
        self.assertAlmostEqual(rows[1]["score"], searcher.time_decay(1 / 62, 1_000, 10_000, 1))

    def test_cli_no_vector_json_and_table(self) -> None:
        with patch.object(searcher, "vector_search", side_effect=AssertionError("vector disabled")):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(searcher.main(["apple", "--global", "--json", "--no-vector"]), 0)
            self.assertTrue(json.loads(output.getvalue())["results"])
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(searcher.main(["apple", "--project", "alpha", "--no-vector"]), 0)
            self.assertIn("alpha", output.getvalue())
            self.assertIn("apple", output.getvalue())
        script = str(Path(searcher.__file__).resolve())
        help_result = subprocess.run([sys.executable, script, "--help"], capture_output=True, text=True)
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        bad = subprocess.run([sys.executable, script, "apple", "--limit", "invalid"], capture_output=True, text=True)
        self.assertNotEqual(bad.returncode, 0)
        cwd = Path(self.temp.name).resolve() / "alpha"
        cwd.mkdir()
        for flags, projects in [([], ["alpha"]), (["--projects", "alpha,beta"], ["alpha", "beta"])]:
            completed = subprocess.run([sys.executable, script, "apple banana", "--json", "--no-vector", *flags],
                                       cwd=cwd, capture_output=True, text=True,
                                       env=dict(os.environ, SB_KO_INDEX=str(cwd / "missing-ko.db")))
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["scope"]["projects"], projects)
            self.assertEqual({row["project"] for row in payload["results"]}, set(projects))

    def test_real_chroma_observation_aggregation_scope_and_source_preserved(self) -> None:
        from chromadb.config import Settings
        from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
        from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import ONNXMiniLM_L6_V2

        cache = ONNXMiniLM_L6_V2.DOWNLOAD_PATH / ONNXMiniLM_L6_V2.EXTRACTED_FOLDER_NAME
        if not all((cache / name).is_file() for name in (
            "config.json", "model.onnx", "special_tokens_map.json",
            "tokenizer_config.json", "tokenizer.json", "vocab.txt",
        )):
            self.skipTest("DefaultEmbeddingFunction model not cached; network download prohibited")
        client = chromadb.PersistentClient(path=str(self.chroma), settings=Settings(anonymized_telemetry=False))
        self.addCleanup(client.close)
        collection = client.create_collection("cm__claude-mem", embedding_function=DefaultEmbeddingFunction())
        with patch.object(ONNXMiniLM_L6_V2, "_download", side_effect=AssertionError("network download prohibited")):
            collection.add(ids=["one", "one-fact", "two", "three"],
                           documents=["apple memory", "apple fact", "banana memory", "apple other"],
                           metadatas=[{"sqlite_id": ident, "project": project, "doc_type": "observation"}
                                      for ident, project in [(1, "alpha"), (1, "alpha"), (2, "beta"), (3, "alpha")]])
            before = {str(path.relative_to(self.chroma)): path.read_bytes()
                      for path in self.chroma.rglob("*") if path.is_file() and not path.name.endswith("-shm")}
            hits, meta = searcher.vector_search("apple", searcher.Scope("project", ["alpha"]), 5, str(self.chroma))
            self.assertTrue(meta["ok"], meta["error"])
            self.assertEqual({hit.obs_id for hit in hits}, {1, 3})
            self.assertEqual(len(hits), 2)
            self.assertEqual(meta["chunks"], 3)
            self.assertFalse(meta["snapshot"]["reused"])
            self.assertEqual(Path(meta["snapshot"]["path"]), self.snapshot)
            snapshot_meta = self.snapshot / "snapshot_meta.json"
            created = json.loads(snapshot_meta.read_text(encoding='utf-8'))["created_at_epoch"]
            snapshot_mtime = self.snapshot.stat().st_mtime_ns
            with patch.object(searcher, "_clone_tree", side_effect=AssertionError("cache hit must not copy")):
                hits, meta = searcher.vector_search("apple", searcher.Scope("multi", ["alpha", "beta"]), 5, str(self.chroma))
            self.assertTrue(meta["ok"], meta["error"])
            self.assertEqual({hit.obs_id for hit in hits}, {1, 2, 3})
            self.assertTrue(meta["snapshot"]["reused"])
            self.assertGreaterEqual(meta["snapshot"]["age_s"], 0)
            self.assertEqual(json.loads(snapshot_meta.read_text(encoding='utf-8'))["created_at_epoch"], created)
            self.assertEqual(self.snapshot.stat().st_mtime_ns, snapshot_mtime)
            with patch.dict(os.environ, {"SB_CHROMA_SNAPSHOT_TTL": "0"}):
                with patch.object(searcher, "_clone_tree", wraps=searcher._clone_tree) as copy:
                    _, meta = searcher.vector_search("apple", searcher.Scope("global", []), 5, str(self.chroma))
                self.assertTrue(copy.called)
            self.assertTrue(meta["ok"], meta["error"])
            self.assertFalse(meta["snapshot"]["reused"])
            self.assertGreater(json.loads(snapshot_meta.read_text(encoding='utf-8'))["created_at_epoch"], created)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(searcher.main(["apple", "--global", "--json", "--fresh"]), 0)
            fresh = json.loads(output.getvalue())
            self.assertTrue(fresh["meta"]["vector_ok"], fresh["meta"]["vector_error"])
            self.assertFalse(fresh["meta"]["snapshot"]["reused"])
            after = {str(path.relative_to(self.chroma)): path.read_bytes()
                     for path in self.chroma.rglob("*") if path.is_file() and not path.name.endswith("-shm")}
            self.assertEqual(before, after)
            client.close()
            output, errors = io.StringIO(), io.StringIO()
            with patch.object(searcher, "_clone_tree", side_effect=AssertionError("snapshot disabled")):
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                    self.assertEqual(searcher.main(["apple", "--global", "--json", "--no-snapshot"]), 0)
            direct = json.loads(output.getvalue())
            self.assertTrue(direct["meta"]["vector_ok"], direct["meta"]["vector_error"])
            self.assertEqual(Path(direct["meta"]["snapshot"]["path"]), self.chroma)
            self.assertEqual(len(errors.getvalue().strip().splitlines()), 1)

    def test_cli_no_snapshot_opens_source_without_copy(self) -> None:
        self.chroma.mkdir()
        with contextlib.closing(sqlite3.connect(self.chroma / "chroma.sqlite3")) as connection:
            connection.execute("CREATE TABLE segments (id TEXT, scope TEXT)")
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(chromadb, "PersistentClient") as factory:
            factory.return_value.get_collection.return_value.query.return_value = {
                "metadatas": [[{"sqlite_id": 1, "doc_type": "observation"}]],
                "distances": [[0.1]],
            }
            with patch.object(searcher, "_clone_tree", side_effect=AssertionError("snapshot disabled")):
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                    self.assertEqual(searcher.main(["apple", "--global", "--json", "--no-snapshot"]), 0)
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["meta"]["vector_ok"], payload["meta"]["vector_error"])
        self.assertEqual(Path(factory.call_args.kwargs["path"]), self.chroma)
        self.assertEqual(Path(payload["meta"]["snapshot"]["path"]), self.chroma)
        self.assertEqual(len(errors.getvalue().strip().splitlines()), 1)
        self.assertFalse(self.snapshot.exists())

    def test_snapshot_corruption_rebuild_and_failed_publication_preserves_cache(self) -> None:
        self.chroma.mkdir()
        with contextlib.closing(sqlite3.connect(self.chroma / "chroma.sqlite3")) as connection:
            connection.execute("CREATE TABLE segments (id TEXT, scope TEXT)")
        segment = self.chroma / "segment"
        segment.mkdir()
        (segment / "data.bin").write_bytes(b"segment data")
        with searcher._vector_snapshot(self.chroma) as path:
            self.assertEqual(Path(path), self.snapshot)
        metadata = self.snapshot / "snapshot_meta.json"
        metadata.write_text("broken json", encoding='utf-8')
        with patch.object(searcher, "_clone_tree", wraps=searcher._clone_tree) as copy:
            with searcher._vector_snapshot(self.chroma):
                self.assertTrue(copy.called)
        preserved = metadata.read_bytes()
        with (self.snapshot.parent / (self.snapshot.name + ".lock")).open("a") as lock:
            sb_lock.lock(lock, sb_lock.LOCK_EX | sb_lock.LOCK_NB)
            original = searcher._flock
            with patch.object(searcher, "_flock", side_effect=lambda stream, operation: original(stream, operation, timeout=0)):
                with self.assertRaises(TimeoutError):
                    with searcher._vector_snapshot(self.chroma):
                        self.fail("contended refresh must not serve stale cache")
            self.assertEqual(metadata.read_bytes(), preserved)
        replace = searcher.os.replace

        def fail_publication(source: str, destination: str) -> None:
            if Path(destination) == self.snapshot and Path(source).name != self.snapshot.name:
                if Path(source, "snapshot_meta.json").read_bytes() != preserved:
                    raise OSError("publication failed")
            replace(source, destination)

        with patch.dict(os.environ, {"SB_CHROMA_SNAPSHOT_TTL": "0"}):
            with patch.object(searcher.os, "replace", side_effect=fail_publication):
                with self.assertRaisesRegex(OSError, "publication failed"):
                    with searcher._vector_snapshot(self.chroma):
                        self.fail("failed publication must not yield")
        self.assertEqual(metadata.read_bytes(), preserved)
        self.assertEqual((self.snapshot / "segment" / "data.bin").read_bytes(), b"segment data")

    def test_defaults_follow_sb_config_and_env_overrides_win(self) -> None:
        home = Path(os.environ["SB_HOME"])
        cm = Path(os.environ["SB_CLAUDE_MEM_DIR"])
        self.assertEqual(searcher._db_path(None), self.db)
        with patch.dict(os.environ):
            for key in ("SB_CLAUDE_MEM_DB", "SB_CHROMA_SNAPSHOT", "SB_KO_INDEX", "SB_CHROMA_PATH"):
                os.environ.pop(key)
            self.assertEqual(searcher._db_path(None), (cm / "claude-mem.db").resolve())
            self.assertEqual(Path(sb_config.claude_mem_db()).resolve(), (cm / "claude-mem.db").resolve())
            self.assertEqual(searcher._path(None, "SB_CHROMA_SNAPSHOT", sb_config.sb_path("index", "chroma-snapshot")),
                             (home / "index" / "chroma-snapshot").resolve())
            # A missing default Chroma under SB_CLAUDE_MEM_DIR fails softly and is never created.
            hits, meta = searcher.vector_search("apple", searcher.Scope("global", []), 5)
            self.assertEqual(hits, [])
            self.assertIn(str(cm / "chroma"), meta["error"])
            self.assertFalse((cm / "chroma").exists())
        rules = home / "rules"
        rules.mkdir(parents=True)
        (rules / "alpha.md").write_text("rule body", encoding='utf-8')
        loops = home / "loops"
        loops.mkdir()
        (loops / "loops.jsonl").write_text(json.dumps({"id": "L1", "title": "t", "project": "alpha", "status": "open"}) + "\n", encoding='utf-8')
        with patch.dict(os.environ):
            os.environ.pop("SB_RULES_DIR", None)
            os.environ.pop("SB_LOOPS_PATH", None)
            envelope = searcher._rules_items("alpha")
            self.assertEqual(envelope["status"], "ok")
            self.assertEqual(Path(envelope["items"][0]["evidence_ref"]), (rules / "alpha.md").resolve())
            self.assertEqual([item["loop_id"] for item in searcher._next_items("alpha")["items"]], ["L1"])
        other = Path(self.temp.name) / "other-rules"
        other.mkdir()
        with patch.dict(os.environ, {"SB_RULES_DIR": str(other)}):
            self.assertEqual(searcher._rules_items("alpha")["status"], "empty")

    def _built_snapshot(self) -> Path:
        self.chroma.mkdir()
        with contextlib.closing(sqlite3.connect(self.chroma / "chroma.sqlite3")) as connection:
            connection.execute("CREATE TABLE segments (id TEXT, scope TEXT)")
        segment = self.chroma / "segment"
        segment.mkdir()
        (segment / "data.bin").write_bytes(b"segment data")
        with searcher._vector_snapshot(self.chroma):
            pass
        return self.snapshot / "snapshot_meta.json"

    def test_snapshot_manifest_mismatch_rejects_metadata(self) -> None:
        meta_path = self._built_snapshot()
        meta = json.loads(meta_path.read_text(encoding='utf-8'))
        # 키는 OS 와 무관하게 '/' 구분 상대경로다.
        self.assertEqual(set(meta["files"]), {"chroma.sqlite3", "segment/data.bin"})
        self.assertIsNotNone(searcher._snapshot_metadata(self.snapshot, self.chroma))
        data = self.snapshot / "segment" / "data.bin"
        stat = data.stat()
        os.utime(data, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000))
        self.assertIsNone(searcher._snapshot_metadata(self.snapshot, self.chroma))
        os.utime(data, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertIsNotNone(searcher._snapshot_metadata(self.snapshot, self.chroma))
        data.write_bytes(b"segment dat!")  # 같은 크기라도 mtime 이 바뀌면 거부
        os.utime(data, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000))
        self.assertIsNone(searcher._snapshot_metadata(self.snapshot, self.chroma))
        data.unlink()
        self.assertIsNone(searcher._snapshot_metadata(self.snapshot, self.chroma))

    def test_snapshot_manifest_rejects_unsafe_entries(self) -> None:
        meta_path = self._built_snapshot()
        meta = json.loads(meta_path.read_text(encoding='utf-8'))
        for files in ({"segment/data.bin": [1, 1]},                      # chroma.sqlite3 빠짐
                      dict(meta["files"], **{"../outside": [1, 1]}),      # 스냅샷 밖 경로
                      dict(meta["files"], **{"segment/data.bin": [True, 1]}),
                      dict(meta["files"], **{"segment/data.bin": [1]}),
                      []):
            self.assertIsNone(searcher._manifest_entries(self.snapshot, dict(meta, files=files)))

    def test_snapshot_meta_without_files_forces_rebuild(self) -> None:
        # files 키가 없는 예전 메타는 거부되고, 다음 조회가 매니페스트를 담아 다시 만든다.
        meta_path = self._built_snapshot()
        meta = json.loads(meta_path.read_text(encoding='utf-8'))
        del meta["files"]
        meta_path.write_text(json.dumps(meta), encoding='utf-8')
        self.assertIsNone(searcher._snapshot_metadata(self.snapshot, self.chroma))
        with patch.object(searcher, "_clone_tree", wraps=searcher._clone_tree) as copy:
            with searcher._vector_snapshot(self.chroma):
                self.assertTrue(copy.called)
        self.assertIn("files", json.loads(meta_path.read_text(encoding='utf-8')))
        self.assertIsNotNone(searcher._snapshot_metadata(self.snapshot, self.chroma))

    def test_restore_manifest_times_only_for_same_size_files(self) -> None:
        # Chroma 가 조회 중 chroma.sqlite3 를 제자리에서 다시 써도(크기 동일) 재사용 검증이 깨지지 않아야 한다.
        self._built_snapshot()
        database = self.snapshot / "chroma.sqlite3"
        stat = database.stat()
        os.utime(database, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000))
        self.assertIsNone(searcher._snapshot_metadata(self.snapshot, self.chroma))
        searcher._restore_manifest_times(self.snapshot)
        self.assertEqual(database.stat().st_mtime_ns, stat.st_mtime_ns)
        self.assertIsNotNone(searcher._snapshot_metadata(self.snapshot, self.chroma))
        # 크기가 달라진 파일은 되돌리지 않아 다음 검증이 재생성하게 둔다.
        data = self.snapshot / "segment" / "data.bin"
        data.write_bytes(b"segment data grown")
        changed = data.stat().st_mtime_ns
        searcher._restore_manifest_times(self.snapshot)
        self.assertEqual(data.stat().st_mtime_ns, changed)
        self.assertIsNone(searcher._snapshot_metadata(self.snapshot, self.chroma))

    def test_clone_tree_falls_back_to_copytree(self) -> None:
        source = Path(self.temp.name) / "src"
        (source / "sub").mkdir(parents=True)
        (source / "sub" / "f.bin").write_bytes(b"x")
        # darwin 에서 cp -c 가 실패하면(APFS 아님 등) 일반 복사로 내려간다.
        with patch.object(searcher.sys, "platform", "darwin"), \
                patch.object(searcher.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "cp")):
            searcher._clone_tree(source, Path(self.temp.name) / "dst")
        self.assertEqual((Path(self.temp.name) / "dst" / "sub" / "f.bin").read_bytes(), b"x")
        # 다른 OS 는 cp 를 부르지 않고 바로 일반 복사한다.
        with patch.object(searcher.sys, "platform", "win32"), \
                patch.object(searcher.subprocess, "run", side_effect=AssertionError("cp must not run")):
            searcher._clone_tree(source, Path(self.temp.name) / "dst2")
        self.assertEqual((Path(self.temp.name) / "dst2" / "sub" / "f.bin").read_bytes(), b"x")

    def test_title_normalization(self) -> None:
        self.assertEqual(searcher.normalize_title("검색 최신성 기록됨"), searcher.normalize_title("검색최신성, 기록함."))
        self.assertEqual(searcher.normalize_title("Push 완료했다"), searcher.normalize_title("push 완료"))
        self.assertEqual(searcher.normalize_title("배포가 완료됐다!"), "배포가완료")
        self.assertNotEqual(searcher.normalize_title("메일 확인"), searcher.normalize_title("메일 발송"))
        self.assertEqual(searcher.normalize_title("다"), "다")  # 끝말만 남는 제목은 그대로
        self.assertEqual(searcher.normalize_title(None), "")

    def test_fold_duplicates_keeps_newest_in_first_rank_slot(self) -> None:
        items = {
            1: {"title": "스냅샷 검증 기록됨", "created_at": "2026-10-01T00:00:00Z", "title_norm_key": None},
            2: {"title": "다른 주제", "created_at": "2026-10-02T00:00:00Z", "title_norm_key": "k"},
            3: {"title": "스냅샷검증 기록함", "created_at": "2026-10-05T00:00:00Z", "title_norm_key": None},
            4: {"title": "표현만 다른 같은 주제", "created_at": "2026-10-09T00:00:00Z", "title_norm_key": "k"},
            5: {"title": "", "created_at": "2026-10-09T00:00:00Z", "title_norm_key": None},
            6: {"title": None, "created_at": "2026-10-10T00:00:00Z", "title_norm_key": None},
        }
        self.assertEqual(searcher.fold_duplicates([1, 2, 3, 4, 5, 6], items),
                         [(3, 1, 1), (4, 2, 1), (5, 5, 0), (6, 6, 0)])

    def test_search_folds_and_still_fills_limit(self) -> None:
        # 테스트 원장에는 title_norm_key 열이 없다 — 옛 DB 에서도 접기가 제목만으로 돌아야 한다.
        dups = list(range(10, 18))  # 같은 주제를 표현만 바꿔 8번 기록
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            for ident in dups:
                title = ("Apple, apple 기록함" if ident % 2 else "apple apple 기록됨") + " " * (ident % 3)
                conn.execute("INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                             (ident, "alpha", "discovery", title, "", "x", "", "[]", "[]",
                              datetime.fromtimestamp(ident, timezone.utc).isoformat(), ident * 1000, "{}"))
            conn.commit()
        ranking = dups + [1, 3]
        counts = []

        def lexical(query, scope, count, db_path):
            counts.append(count)
            return [searcher.Hit(ident, 1.0 / rank, "fts") for rank, ident in enumerate(ranking[:count], 1)], "fts5"

        with patch.object(searcher, "_lexical", side_effect=lexical):
            result = searcher.search("apple", searcher.Scope("global", []), limit=2, db_path=str(self.db),
                                     chroma_path=str(self.chroma))
        self.assertEqual(counts, [6, 24])  # 접고 나니 1개뿐 → 후보를 4배로 다시 모음
        self.assertEqual([(row["obs_id"], row["folded"]) for row in result["results"]], [(17, 7), (1, 0)])
        scores = [row["score"] for row in result["results"]]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_refetch_stops_after_three_rounds(self) -> None:
        # 후보가 계속 꽉 차서 돌아와도 재수집은 최대 3회(첫 수집 포함)에서 멈춘다.
        counts = []

        def lexical(query, scope, count, db_path):
            counts.append(count)
            return [searcher.Hit(1, 1.0, "fts")] * count, "fts5"

        with patch.object(searcher, "_lexical", side_effect=lexical):
            result = searcher.search("apple", searcher.Scope("global", []), limit=3, db_path=str(self.db),
                                     chroma_path=str(self.chroma))
        self.assertEqual(counts, [9, 36, 144])
        self.assertEqual([row["obs_id"] for row in result["results"]], [1])

    def test_refetch_candidates_capped(self) -> None:
        # 4배씩 늘려도 후보 수는 상한(REFETCH_MAX_CANDIDATES)에서 멈춘다 — 벡터 n_results 가 limit×432 로 커지지 않게.
        counts = []

        def lexical(query, scope, count, db_path):
            counts.append(count)
            return [searcher.Hit(1, 1.0, "fts")] * count, "fts5"

        with patch.object(searcher, "_lexical", side_effect=lexical):
            searcher.search("apple", searcher.Scope("global", []), limit=30, db_path=str(self.db),
                            chroma_path=str(self.chroma))
            counts_big = list(counts)
            counts.clear()
            searcher.search("apple", searcher.Scope("global", []), limit=80, db_path=str(self.db),
                            chroma_path=str(self.chroma))
        self.assertEqual(counts_big, [90, searcher.REFETCH_MAX_CANDIDATES])
        self.assertEqual(counts, [240])  # 첫 수집이 이미 상한보다 크면 재수집하지 않는다

    def test_refetch_skips_failed_vector_and_reranks_once(self) -> None:
        dups = list(range(10, 18))
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            for ident in dups:
                conn.execute("INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                             (ident, "alpha", "discovery", "apple 기록됨" if ident % 2 else "Apple 기록함", "", "x",
                              "", "[]", "[]", datetime.fromtimestamp(ident, timezone.utc).isoformat(), ident * 1000, "{}"))
            conn.commit()
        ranking = dups + [1, 3]
        counts, reranked = [], []

        def lexical(query, scope, count, db_path):
            counts.append(count)
            return [searcher.Hit(ident, 1.0 / rank, "fts") for rank, ident in enumerate(ranking[:count], 1)], "fts5"

        def fake_rerank(query, candidates, top_n):
            reranked.append(([item["obs_id"] for item in candidates], top_n))
            return [searcher.Hit(item["obs_id"], 1.0 / rank, "rerank") for rank, item in enumerate(candidates, 1)]

        down = ([], {"ok": False, "error": "Failed to connect to Ollama", "chunks": 0})
        with patch.object(searcher, "_lexical", side_effect=lexical), \
                patch.object(searcher, "rerank", side_effect=fake_rerank), \
                patch.object(searcher, "vector_search", return_value=down) as vector:
            result = searcher.search("apple", searcher.Scope("global", []), limit=2, use_rerank=True,
                                     db_path=str(self.db), chroma_path=str(self.chroma))
        self.assertEqual(counts, [6, 24])        # 접고 나니 모자라 재수집은 그대로 한다
        self.assertEqual(vector.call_count, 1)   # 실패한 벡터는 재수집 때 다시 부르지 않는다
        self.assertEqual(reranked, [(ranking, 24)])  # 재순위는 최종 후보에 한 번만
        self.assertEqual([(row["obs_id"], row["folded"]) for row in result["results"]], [(17, 7), (1, 0)])
        self.assertTrue(result["meta"]["rerank_ok"])
        self.assertFalse(result["meta"]["vector_ok"])
        # 벡터가 살아 있으면 재수집 때 늘어난 후보 수로 다시 조회한다.
        counts.clear()
        with patch.object(searcher, "_lexical", side_effect=lexical), \
                patch.object(searcher, "vector_search",
                             return_value=([], {"ok": True, "error": None, "chunks": 0})) as vector:
            searcher.search("apple", searcher.Scope("global", []), limit=2, db_path=str(self.db),
                            chroma_path=str(self.chroma))
        self.assertEqual([call.args[2] for call in vector.call_args_list], [6, 24])

    def test_title_norm_key_column_folds_when_present(self) -> None:
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute("ALTER TABLE observations ADD COLUMN title_norm_key TEXT")
            conn.execute("UPDATE observations SET title_norm_key='same' WHERE id IN (1, 3)")
            conn.commit()
        self.assertEqual(searcher.hydrate([1, 2], str(self.db))[1]["title_norm_key"], "same")
        self.assertIsNone(searcher.hydrate([1, 2], str(self.db))[2]["title_norm_key"])
        with patch.object(searcher, "_lexical", return_value=([searcher.Hit(i, 1.0, "fts") for i in (1, 3, 2)], "fts5")):
            result = searcher.search("apple", searcher.Scope("global", []), limit=5, db_path=str(self.db),
                                     chroma_path=str(self.chroma))
        self.assertEqual([(row["obs_id"], row["folded"]) for row in result["results"]], [(3, 1), (2, 0)])

    def test_rerank_blend_matches_recalld_reranker(self) -> None:
        # 상주 회수(sb_recalld.Reranker.order)와 CLI 재순위가 같은 모델·같은 RRF k=10 식으로 같은 순서를 내야 한다.
        import sb_recalld
        self.assertEqual(searcher.RERANK_MODEL, sb_recalld.RERANK_MODEL)

        class FakeModel:
            def __init__(self, scores):
                self.scores = scores

            def predict(self, pairs, batch_size=16):
                return self.scores

        for scores in ([0.1, 0.2, 0.3, 0.9], [0.9, 0.5, 0.1], [0.5, 0.5, 0.9, 0.1, 0.7], [0.0, 0.0]):
            ids = list(range(100, 100 + len(scores)))
            recalld = sb_recalld.Reranker.__new__(sb_recalld.Reranker)
            recalld.model, recalld.error = FakeModel(scores), None
            expected = recalld.order("q", ids, ["t"] * len(ids))
            with patch.object(searcher, "_reranker", return_value=FakeModel(scores)):
                hits = searcher.rerank("q", [{"obs_id": i, "title": "t"} for i in ids], len(ids))
            self.assertEqual([hit.obs_id for hit in hits], expected)
            self.assertEqual([hit.score for hit in hits], sorted((hit.score for hit in hits), reverse=True))
        with patch.object(searcher, "_reranker", return_value=FakeModel([0.1, 0.2, 0.3, 0.9])):
            hits = searcher.rerank("q", [{"obs_id": i} for i in (1, 2, 3, 4)], 2)
        self.assertEqual([hit.obs_id for hit in hits], [1, 4])

    def test_reranker_loads_configured_model_once(self) -> None:
        calls = []

        class FakeCrossEncoder:
            def __init__(self, name, **kwargs):
                calls.append((name, kwargs))

        transformers = types.ModuleType("sentence_transformers")
        transformers.CrossEncoder = FakeCrossEncoder
        torch = types.ModuleType("torch")
        torch.backends = types.SimpleNamespace(mps=types.SimpleNamespace(is_available=lambda: False))
        with module_override("sentence_transformers", transformers), module_override("torch", torch), \
                patch.object(searcher, "_RERANKER", None), patch.object(searcher, "RERANK_MODEL", "custom/reranker"):
            first = searcher._reranker()
            self.assertIs(searcher._reranker(), first)
        self.assertEqual(calls, [("custom/reranker", {"device": "cpu", "max_length": 512, "local_files_only": True})])


if __name__ == "__main__":
    unittest.main()
