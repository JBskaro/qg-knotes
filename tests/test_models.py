# SPDX-License-Identifier: GPL-3.0-only
"""Models for recognition on this computer: catalog, file selection, custom models from Hugging Face, state on disk
and download via curl from a local server: break and resume, corruption, 404, answer 416, archive, mirror, cancel.
Real models are not downloaded."""

import collections
import hashlib
import http.server
import io
import json
import logging
import pathlib
import re
import tarfile
import tempfile
import threading
import unittest
import urllib.parse
from unittest import mock

from knotes import models, providers, transcript

COMMIT = "c" * 40
models.log.addHandler(logging.NullHandler())  # assertLogs checks the warnings, they stay out of the test output


class FileServer(http.server.BaseHTTPRequestHandler):
    """Fake server: files with resume (Range), a break in the middle, slow delivery, 500 failures."""

    files: dict[str, bytes] = {}
    fragments: set[str] = set()  # these paths break in the middle the first time
    slow: set[str] = set()
    faults: collections.Counter = collections.Counter()  # how many times to answer 500
    requests: list[tuple[str, str]] = []  # path and Range header

    def log_message(self, *_: object) -> None:
        pass

    def _answer(self, code: int, body: bytes, **headings: str) -> None:
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        for name, value in headings.items():
            self.send_header(name.replace("_", "-"), value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        span = self.headers.get("Range", "")
        self.requests.append((path, span))
        data = self.files.get(path)
        if data is None:
            return self._answer(404, b"not found")
        if self.faults[path]:
            self.faults[path] -= 1
            return self._answer(500, b"oops")
        start = int(re.match(r"bytes=(\d+)-", span).group(1)) if span else 0
        if start >= len(data) and start:
            return self._answer(416, b"range not satisfiable", Content_Range=f"bytes */{len(data)}")
        self.send_response(206 if start else 200)
        self.send_header("Content-Length", str(len(data) - start))
        if start:
            self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
        self.end_headers()
        return self._hand_over(path, data[start:])

    def _hand_over(self, path: str, data: bytes) -> None:
        try:
            if path in self.fragments:
                self.fragments.discard(path)
                self.wfile.write(data[: len(data) // 2])
                self.close_connection = True
            elif path in self.slow:
                for start in range(0, len(data), 1024):
                    self.wfile.write(data[start : start + 1024])
                    self.wfile.flush()
                    threading.Event().wait(0.02)
            else:
                self.wfile.write(data)
        except ConnectionError:
            pass


class WithServer(unittest.TestCase):
    """A server on 127.0.0.1 for the whole class, a models folder with Cyrillic in the path for each test."""

    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FileServer)
        cls.server.daemon_threads = True
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.address = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        FileServer.files, FileServer.fragments, FileServer.slow = {}, set(), set()
        FileServer.faults, FileServer.requests = collections.Counter(), []
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = pathlib.Path(self._tmp.name) / "QG Knotes" / "модели"
        for module, name, value in ((models, "PAUSES", (0, 0, 0, 0)), (providers, "TICK_S", 0.05)):
            patcher = mock.patch.object(module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def lay_out(self, path: str, data: bytes) -> str:
        FileServer.files[path] = data
        return self.address + path

    def file(self, name: str, data: bytes, *addresses: str) -> models.File:
        return models.File(name, len(data), hashlib.sha256(data).hexdigest(), addresses)


def _model(*files: models.File, archive: models.File | None = None) -> models.Model:
    return models.Model("проба", "Проба", kind="nemo_ctc", files=files, archive=archive)


class TestCatalog(unittest.TestCase):
    def test_catalog_consistent(self):
        model_list = models.catalog()
        ids = [model.id for model in model_list]
        self.assertEqual(len(ids), len(set(ids)))
        service_items = {model.id for model in model_list if model.service_one}
        self.assertEqual({"silero-vad", "pyannote-segmentation-3-0", "wespeaker-voxceleb-resnet34"}, service_items)
        data = models._catalog_data()
        self.assertEqual({*data["речь"], *data["разметка"]}, service_items)
        self.assertEqual([model.id for model in model_list if model.recommended], ["gigaam-v3-ctc-punct-int8"])
        for model in model_list:
            with self.subTest(model=model.id):
                if not model.service_one:
                    self.assertEqual(
                        models.pick_out((f.name for f in model.files), models.repository(model))[0], model.kind
                    )
                for file in [*model.files, *([model.archive] if model.archive else [])]:
                    self.assertTrue(file.size > 0 and re.fullmatch("[0-9a-f]{64}", file.sha256))
                    self.assertTrue(file.addresses and all(a.startswith("https://") for a in file.addresses))
                    self.assertTrue(all("/resolve/" in a for a in file.addresses if a.startswith(models.HF)))

    def test_files_for_engine(self):
        folder = pathlib.Path("модели")
        self.assertEqual(
            models.engine_files(folder, models.find("gigaam-v3-ctc-punct-int8", folder)),
            {
                "model": folder / "gigaam-v3-ctc-punct-int8" / "model.int8.onnx",
                "tokens": folder / "gigaam-v3-ctc-punct-int8" / "tokens.txt",
            },
        )
        self.assertEqual(
            set(models.engine_files(folder, models.find("whisper-large-v3-turbo-int8", folder))),
            {"encoder", "decoder", "tokens"},
        )
        self.assertEqual(set(models.engine_files(folder, models.find("silero-vad", folder))), {"model"})
        with self.assertRaises(models.ModelError):
            models.find("нет-такой", folder)

    def test_languages(self):
        gigaam, whisper = models.catalog()[:2]
        self.assertTrue(models.understands(gigaam, "ru"))
        self.assertFalse(models.understands(gigaam, "en"))
        self.assertTrue(models.understands(gigaam, ""))
        self.assertTrue(models.understands(whisper, "en"))

    def test_broken_recording(self):
        with self.assertRaises(models.ModelError):
            models._model({"id": "../вне", "название": "x"})
        with self.assertRaises(models.ModelError):
            models._model({"id": "x", "название": "x", "файлы": [{"имя": "..\\model.onnx"}]})
        with self.assertRaises(models.ModelError):
            models._model({"id": "x", "название": "x", "файлы": [{"имя": "con.txt"}]})

    def test_size_as_text(self):
        self.assertEqual(models.size_text(0), "0 МБ")
        self.assertEqual(models.size_text(643854), "1 МБ")
        self.assertEqual(models.size_text(230 * 1024**2), "230 МБ")
        self.assertEqual(models.size_text(1288490189), "1,2 ГБ")


class TestFilter(unittest.TestCase):
    def test_files_with_code_not_taken(self):
        names = [
            "model.onnx",
            "tokens.txt",
            "config.yaml",
            "config.json",
            "README.md",
            "pytorch_model.bin",
            "model.pt",
            "model.pth",
            "last.ckpt",
            "data.pkl",
            "model.safetensors",
        ]
        self.assertEqual(
            models.pick_out(names, "nemo-ctc-x"),
            ("nemo_ctc", ["model.onnx", "tokens.txt", "config.yaml", "config.json"]),
        )

    def test_kind_by_name_and_files(self):
        ctc = ["model.int8.onnx", "model.onnx", "tokens.txt"]
        transducer = ["encoder.int8.onnx", "encoder.onnx", "decoder.onnx", "joiner.onnx", "tokens.txt"]
        self.assertEqual(
            models.pick_out(ctc, "sherpa-onnx-nemo-ctc-giga-am-v3"), ("nemo_ctc", ["model.int8.onnx", "tokens.txt"])
        )
        self.assertEqual(
            models.pick_out(transducer, "sherpa-onnx-zipformer-ru"),
            ("transducer", ["encoder.int8.onnx", "decoder.onnx", "joiner.onnx", "tokens.txt"]),
        )
        whisper = ["base-encoder.onnx", "base-decoder.onnx", "base-tokens.txt"]
        for name, files, kind in (
            ("sherpa-onnx-nemo-transducer-giga-am-v3", transducer, "nemo_transducer"),
            ("sherpa-onnx-nemo-parakeet-tdt-0.6b-v3", transducer, "nemo_transducer"),
            ("sherpa-onnx-paraformer-zh", ctc, "paraformer"),
            ("sherpa-onnx-sense-voice-zh-en", ctc, "sense_voice"),
            ("sherpa-onnx-zipformer-ctc-en", ctc, "zipformer_ctc"),
            ("sherpa-onnx-whisper-base", whisper, "whisper"),
            ("sherpa-onnx-telespeech-ctc-zh", ctc, ""),  # CTC of an unknown kind is not guessed
            ("sherpa-onnx-nemo-canary-180m", ["encoder.onnx", "decoder.onnx", "tokens.txt"], ""),
            ("nemo", ["model.onnx"], ""),  # cannot tell without tokens.txt
            ("nemo", ["encoder.bin", "decoder.bin", "joiner.bin", "tokens.txt"], ""),
        ):
            with self.subTest(name=name, kind=kind):
                self.assertEqual(models.pick_out(files, name)[0], kind)

    def test_languages_and_title_by_name(self):
        for name, kind, languages, title in (
            (
                "sherpa-onnx-nemo-transducer-giga-am-v3-russian-2025-12-16",
                "nemo_transducer",
                ("ru",),
                "GigaAM v3 Transducer",
            ),
            ("sherpa-onnx-whisper-large-v3", "whisper", ("*",), "Whisper large v3"),
            ("sherpa-onnx-whisper-tiny.en", "whisper", ("en",), "Whisper tiny"),
            ("sherpa-onnx-whisper-distil-large-v3.5", "whisper", ("en",), "Whisper distil large v3.5"),
            (
                "sherpa-onnx-nemo-fast-conformer-ctc-be-de-en-es-fr-hr-it-pl-ru-uk-20k-int8",
                "nemo_ctc",
                ("be", "de", "en", "es", "fr", "hr", "it", "pl", "ru", "uk"),
                "FastConformer CTC",
            ),
            (
                "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8",
                "nemo_transducer",
                ("*",),  # languages come from the catalog
                "Parakeet TDT 0.6b v3",
            ),
            (
                "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09",
                "sense_voice",
                ("zh", "en", "ja", "ko", "yue"),
                "SenseVoice",
            ),
            ("sherpa-onnx-omnilingual-asr-1600-languages-300M-ctc", "", ("*",), "omnilingual asr 300M CTC"),
            ("My-Model_V2", "nemo_ctc", (), "My Model V2"),
        ):
            with self.subTest(name=name):
                self.assertEqual(models.languages_by_name(name, kind), languages)
                self.assertEqual(models.title_by_name(name), title)

    def test_links(self):
        self.assertEqual(
            models.parse_link(" csukuangfj/sherpa-onnx-whisper-turbo "),
            ("csukuangfj/sherpa-onnx-whisper-turbo", "main", ""),
        )
        self.assertEqual(models.parse_link("https://huggingface.co/o/n"), ("o/n", "main", ""))
        self.assertEqual(models.parse_link("huggingface.co/o/n/tree/v2/int8"), ("o/n", "v2", "int8"))
        self.assertEqual(models.parse_link("https://hf-mirror.com/o/n/blob/abc/sub/model.onnx"), ("o/n", "abc", "sub"))
        self.assertEqual(models.parse_link("https://huggingface.co/o/n/tree/refs%2Fpr%2F1"), ("o/n", "refs/pr/1", ""))
        for bad in (
            "",
            "https://example.com/o/n",
            "https://huggingface.co/datasets/o/n",
            "o",
            "https://huggingface.co/o/n/tree/main/..",
            "o/n; rm -rf",
        ):
            with self.subTest(url=bad), self.assertRaises(models.ModelError):
                models.parse_link(bad)

    def test_api_answer_parsing(self):
        files = [
            {
                "rfilename": "int8/model.int8.onnx",
                "size": 230_000_000,
                "lfs": {"sha256": "a" * 64, "size": 230_000_000, "pointerSize": 134},
            },
            {"rfilename": "int8/model.onnx", "size": 900, "lfs": {"sha256": "b" * 64, "size": 900}},
            {"rfilename": "int8/tokens.txt", "size": 2007},
            {"rfilename": "model.onnx", "size": 5},
            {"rfilename": "int8/pytorch_model.bin", "size": 9, "lfs": {"sha256": "c" * 64, "size": 9}},
            {"rfilename": "int8/модель.json", "size": 5},
            {"rfilename": "int8/wavs/0.txt", "size": 5},
            "мусор",
        ]
        info = {
            "id": "Org/GigaAM.V3",
            "sha": COMMIT,
            "siblings": files,
            "cardData": {"license": "mit", "language": ["ru", "Russian"]},
        }
        model = models.hf_model(info, "int8")
        self.assertEqual((model.id, model.kind, model.title), ("hf-org--gigaam.v3-int8", "nemo_ctc", "GigaAM.V3"))
        self.assertEqual((model.languages, model.license, model.custom), (("ru",), "mit", True))
        self.assertEqual(
            model.files,
            (
                models.File(
                    "model.int8.onnx",
                    230_000_000,
                    "a" * 64,
                    (f"{models.HF}/Org/GigaAM.V3/resolve/{COMMIT}/int8/model.int8.onnx",),
                ),
                models.File("tokens.txt", 2007, "", (f"{models.HF}/Org/GigaAM.V3/resolve/{COMMIT}/int8/tokens.txt",)),
            ),
        )

    def test_not_a_recognition_model(self):
        for info in (
            {"id": "o/nemo", "sha": COMMIT, "siblings": [{"rfilename": "README.md"}, {"rfilename": "model.bin"}]},
            {"id": "o/nemo", "sha": "main", "siblings": []},
            {"id": "../x", "sha": COMMIT},
            {"id": "o/nemo", "sha": COMMIT, "siblings": "model.onnx tokens.txt"},
        ):
            with self.subTest(info=info):
                self.assertIsNone(models.hf_model(info))

    def test_long_name_is_shortened(self):
        id_ = models._id("o" * 90 + "/" + "n" * 90, "")
        self.assertTrue(models.ID_PATTERN.fullmatch(id_) and len(id_) <= 52, id_)
        self.assertNotEqual(id_, models._id("o" * 90 + "/" + "n" * 89, ""))


class TestWriteJson(unittest.TestCase):
    def test_written_whole_or_not_at_all(self):
        """A failure while writing (a crash, a full disk) leaves the previous file and no temporary one."""
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "запись.json"
            transcript.write_json(path, [{"дорожка": "микрофон"}])
            first = path.stat().st_ino
            with self.assertRaises(TypeError):
                transcript.write_json(path, [{"дорожка": "собеседники"}, object()])
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), [{"дорожка": "микрофон"}])
            transcript.write_json(path, {"ответ": "да"}, None)
            self.assertEqual(path.read_text(encoding="utf-8"), '{"ответ": "да"}')
            self.assertNotEqual(path.stat().st_ino, first)  # a new file in place, not the old one rewritten
            self.assertEqual([p.name for p in pathlib.Path(tmp).iterdir()], ["запись.json"])


class TestState(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = pathlib.Path(self._tmp.name) / "модели"
        self.model = _model(models.File("model.onnx", 3), models.File("tokens.txt", 2))
        self.place = self.folder / "проба"

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_partial_downloaded_broken(self):
        self.assertEqual(models.status(self.folder, self.model), models.NONE)
        self.place.mkdir(parents=True)
        (self.place / "model.onnx.part").write_bytes(b"1")
        self.assertEqual(models.status(self.folder, self.model), models.PARTIAL)
        (self.place / "model.onnx.part").rename(self.place / "model.onnx")
        (self.place / "model.onnx").write_bytes(b"123")
        (self.place / "tokens.txt").write_bytes(b"ab")
        models._write_json(
            self.place / models.MARKER,
            {"id": "проба", "файлы": {"model.onnx": {"размер": 3}, "tokens.txt": {"размер": 2}}},
        )
        self.assertEqual(models.status(self.folder, self.model), models.DOWNLOADED)
        self.assertEqual(models.size_on_disk(self.folder, self.model), 5 + (self.place / models.MARKER).stat().st_size)
        (self.place / "tokens.txt").write_bytes(b"a")
        self.assertEqual(models.status(self.folder, self.model), models.BROKEN)
        (self.place / "tokens.txt").unlink()
        self.assertEqual(models.status(self.folder, self.model), models.BROKEN)
        (self.place / models.MARKER).write_text("не json", encoding="utf-8")
        self.assertEqual(models.status(self.folder, self.model), models.BROKEN)

    def test_delete_whole(self):
        self.place.mkdir(parents=True)
        (self.place / "model.onnx.part").write_bytes(b"1")
        (self.place / models.MARKER).write_text("{}", encoding="utf-8")
        models.delete(self.folder, self.model)
        self.assertFalse(self.place.exists())
        self.assertEqual(models.size_on_disk(self.folder), 0)
        models.delete(self.folder, self.model)  # already deleted: not an error

    def test_busy_files_cannot_be_deleted(self):
        self.place.mkdir(parents=True)
        with (
            mock.patch.object(models.shutil, "rmtree", side_effect=PermissionError),
            self.assertRaises(models.ModelError),
        ):
            models.delete(self.folder, self.model)

    def test_free_space_of_missing_folder(self):
        self.assertGreater(models.free(self.folder / "еще" / "нет"), 0)

    def test_needed_service_models(self):
        gigaam = models.find("gigaam-v3-ctc-punct-int8", self.folder)
        self.assertEqual([m_.id for m_ in models.needed_models(gigaam, self.folder)], ["silero-vad", gigaam.id])
        self.assertEqual(
            [m_.id for m_ in models.needed_models(gigaam, self.folder, diarization=True)],
            ["silero-vad", "pyannote-segmentation-3-0", "wespeaker-voxceleb-resnet34", gigaam.id],
        )

    def test_replace_waits_for_antivirus(self):
        source_of, where_to = self.folder / "а.part", self.folder / "а"
        self.folder.mkdir(parents=True)
        source_of.write_bytes(b"1")
        real = transcript.os.replace
        faults = iter([PermissionError, PermissionError])

        def replace(*arguments: object) -> None:
            error = next(faults, None)
            if error:
                raise error
            real(*arguments)

        with (
            mock.patch.object(transcript.os, "replace", replace),
            mock.patch.object(transcript.clock, "sleep") as pause,
        ):
            models._replace(source_of, where_to)
        self.assertTrue(where_to.exists())
        self.assertEqual(pause.call_count, 2)
        with (
            mock.patch.object(transcript.os, "replace", side_effect=PermissionError),
            mock.patch.object(transcript.clock, "sleep"),
            self.assertRaises(models.ModelError),
        ):
            models._replace(where_to, source_of)

    def test_broken_own_list(self):
        """A broken свои.json is set aside as .bad, not overwritten by the next write; entries are checked like a
        Hugging Face answer: device names, kind, files with code."""
        self.folder.mkdir(parents=True)
        (self.folder / models.OWN_FILE).write_text("{", encoding="utf-8")
        with self.assertLogs(models.log, "WARNING"):
            self.assertEqual(models.custom_items(self.folder), [])
        self.assertEqual((self.folder / (models.OWN_FILE + ".bad")).read_text(encoding="utf-8"), "{")
        usable = {"id": "ok", "название": "Ок", "вид": "nemo_ctc", "файлы": [{"имя": "model.onnx", "размер": 1}]}
        recordings = [
            "x",
            {"id": "../x"},
            usable,
            usable | {"id": "con"},
            usable | {"id": "ok."},
            usable | {"id": "без-вида", "вид": "что-то"},
            usable | {"id": "dll", "файлы": [{"имя": "evil.dll"}]},
            usable | {"id": "архив", "архив": {"имя": "a.tar.bz2"}},
        ]
        (self.folder / models.OWN_FILE).write_text(json.dumps({"модели": recordings}), encoding="utf-8")
        with self.assertLogs(models.log, "WARNING"):
            self.assertEqual([model.id for model in models.custom_items(self.folder)], ["ok"])

    def test_own_unreadable(self):
        self.folder.mkdir(parents=True)
        (self.folder / models.OWN_FILE).write_text("{}", encoding="utf-8")
        with (
            mock.patch.object(pathlib.Path, "read_text", side_effect=PermissionError),
            self.assertLogs(models.log, "WARNING"),
            self.assertRaisesRegex(models.ModelError, "Нет доступа"),
        ):
            models.custom_items(self.folder)

    def test_no_access_to_models_folder(self):
        with (
            mock.patch.object(pathlib.Path, "mkdir", side_effect=PermissionError),
            self.assertLogs(models.log, "WARNING"),
            self.assertRaisesRegex(models.ModelError, "Нет доступа к папке"),
        ):
            models.remember(self.folder, _model(models.File("model.onnx", 1)))


class TestDownload(WithServer):
    def test_break_mid_file_and_resume(self):
        data = bytes(range(256)) * 1200
        file = self.file("model.onnx", data, self.lay_out("/model.onnx", data))
        FileServer.fragments.add("/model.onnx")
        progress = []
        models.download(
            self.folder, _model(file), progress=lambda done_count, total: progress.append((done_count, total))
        )
        self.assertEqual((self.folder / "проба" / "model.onnx").read_bytes(), data)
        self.assertEqual(FileServer.requests, [("/model.onnx", ""), ("/model.onnx", f"bytes={len(data) // 2}-")])
        self.assertEqual(progress[-1], (len(data), len(data)))
        self.assertEqual(models.status(self.folder, _model(file)), models.DOWNLOADED)
        label = json.loads((self.folder / "проба" / models.MARKER).read_text(encoding="utf-8"))
        self.assertEqual(label["файлы"]["model.onnx"], {"размер": len(data), "sha256": file.sha256})
        self.assertEqual(sorted(p.name for p in (self.folder / "проба").iterdir()), ["model.onnx", models.MARKER])

    def test_broken_file_and_404_go_to_next_address(self):
        data = b"tokens" * 100
        file = self.file(
            "tokens.txt",
            data,
            self.address + "/нет/tokens.txt",
            self.lay_out("/порча/tokens.txt", b"x" * len(data)),
            self.lay_out("/хорошо/tokens.txt", data),
        )
        models.download(self.folder, _model(file))
        self.assertEqual((self.folder / "проба" / "tokens.txt").read_bytes(), data)
        self.assertEqual(
            [path for path, _ in FileServer.requests], ["/нет/tokens.txt", "/порча/tokens.txt", "/хорошо/tokens.txt"]
        )

    def test_broken_everywhere(self):
        file = self.file("tokens.txt", b"tokens", self.lay_out("/порча/tokens.txt", b"xxxxxx"))
        with self.assertRaises(models.ModelError) as error:
            models.download(self.folder, _model(file))
        self.assertIn("поврежденным", str(error.exception))
        self.assertEqual(list((self.folder / "проба").iterdir()), [])
        self.assertEqual(models.status(self.folder, _model(file)), models.NONE)

    def test_416_on_resumed_file(self):
        data = b"0123456789" * 50
        file = self.file("model.onnx", data, self.lay_out("/model.onnx", data))
        (self.folder / "проба").mkdir(parents=True)
        (self.folder / "проба" / "model.onnx.part").write_bytes(data)
        models.download(self.folder, _model(file))
        self.assertEqual(FileServer.requests, [("/model.onnx", f"bytes={len(data)}-")])
        self.assertEqual((self.folder / "проба" / "model.onnx").read_bytes(), data)  # the 416 body is not appended

    def test_retries_after_server_failure(self):
        file = self.file("tokens.txt", b"abc", self.lay_out("/tokens.txt", b"abc"))
        FileServer.faults["/tokens.txt"] = 2
        models.download(self.folder, _model(file))
        self.assertEqual(len(FileServer.requests), 3)
        FileServer.faults["/tokens.txt"] = 5
        with self.assertRaises(models.ModelError) as error:
            models.download(self.folder, _model(models.File("tokens.txt", 4, "", (self.address + "/tokens.txt",))))
        self.assertIn("временный сбой (код 500)", str(error.exception))
        self.assertEqual(len(FileServer.requests), 8)

    def test_cancel_keeps_part(self):
        data = b"z" * 400_000
        file = self.file("model.onnx", data, self.lay_out("/model.onnx", data))
        FileServer.slow.add("/model.onnx")
        cancel = threading.Event()
        with self.assertRaises(models.DownloadCancelled):
            models.download(
                self.folder, _model(file), progress=lambda done_count, _: done_count and cancel.set(), cancel=cancel
            )
        part = self.folder / "проба" / "model.onnx.part"
        self.assertTrue(0 < part.stat().st_size < len(data))
        self.assertEqual(models.status(self.folder, _model(file)), models.PARTIAL)

    def test_archive_when_files_fail(self):
        contents_ = {"model.onnx": b"onnx" * 300, "tokens.txt": b"a 0\nb 1\n"}
        clipboard = io.BytesIO()
        with tarfile.open(fileobj=clipboard, mode="w:bz2") as archive:
            for name, data in [
                *(("pack/" + i, d) for i, d in contents_.items()),
                ("../../зло.txt", b"!"),
                ("pack/test_wavs/0.wav", b"RIFF"),
            ]:
                member = tarfile.TarInfo(name)
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
        files = [self.file(name, data, self.address + "/нет/" + name) for name, data in contents_.items()]
        model = _model(
            *files,
            archive=self.file(
                "pack.tar.bz2", clipboard.getvalue(), self.lay_out("/pack.tar.bz2", clipboard.getvalue())
            ),
        )
        models.download(self.folder, model)
        place = self.folder / "проба"
        self.assertEqual(sorted(p.name for p in place.iterdir()), sorted(["model.onnx", models.MARKER, "tokens.txt"]))
        self.assertEqual((place / "tokens.txt").read_bytes(), contents_["tokens.txt"])
        self.assertFalse(any(pathlib.Path(self._tmp.name).rglob("зло.txt")))
        self.assertEqual(models.status(self.folder, model), models.DOWNLOADED)

    def test_broken_archive(self):
        file = self.file("model.onnx", b"onnx", self.address + "/нет/model.onnx")
        archive = self.file("pack.tar.bz2", b"not a tar", self.lay_out("/pack.tar.bz2", b"not a tar"))
        with self.assertRaises(models.ModelError) as error:
            models.download(self.folder, _model(file, archive=archive))
        self.assertIn("Архив", str(error.exception))
        self.assertFalse((self.folder / "проба" / "pack.tar.bz2").exists())

    def test_mirror_goes_first(self):
        data = b"weights"
        hf = f"{models.HF}/o/n/resolve/{COMMIT}/model.onnx"
        self.lay_out(f"/o/n/resolve/{COMMIT}/model.onnx", data)
        file = self.file("model.onnx", data, hf, "https://github.com/o/n/releases/download/x/model.onnx")
        self.assertEqual(
            models.addresses(file, "https://hf-mirror.com/")[0],
            f"https://hf-mirror.com/o/n/resolve/{COMMIT}/model.onnx",
        )
        self.assertEqual(models.addresses(file, models.HF), list(file.addresses))
        for bad_value in ("http://hf-mirror.com", "hf-mirror.com", "ftp://x"):
            with self.subTest(mirror=bad_value), self.assertRaises(models.ModelError):
                models.addresses(file, bad_value)
        models.download(self.folder, _model(file), mirror=self.address)
        self.assertEqual(FileServer.requests, [(f"/o/n/resolve/{COMMIT}/model.onnx", "")])

    def test_low_space_before_download(self):
        file = models.File("model.onnx", 300 * 1024**2, "", (self.address + "/model.onnx",))
        place = collections.namedtuple("Место", "total used free")(10**12, 0, 100 * 1024**2)
        with (
            mock.patch.object(models.shutil, "disk_usage", return_value=place),
            mock.patch.object(providers.subprocess, "Popen") as curl,
            self.assertRaises(models.DiskSpaceError) as error,
        ):
            models.download(self.folder, _model(file))
        self.assertEqual(
            str(error.exception), "Для модели нужно 330 МБ, на диске свободно 100 МБ. Освободите место и повторите."
        )
        curl.assert_not_called()

    def test_redownload_of_broken(self):
        data = {"model.onnx": b"onnx" * 50, "tokens.txt": b"a 0\n"}
        model = _model(*(self.file(i, d, self.lay_out("/" + i, d)) for i, d in data.items()))
        models.download(self.folder, model)
        (self.folder / "проба" / "model.onnx").write_bytes(b"onnx")
        self.assertEqual(models.status(self.folder, model), models.BROKEN)
        FileServer.requests.clear()
        models.download(self.folder, model)
        self.assertEqual(FileServer.requests, [("/model.onnx", "")])
        self.assertEqual(models.status(self.folder, model), models.DOWNLOADED)

    def test_manually_placed_files_not_downloaded(self):
        data = b"manual"
        file = self.file("model.onnx", data, self.address + "/model.onnx")
        (self.folder / "проба").mkdir(parents=True)
        (self.folder / "проба" / "model.onnx").write_bytes(data)
        models.download(self.folder, _model(file))
        self.assertEqual(FileServer.requests, [])
        self.assertEqual(models.status(self.folder, _model(file)), models.DOWNLOADED)

    def test_web_page_instead_of_file(self):
        page = b"<html><body>Login to Wi-Fi</body></html>"
        file = models.File("model.onnx", len(page), "e" * 64, (self.lay_out("/model.onnx", page),))
        with self.assertLogs(models.log, "WARNING") as log, self.assertRaises(models.ModelError) as error:
            models.download(self.folder, _model(file))
        self.assertIn("веб-страница", str(error.exception))
        self.assertIn(f"size {len(page)} of {len(page)}", "\n".join(log.output))

    def test_file_without_size_not_downloaded(self):
        with self.assertRaisesRegex(models.ModelError, "не известен размер"):
            models.download(self.folder, _model(models.File("model.onnx", 0, "", (self.address + "/model.onnx",))))
        self.assertEqual(FileServer.requests, [])

    def test_one_thread_downloads_one_model(self):
        data = b"y" * 300_000
        file = self.file("model.onnx", data, self.lay_out("/model.onnx", data))
        FileServer.slow.add("/model.onnx")
        streams = [threading.Thread(target=models.download, args=(self.folder, _model(file))) for _ in range(2)]
        for stream in streams:
            stream.start()
        for stream in streams:
            stream.join(60)
        self.assertEqual(FileServer.requests, [("/model.onnx", "")])  # the second waited and saw it downloaded
        self.assertEqual(models.status(self.folder, _model(file)), models.DOWNLOADED)

    def test_unreachable_host_not_waited_or_retried(self):
        """Connection refused (curl 7): no retries or pauses, straight to the next address; for the second file the host
        is not tried anymore."""
        dead_one = "http://127.0.0.2:9/x"  # a different host than the fake server's
        files = [
            self.file(name, data, dead_one + "/" + name, self.lay_out("/" + name, data))
            for name, data in (("model.onnx", b"onnx"), ("tokens.txt", b"a 0\n"))
        ]
        attempts = []
        real = models._attempt
        with (
            mock.patch.object(
                models, "_attempt", lambda z, address, f: (attempts.append(address), real(z, address, f))[1]
            ),
            mock.patch.object(models, "PAUSES", (60, 60)),
            self.assertLogs(models.log, "WARNING"),
        ):
            models.download(self.folder, _model(*files))
        self.assertEqual([a for a in attempts if a.startswith(dead_one)], [dead_one + "/model.onnx"])
        self.assertEqual(models.status(self.folder, _model(*files)), models.DOWNLOADED)

    def test_old_version_extra_files_are_removed(self):
        data = b"onnx" * 10
        file = self.file("model.onnx", data, self.lay_out("/model.onnx", data))
        (self.folder / "проба").mkdir(parents=True)
        (self.folder / "проба" / "old-encoder.onnx").write_bytes(b"old")
        models.download(self.folder, _model(file))
        self.assertEqual(sorted(p_.name for p_ in (self.folder / "проба").iterdir()), ["model.onnx", models.MARKER])

    def test_download_all_with_shared_progress_and_quick_resume(self):
        a, b = b"a" * 1000, b"b" * 3000
        model_list = [
            models.Model(id_, id_, files=(self.file("model.onnx", data, self.lay_out(f"/{id_}/model.onnx", data)),))
            for id_, data in (("один", a), ("два", b))
        ]
        progress = []
        models.download_all(self.folder, model_list, lambda done_count, total: progress.append((done_count, total)))
        self.assertEqual(progress[-1], (4000, 4000))
        self.assertTrue(all(total == 4000 for _, total in progress))
        fault = models.Model("сбой", "сбой", files=(self.file("model.onnx", a, self.address + "/сбой/model.onnx"),))
        FileServer.faults["/сбой/model.onnx"] = 1
        with (
            self.assertLogs(models.log, "WARNING"),
            self.assertRaises(models.ModelError),
            mock.patch.object(models.time, "sleep") as pause,
        ):
            models.fetch_missing(self.folder, [fault])  # one try, no pauses
        pause.assert_not_called()

    def test_cancel_between_models(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(models.DownloadCancelled):
            models.download_all(
                self.folder, [_model(models.File("model.onnx", 1, "", (self.address + "/x",)))], cancel=cancel
            )
        self.assertEqual(FileServer.requests, [])

    def test_archive_held_by_antivirus_stays(self):
        data = b"onnx" * 50
        archive = self.file("pack.tar.bz2", b"tar", self.lay_out("/pack.tar.bz2", b"tar"))
        model = _model(self.file("model.onnx", data, self.address + "/нет/model.onnx"), archive=archive)
        with (
            mock.patch.object(models.tarfile, "open", side_effect=PermissionError("занят")),
            self.assertLogs(models.log, "WARNING"),
            self.assertRaisesRegex(models.ModelError, "антивирусом"),
        ):
            models.download(self.folder, model)
        self.assertTrue((self.folder / "проба" / "pack.tar.bz2").exists())  # unpacks on the next try

    def test_mirror_address_is_normalized(self):
        self.assertEqual(
            models.mirror_address(" https://user:pass@HF-mirror.com:8443/hub/?a=1#b "), "https://hf-mirror.com:8443/hub"
        )
        self.assertEqual(models.mirror_address("https://huggingface.co/"), "")
        self.assertEqual(models.mirror_address(""), "")
        for bad_value in ("https://", "https://x:порт", "hf-mirror.com", "http://hf-mirror.com", "ftp://x"):
            with self.subTest(mirror=bad_value), self.assertRaises(models.ModelError):
                models.mirror_address(bad_value)


class TestOwnModels(WithServer):
    """Hugging Face is the fake server: the HF address in the program is replaced with it."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(models, "HF", self.address)
        patcher.start()
        self.addCleanup(patcher.stop)

    def api(self, path: str, answer: object) -> None:
        self.lay_out(path, json.dumps(answer).encode())

    def _own(self, data: dict[str, bytes]) -> None:
        self.api(
            "/api/models/o/giga-am/revision/main",
            {
                "id": "o/giga-am",
                "sha": COMMIT,
                "cardData": {"license": "apache-2.0"},
                "siblings": [
                    *(
                        {
                            "rfilename": name,
                            "size": len(d),
                            "lfs": {"sha256": hashlib.sha256(d).hexdigest(), "size": len(d)},
                        }
                        for name, d in data.items()
                    ),
                    {"rfilename": "model.pt", "size": 1, "lfs": {"sha256": "d" * 64, "size": 1}},
                ],
            },
        )
        for name, contents_ in data.items():
            self.lay_out(f"/o/giga-am/resolve/{COMMIT}/{name}", contents_)

    def test_read_by_link_remember_and_download(self):
        self._own({"model.int8.onnx": b"onnx" * 10, "tokens.txt": b"a 0\n"})
        model = models.read("https://huggingface.co/o/giga-am")
        self.assertEqual([f.name for f in model.files], ["model.int8.onnx", "tokens.txt"])
        self.assertEqual(FileServer.requests, [("/api/models/o/giga-am/revision/main", "")])  # one request
        models.remember(self.folder, model)
        models.remember(self.folder, models.read("o/giga-am"))  # once more: not a duplicate
        self.assertEqual(models.custom_items(self.folder), [model])
        self.assertEqual(models.find("hf-o--giga-am", self.folder), model)
        models.download(self.folder, model)
        self.assertEqual(models.status(self.folder, model), models.DOWNLOADED)
        self.assertNotIn("model.pt", [p_.name for p_ in (self.folder / model.id).iterdir()])
        models.remove_custom(self.folder, model.id)
        self.assertEqual(models.custom_items(self.folder), [])
        self.assertFalse((self.folder / model.id).exists())

    def test_info_from_huggingface_mirror_as_fallback(self):
        """The mirror serves the files too: their details are first taken from huggingface.co; if it does not answer,
        from the mirror, and this shows in the log."""
        self._own({"model.int8.onnx": b"onnx", "tokens.txt": b"a 0\n"})
        with mock.patch.object(models, "_api", wraps=models._api):
            models.read("o/giga-am", mirror="http://127.0.0.1:9")
        self.assertEqual(len(FileServer.requests), 1)  # huggingface.co answered: the mirror was not asked
        FileServer.requests.clear()
        with mock.patch.object(models, "HF", "http://127.0.0.1:9"), self.assertLogs(models.log, "WARNING") as log:
            model = models.read("o/giga-am", mirror=self.address)
        self.assertEqual(model.id, "hf-o--giga-am")
        self.assertIn("from mirror", "\n".join(log.output))

    def test_list_from_site(self):
        """sherpa-onnx models from the site: only by the sherpa-onnx author, without catalog, custom, hidden and unclear
        models; a broken entry is skipped; of a compressed and a full version, the compressed one; the date tells
        identical titles apart."""

        def info(name: str, *files: str, author: str = models.HF_AUTHOR) -> dict:
            return {
                "id": f"{author}/sherpa-onnx-{name}",
                "sha": COMMIT,
                "siblings": [{"rfilename": f} for f in (".gitattributes", *files)],
            }

        ctc = ("model.int8.onnx", "tokens.txt")
        transducer = ("encoder.onnx", "decoder.onnx", "joiner.onnx", "tokens.txt")
        whisper = info("whisper-tiny.en", "tiny.en-encoder.onnx", "tiny.en-decoder.onnx", "tiny.en-tokens.txt")
        broken = info("zipformer-vi", *transducer)
        broken["siblings"][1] |= {"lfs": {"size": "много"}}
        self.api(
            "/api/models",
            [
                info("nemo-ctc-punct-giga-am-v3-russian-2025-12-16", *ctc),  # in the catalog
                info("zipformer-ru-2025-04-20", *transducer),
                info("zipformer-ru-int8-2025-04-20", *transducer),
                info("zipformer-en-2023-03-30", *transducer),
                info("zipformer-en-2023-06-26", *transducer),
                info("streaming-zipformer-ru-2025-01-01", *transducer),
                info("nemo-ctc-giga-am-russian-2024-10-24", *ctc),  # non-commercial
                info("nemo-fast-conformer-ctc-en-24500-int8"),  # the model is only in an archive
                info("zipformer-de", *transducer, author="чужой"),
                broken,
                info("vits-piper-ru", "model.onnx", "tokens.txt"),
                whisper,
                "мусор",
            ],
        )
        models.remember(self.folder, models.hf_model(whisper))  # already among the custom ones
        with self.assertLogs(models.log, "WARNING"):
            model_list = models.from_site(self.folder)
        self.assertEqual(
            [(model.title, model.languages, model.kind) for model in model_list],
            [
                ("Zipformer", ("ru",), "transducer"),
                ("Zipformer 2023-03-30", ("en",), "transducer"),
                ("Zipformer 2023-06-26", ("en",), "transducer"),
            ],
        )
        self.assertIn("int8", model_list[0].url)
        self.assertIsNone(models.size(model_list[0]))  # sizes come with a separate request

    def test_list_from_site_mirror_and_error_text(self):
        """A huggingface.co refusal for the list leads to the mirror; if it fails everywhere, the text has only the
        reason."""
        with self.assertLogs(models.log, "WARNING"), self.assertRaises(models.ModelError) as error:
            models.from_site(self.folder)
        self.assertNotIn("Модель", str(error.exception))
        self.assertIn("нет такого файла", str(error.exception))
        with mock.patch.object(models, "HF", "http://127.0.0.1:9"), self.assertLogs(models.log, "WARNING"):
            self.api("/api/models", [])
            self.assertEqual(models.from_site(self.folder, mirror=self.address), [])

    def test_closed_and_missing(self):
        with (
            self.assertLogs(models.log, "WARNING"),
            self.assertRaises(models.ModelError) as error,
        ):  # HF answers 401 without sign-in
            models.read("o/missing")
        self.assertIn("не найдена на Hugging Face или закрыта", str(error.exception))
        self.api("/api/models/o/gated/revision/main", {"sha": COMMIT, "gated": "manual"})
        with self.assertRaises(models.ModelError) as error:
            models.read("o/gated")
        self.assertIn("закрыта", str(error.exception))

    def test_unclear_answer(self):
        with (
            mock.patch.object(models.providers, "fetch", return_value=(200, "<html>вход в сеть</html>")),
            self.assertLogs(models.log, "WARNING") as log,
            self.assertRaises(models.ModelError) as error,
        ):
            models.read("o/n")
        self.assertIn("сеть требует входа", str(error.exception))
        self.assertIn("<html>вход в сеть", "\n".join(log.output))  # the start of the answer goes to the log
        no_connection = models.providers.link(6, "", "https://huggingface.co/api")
        with (
            mock.patch.object(models.providers, "fetch", side_effect=no_connection),
            self.assertLogs(models.log, "WARNING"),
            self.assertRaises(models.ModelError) as error,
        ):
            models.read("o/n")
        self.assertIn("huggingface.co", str(error.exception))


class TestCommand(unittest.TestCase):
    def test_curl_command_https_only_except_local_network(self):
        z = models._Download(pathlib.Path(tempfile.mkdtemp()))
        with mock.patch.object(providers.subprocess, "Popen") as startup, self.assertRaises(models.ModelError):
            models._attempt(z, "http://example.com/x", models.File("x", 1))
        startup.assert_not_called()
        process_ = mock.MagicMock(returncode=0)
        process_.communicate.return_value = (b"200 000", b"")
        startup_ = mock.MagicMock()
        startup_.return_value.__enter__.return_value = process_
        with mock.patch.object(providers.subprocess, "Popen", startup_):
            self.assertEqual(
                models._attempt(z, "https://huggingface.co/x", models.File("x", 5)), models._Outcome(0, 200, 0, "")
            )
        command = startup_.call_args.args[0]
        self.assertEqual(command[command.index("--proto") + 1], "=https")
        self.assertEqual(command[command.index("--max-filesize") + 1], str(5 + 2**20))
        self.assertEqual(startup_.call_args.kwargs["creationflags"], providers.subprocess.CREATE_NO_WINDOW)

    def test_failure_reasons(self):
        address, place = "https://huggingface.co/x", pathlib.Path(tempfile.mkdtemp())
        for result, words in (
            (models._Outcome(6), "Нет интернета"),
            (models._Outcome(28), "слишком медленно"),
            (models._Outcome(18), "Нажмите «Скачать» еще раз"),
            (models._Outcome(1), "требует входа"),
            (models._Outcome(63), "больше ожидаемого"),
            (models._Outcome(35), "антивирусом"),
            (models._Outcome(60), "зеркало"),
            (models._Outcome(22, 401), "закрыта"),
            (models._Outcome(22, 404), "нет такого файла"),
            (models._Outcome(22, 429), "просит подождать"),
            (models._Outcome(22, 503), "временный сбой (код 503)"),
            (models._Outcome(56, 0, 407), "код 407"),
            (models._Outcome(99), "ошибка curl 99"),
        ):
            with self.subTest(result=result):
                self.assertIn(words, models._reason(result, address, place))
        place_ = collections.namedtuple("Место", "total used free")
        with (
            mock.patch.object(models.shutil, "disk_usage", return_value=place_(10**12, 0, 10)),
            self.assertRaisesRegex(models.DiskSpaceError, "закончилось место"),
        ):
            models._reason(models._Outcome(23), address, place)
        with (
            mock.patch.object(models.shutil, "disk_usage", return_value=place_(10**12, 0, 10**11)),
            self.assertRaisesRegex(models.DiskSpaceError, "Нет доступа к папке"),
        ):
            models._reason(models._Outcome(23), address, place)


if __name__ == "__main__":
    unittest.main()
