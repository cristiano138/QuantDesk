"""编译真实启动器源码，在隔离目录与回环 HTTP 源验证更新校验，不改注册表。"""

import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


HARNESS = r'''
using System;
using System.Reflection;
using System.Windows.Forms;
class UpdateHarness {
    static int Main(string[] args) {
        Type t = typeof(QuantDesk.Watch.Program);
        BindingFlags flags = BindingFlags.Static | BindingFlags.NonPublic;
        t.GetField("Silent", flags).SetValue(null, true);
        t.GetField("ReportPath", flags).SetValue(null, args[2]);
        t.GetMethod("CheckUpdate", flags).Invoke(null, new object[] {args[0], false, args[1]});
        return 0;
    }
}
'''


@pytest.fixture(scope="module")
def launcher_harness(tmp_path_factory):
    csc = Path(os.environ.get("WINDIR", "C:/Windows")) / (
        "Microsoft.NET/Framework64/v4.0.30319/csc.exe")
    if os.name != "nt" or not csc.is_file():
        pytest.skip("需要 Windows 自带的 .NET Framework C# 编译器")
    root = tmp_path_factory.mktemp("update_harness")
    harness = root / "Harness.cs"
    harness.write_text(HARNESS, encoding="utf-8")
    source = Path(__file__).resolve().parents[1] / "QuantDesk-App/pc/Launcher.cs"
    exe = root / "Harness.exe"
    refs = ["System.dll", "System.Core.dll", "System.Windows.Forms.dll", "System.Drawing.dll",
            "System.IO.Compression.dll", "System.IO.Compression.FileSystem.dll"]
    cmd = [str(csc), "/nologo", "/target:exe", "/main:UpdateHarness", "/codepage:65001",
           "/out:" + str(exe), *["/r:" + name for name in refs], str(source), str(harness)]
    result = subprocess.run(cmd, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout.decode("utf-8", errors="replace")
    (root / "quantdesk").mkdir()
    (root / "quantdesk/__init__.py").write_text('__version__ = "1.0"\n', encoding="utf-8")
    (root / "frontend/dist").mkdir(parents=True)
    (root / "frontend/dist/index.html").write_text("OLD PAGE", encoding="utf-8")
    return exe


@pytest.mark.parametrize("sha_kind,version,expect_download,expect_ready", [
    ("missing", "2.0", False, False),
    ("short", "2.0", False, False),
    ("nonhex", "2.0", False, False),
    ("valid", "../../outside", False, False),
    ("valid", "C:\\outside", False, False),
    ("wrong", "2.0", True, False),
    ("valid", "2.0", True, True),
])
def test_real_launcher_rejects_invalid_manifest_before_downloading(
        launcher_harness, tmp_path, sha_kind, version, expect_download, expect_ready):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("QuantDesk-App/QuantDesk.exe", b"SANDBOX LAUNCHER")
    payload = archive.getvalue()
    got = hashlib.sha256(payload).hexdigest()
    sha = {"missing": None, "short": "abc", "nonhex": "z" * 64,
           "wrong": "0" * 64, "valid": got}[sha_kind]
    requests = []
    manifest = {"version": version, "pc": {"size": len(payload)}}
    if sha is not None:
        manifest["pc"]["sha256"] = sha

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            data = json.dumps(manifest).encode() if self.path == "/update.json" else payload
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    address = "http://127.0.0.1:" + str(server.server_port)
    manifest["pc"]["url"] = address + "/package.zip"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    report = tmp_path / "report.txt"
    env = {**os.environ, "TEMP": str(tmp_path), "TMP": str(tmp_path)}
    try:
        result = subprocess.run([str(launcher_harness), str(launcher_harness.parent),
                                 address + "/update.json", str(report)],
                                env=env, capture_output=True, timeout=20)
        assert result.returncode == 0, result.stderr.decode(errors="replace")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert ("/package.zip" in requests) is expect_download
    assert (tmp_path / "QuantDeskUpdate/pending.txt").exists() is expect_ready
    assert (launcher_harness.parent / "frontend/dist/index.html").read_text() == "OLD PAGE"
    if not expect_ready:
        assert report.is_file() and report.stat().st_size > 0
