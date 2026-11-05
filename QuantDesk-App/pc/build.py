"""QuantDesk 看盘台 —— 电脑端原生 exe 构建脚本。

只用 Windows 自带的东西：
  · C# 编译器 csc.exe（C:\\Windows\\Microsoft.NET\\Framework64\\v4.0.30319，系统自带）
  · 图标由 appicon.py 纯 Python 画，不下载任何图像库
产物：
  QuantDesk-App\\QuantDesk.exe            双击打开软件（原生窗口，不是浏览器）
  QuantDesk-App\\QuantDesk-安装程序.exe    装进开始菜单 / 桌面 / 「设置 → 应用」

用法：uv run python QuantDesk-App/pc/build.py
"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(HERE)
sys.path.insert(0, APP_DIR)
import appicon  # noqa: E402

CSC = os.path.join(os.environ.get("WINDIR", r"C:\Windows"),
                   "Microsoft.NET", "Framework64", "v4.0.30319", "csc.exe")
ICO = os.path.join(HERE, "QuantDesk.ico")
RUN_EXE = os.path.join(APP_DIR, "QuantDesk.exe")
SETUP_EXE = os.path.join(APP_DIR, "QuantDesk-安装程序.exe")
SRC = os.path.join(HERE, "Launcher.cs")
REFS = ["/r:System.dll", "/r:System.Core.dll", "/r:System.Windows.Forms.dll", "/r:System.Drawing.dll"]


def compile_exe(out_path, define=None):
    if not os.path.exists(CSC):
        sys.exit("找不到系统自带的 C# 编译器：" + CSC)
    cmd = [CSC, "/nologo", "/target:winexe", "/codepage:65001", "/utf8output",
           "/nowarn:0219,0168", "/win32icon:" + ICO, "/out:" + out_path] + REFS
    if define:
        cmd.append("/define:" + define)
    cmd.append(SRC)
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        print(proc.stdout)
        print(proc.stderr)
        sys.exit("编译失败：" + out_path)
    print("  %-28s %6.0f KB" % (os.path.basename(out_path), os.path.getsize(out_path) / 1024))


def main():
    with open(ICO, "wb") as fh:
        fh.write(appicon.ico_bytes([16, 32, 48, 64, 128, 256]))
    print("图标：%s（%d 字节，6 档尺寸）" % (os.path.basename(ICO), os.path.getsize(ICO)))

    print("编译：")
    compile_exe(RUN_EXE)
    compile_exe(SETUP_EXE, define="SETUP")

    report = os.path.join(tempfile.gettempdir(), "quantdesk-selftest.txt")
    if os.path.exists(report):
        os.remove(report)
    proc = subprocess.run([RUN_EXE, "/selftest", report], capture_output=True, timeout=60)
    if not os.path.exists(report):
        sys.exit("自检没产出报告（exit=%s）" % proc.returncode)
    print("\n自检（QuantDesk.exe /selftest）：")
    print("  " + open(report, encoding="utf-8").read().replace("\n", "\n  ").rstrip())


if __name__ == "__main__":
    main()
