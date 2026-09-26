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
        self.db = Path(self.temp.name).resolve() / "observations?#.db"
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
        ko = patch.dict(sys.modules, {"sb_fts_ko": None})
        ko.start()
        self.addCleanup(ko.stop)
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
                    query_texts=["apple"], n_results=16,
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
            with patch.dict(sys.modules, {"sb_fts_ko": module}):
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
            created = json.loads(snapshot_meta.read_text())["created_at_epoch"]
            snapshot_mtime = self.snapshot.stat().st_mtime_ns
            with patch.object(searcher.shutil, "copytree", side_effect=AssertionError("cache hit must not copy")):
                hits, meta = searcher.vector_search("apple", searcher.Scope("multi", ["alpha", "beta"]), 5, str(self.chroma))
            self.assertTrue(meta["ok"], meta["error"])
            self.assertEqual({hit.obs_id for hit in hits}, {1, 2, 3})
            self.assertTrue(meta["snapshot"]["reused"])
            self.assertGreaterEqual(meta["snapshot"]["age_s"], 0)
            self.assertEqual(json.loads(snapshot_meta.read_text())["created_at_epoch"], created)
            self.assertEqual(self.snapshot.stat().st_mtime_ns, snapshot_mtime)
            with patch.dict(os.environ, {"SB_CHROMA_SNAPSHOT_TTL": "0"}):
                with patch.object(searcher.shutil, "copytree", wraps=searcher.shutil.copytree) as copy:
                    _, meta = searcher.vector_search("apple", searcher.Scope("global", []), 5, str(self.chroma))
                self.assertTrue(copy.called)
            self.assertTrue(meta["ok"], meta["error"])
            self.assertFalse(meta["snapshot"]["reused"])
            self.assertGreater(json.loads(snapshot_meta.read_text())["created_at_epoch"], created)
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
            with patch.object(searcher.shutil, "copytree", side_effect=AssertionError("snapshot disabled")):
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
            with patch.object(searcher.shutil, "copytree", side_effect=AssertionError("snapshot disabled")):
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
        metadata.write_text("broken json")
        with patch.object(searcher.shutil, "copytree", wraps=searcher.shutil.copytree) as copy:
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
        (rules / "alpha.md").write_text("rule body")
        loops = home / "loops"
        loops.mkdir()
        (loops / "loops.jsonl").write_text(json.dumps({"id": "L1", "title": "t", "project": "alpha", "status": "open"}) + "\n")
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


if __name__ == "__main__":
    unittest.main()
