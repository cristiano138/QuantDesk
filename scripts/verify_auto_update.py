"""自动更新端到端验收（本机 127.0.0.1 起一个更新源，不出网）。

跑法：先 `python QuantDesk-App/pc/build.py` 出成品 exe，再
      `python scripts/verify_auto_update.py`（中文路径那条：`QD_UPD_DIR=量化更新沙箱 python ...`）

三段都要绿才算「一发布就能更新」：
  1 检查一次更新      → 清单取到、SHA-256 对得上、包解压备好，现有文件不动
  2 模拟下次打开软件  → 启动画面之前自己换上；用户自己的文件一个都不删；标记清掉不再重复换
  3 包被人换过        → 指纹对不上就删包，页面与文件分毫未动，并把期望/实际值报出来

沙箱刻意复刻真实装机布局（软件目录 / QuantDesk-App / QuantDesk.exe），布局不对就是第 3 类缺陷。
结束后自动删掉它写进 HKCU 的测试配置，并只清理由沙箱里那个 exe 拉起来的进程。
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

sys.stdout.reconfigure(encoding="utf-8")
TEMP = os.environ["TEMP"]
SANDBOX = os.path.join(TEMP, os.environ.get("QD_UPD_DIR", "qd_upd"))   # 中文名一跑就验到带中文的路径
FEED_DIR = os.path.join(SANDBOX, "feed")
ROOT = os.path.join(SANDBOX, "installed")          # 假装的「软件目录」
SRC_EXE = r"F:\量化交易\QuantDesk-App\QuantDesk.exe"
PORT = 8799

EXE_REL = os.path.join("QuantDesk-App", "QuantDesk.exe")     # 与 Launcher.cs 的 ExeRelPath 一致
EXE = os.path.join(ROOT, EXE_REL)

for d in (SANDBOX, os.path.join(TEMP, "QuantDeskUpdate")):
    shutil.rmtree(d, ignore_errors=True)
os.makedirs(FEED_DIR)
os.makedirs(os.path.join(ROOT, "quantdesk"))
os.makedirs(os.path.join(ROOT, "frontend", "dist"))
os.makedirs(os.path.join(ROOT, "QuantDesk-App"))

# 已装的旧版：版本 1.0，页面里有一句旧标记
open(os.path.join(ROOT, "quantdesk", "__init__.py"), "w", encoding="utf-8").write(
    '__version__ = "1.0"\n')
open(os.path.join(ROOT, "frontend", "dist", "index.html"), "w", encoding="utf-8").write(
    "<html>OLD-PAGE</html>")
shutil.copy(SRC_EXE, EXE)
# 用户自己放的东西：更新不许动它（只叠加、不镜像删除）
os.makedirs(os.path.join(ROOT, "strategies"), exist_ok=True)
open(os.path.join(ROOT, "strategies", "我的策略.py"), "w", encoding="utf-8").write("# 我自己的\n")

# 发布方做的更新包：新版本 2.0，带一个一眼能认出来的标记文件；目录树与装机一致
pkg_dir = os.path.join(SANDBOX, "pkg")
os.makedirs(os.path.join(pkg_dir, "QuantDesk-App"), exist_ok=True)
os.makedirs(os.path.join(pkg_dir, "frontend", "dist"), exist_ok=True)
shutil.copy(SRC_EXE, os.path.join(pkg_dir, EXE_REL))
open(os.path.join(pkg_dir, "NEW-PAGE.flag"), "w", encoding="utf-8").write("更新包到位")
open(os.path.join(pkg_dir, "frontend", "dist", "index.html"), "w", encoding="utf-8").write(
    "<html>NEW-PAGE</html>")
zip_path = os.path.join(FEED_DIR, "QuantDesk-PC.zip")
with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
    for base, _dirs, names in os.walk(pkg_dir):
        for name in names:
            full = os.path.join(base, name)
            z.write(full, os.path.relpath(full, pkg_dir).replace("\\", "/"))
blob = open(zip_path, "rb").read()
manifest = {
    "version": "2.0", "version_code": 2, "notes": "测试更新说明",
    "pc": {"name": "QuantDesk-PC.zip", "url": f"http://127.0.0.1:{PORT}/QuantDesk-PC.zip",
           "sha256": hashlib.sha256(blob).hexdigest(), "size": len(blob), "files": 3},
    "android": {"name": "x.apk", "url": f"http://127.0.0.1:{PORT}/none.apk",
                "sha256": "0" * 64, "size": 1, "version_code": 2},
}
open(os.path.join(FEED_DIR, "update.json"), "w", encoding="utf-8").write(
    json.dumps(manifest, ensure_ascii=False))


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *a):        # 不打请求日志，免得淹掉验证输出
        pass


srv = ThreadingHTTPServer(("127.0.0.1", PORT), lambda *a: Quiet(*a, directory=FEED_DIR))
threading.Thread(target=srv.serve_forever, daemon=True).start()
print("本地更新源已起：http://127.0.0.1:%d/update.json" % PORT)

# 更新源指到本机（应用自己的配置键），测完删掉
reg = __import__("subprocess").run
reg(["reg", "add", r"HKCU\Software\QuantDesk", "/v", "update_feed", "/t", "REG_SZ",
     "/d", f"http://127.0.0.1:{PORT}/update.json", "/f"], check=True, capture_output=True)

upd = os.path.join(TEMP, "QuantDeskUpdate")
report = os.path.join(SANDBOX, "report.txt")

print("\n— 第 1 步：检查一次更新（下载 + SHA-256 校验 + 解压备好）—")
t0 = time.time()
p = subprocess.run([EXE, "/check-update", "/silent", "/report=" + report],
                   capture_output=True, timeout=180)
print("exit=%s 用时 %.1fs" % (p.returncode, time.time() - t0))
print("报告：", open(report, encoding="utf-8").read().strip().replace("\n", " | ")
      if os.path.exists(report) else "(没写报告——手动检查在 silent 下成功路径不弹窗也不落话，正常)")
pending = os.path.join(upd, "pending.txt")
files = os.path.join(upd, "pending", "2.0", "files")
print("标记文件：", open(pending, encoding="utf-8").read().strip().replace("\n", " | ")
      if os.path.exists(pending) else "(没有)")
print("备好的文件：", sorted(os.listdir(files)) if os.path.isdir(files) else "(没解出来)")
print("旧页面还在：", open(os.path.join(ROOT, "frontend", "dist", "index.html"),
                           encoding="utf-8").read())

print("\n— 第 2 步：模拟「下次打开软件」——应在启动画面之前自动换上 —")
proc = subprocess.Popen([EXE, "/silent", "/report=" + os.path.join(SANDBOX, "run.txt")],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
deadline = time.time() + 150
swapped = False
while time.time() < deadline:
    if os.path.exists(os.path.join(ROOT, "NEW-PAGE.flag")):
        swapped = True
        break
    time.sleep(2)
proc.wait(timeout=30)
log = os.path.join(upd, "update.log")
print("已换上：", swapped, "｜ 主进程 exit=", proc.returncode)
print("页面内容：", open(os.path.join(ROOT, "frontend", "dist", "index.html"),
                        encoding="utf-8").read())
print("用户自己的文件还在：", os.path.exists(os.path.join(ROOT, "strategies", "我的策略.py")))
print("标记已清（下次不再重复换）：", not os.path.exists(pending))
print("本体版本号：", "__version__" in
      open(os.path.join(ROOT, "quantdesk", "__init__.py"), encoding="utf-8").read(),
      "（包里没带 quantdesk，所以仍是本机那份——只叠加不删）")
print("\n换文件流水（%TEMP%\\QuantDeskUpdate\\update.log）：")
print("  " + (open(log, encoding="utf-8").read().strip().replace("\n", "\n  ")
             if os.path.exists(log) else "(没有)"))

print("\n— 第 3 步：更新包被人换过一次（SHA 对不上）——必须一个字节都不动 —")
bad = json.loads(json.dumps(manifest))
bad["version"] = "3.0"
bad["pc"]["sha256"] = "0" * 64          # 发布时的指纹，和下载到的内容已经不一致
open(os.path.join(FEED_DIR, "update.json"), "w", encoding="utf-8").write(
    json.dumps(bad, ensure_ascii=False))
shutil.rmtree(upd, ignore_errors=True)
rep2 = os.path.join(SANDBOX, "tamper.txt")
if os.path.exists(rep2):
    os.remove(rep2)
subprocess.run([EXE, "/check-update", "/silent", "/report=" + rep2], capture_output=True, timeout=120)
print("客户端原话：", open(rep2, encoding="utf-8").read().strip().replace("\n", " | ")
      if os.path.exists(rep2) else "(没落话——没判出篡改，这是缺陷)")
print("没有备好任何东西：", not os.path.exists(os.path.join(upd, "pending.txt")))
print("页面还是上一轮的：", open(os.path.join(ROOT, "frontend", "dist", "index.html"),
                                 encoding="utf-8").read())
print("残留的下载包已删：", not os.path.exists(os.path.join(upd, "pending", "3.0", "QuantDesk-PC.zip")))

# 收尾：只收这条链路上、由沙箱目录里那个 exe 拉起来的进程（机器上一律不碰别的程序）
for _ in range(6):
    out = subprocess.run(["wmic", "process", "where", "name='QuantDesk.exe'", "get",
                          "processid,executablepath", "/format:csv"],
                         capture_output=True, text=True).stdout
    hits = [ln for ln in out.splitlines() if SANDBOX.lower() in ln.lower()]
    if not hits:
        break
    pid = hits[0].rstrip().split(",")[-1]
    subprocess.run(["taskkill", "/f", "/t", "/pid", pid], capture_output=True)
    time.sleep(1)
subprocess.run(["reg", "delete", r"HKCU\Software\QuantDesk", "/f"], capture_output=True)
print("\n已删除测试写入的 HKCU\\Software\\QuantDesk 配置")
srv.shutdown()
