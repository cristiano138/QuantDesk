"""QuantDesk 发布清单生成 —— 「我一发布，客户端就能自动更新」的那一份事实。

产出两样东西，都放在 `QuantDesk-App/release/`：

  update.json    客户端读的更新清单（PC 启动器 + 安卓外壳都读同一份）
  app.zip        电脑端整包（QuantDesk.exe / 安装程序 / frontend/dist / QuantDesk-App）

清单里每个包都带 SHA-256。客户端下载完先比 SHA-256，不一致就当没发生：
更新通道一旦被换成别的文件，装到用户机器上的就是别人的东西——这一条不许省。

用法：
    uv run python QuantDesk-App/发布.py                      # 打包 + 出清单
    uv run python QuantDesk-App/发布.py --print-command       # 顺带打印上传命令（不代你上传）

本脚本**不碰 git、不碰网络、不上传任何东西**。发布由人来做。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))          # QuantDesk-App/
APP_DIR = os.path.dirname(HERE)                       # 仓库根
RELEASE_DIR = os.path.join(HERE, "update")            # 更新产物都待在 QuantDesk-App 里，不另开顶层目录
APK_PATH = os.path.join(HERE, "QuantDesk.apk")
# 成品 exe 由 pc/build.py 落在 QuantDesk-App/ 顶层（装机后软件目录里也是这一层），
# 不是 pc/ 子目录——写错这里会在打包时才报「缺少成品文件」。
PC_EXE = os.path.join(HERE, "QuantDesk.exe")
PC_SETUP = os.path.join(HERE, "QuantDesk-安装程序.exe")

FEED = "https://github.com/cristiano138/QuantDesk/releases/latest/download"


def version_of() -> tuple[str, int]:
    """版本号有两个事实源：后端 `quantdesk/__init__.py` 和安卓 `build.py`。

    两边不一致时客户端会出现「手机已经是 1.2、电脑还是 1.1」这种讲不清的状态，
    所以这里直接拒绝生成清单，让人先去把 build.py 对齐。
    """
    init = os.path.join(APP_DIR, "quantdesk", "__init__.py")
    text = open(init, encoding="utf-8").read()
    m = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', text)
    if not m:
        raise SystemExit(f"{init} 里找不到 __version__")
    ver = m.group(1)

    b = open(os.path.join(HERE, "android", "build.py"), encoding="utf-8").read()
    vc = re.search(r'VERSION_CODE\s*=\s*["\'](\d+)["\']', b)
    vn = re.search(r'VERSION_NAME\s*=\s*["\']([^"\']+)["\']', b)
    if not vc or not vn:
        raise SystemExit("android/build.py 里找不到 VERSION_CODE / VERSION_NAME")
    if vn.group(1) != ver:
        raise SystemExit(f"版本号不一致：后端 {ver} / 安卓 {vn.group(1)}——先对齐再生成清单")
    return ver, int(vc.group(1))


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def pc_zip(out_zip: str) -> list[str]:
    """电脑端整包：两个 exe + 前端页面 + 手机端页面（跟着一起走，方便一台电脑装机）。

    包内目录树必须与装机后的布局一模一样——客户端是把包里的东西按相对路径铺进软件目录的，
    布局不一致就会铺出一个没人用的文件、真正的本体却没更新（这种失败在界面上看不出来）。
    """
    include = [
        (PC_EXE, "QuantDesk-App/QuantDesk.exe"),
        (PC_SETUP, "QuantDesk-App/QuantDesk-安装程序.exe"),
    ]
    dist = os.path.join(APP_DIR, "frontend", "dist")
    if not os.path.isdir(dist):
        # 只警告不拦：更新是「包里有什么就铺什么」，不镜像、不删多余文件，
        # 包里没页面就等于这一轮不动用户已有的页面。真拦下来反而会被绕过。
        print("警告：frontend/dist 不存在，这次包里不含前端页面（用户已有的页面不会被删除）。")
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as z:
        written = []
        for src, arc in include:
            if not os.path.isfile(src):
                raise SystemExit(f"缺少成品文件：{src}")
            z.write(src, arc)
            written.append(arc)
        for root, _dirs, files in os.walk(dist):
            for name in files:
                full = os.path.join(root, name)
                arc = os.path.join("frontend", "dist", os.path.relpath(full, dist))
                z.write(full, arc.replace("\\", "/"))
                written.append(arc)
        # 手机端页面单独留一份，装了软件的人也能直接拿到 html
        html = os.path.join(HERE, "手机看盘.html")
        if os.path.isfile(html):
            z.write(html, "QuantDesk-App/手机看盘.html")
            written.append("QuantDesk-App/手机看盘.html")
    return written


def build(notes: str, print_command: bool) -> dict:
    ver, code = version_of()
    os.makedirs(RELEASE_DIR, exist_ok=True)
    zip_path = os.path.join(RELEASE_DIR, "QuantDesk-PC.zip")
    members = pc_zip(zip_path)

    if not os.path.isfile(APK_PATH):
        raise SystemExit(f"缺少安卓安装包：{APK_PATH}——先跑 QuantDesk-App/android/build.py")

    manifest = {
        "version": ver,
        "version_code": code,
        "notes": notes,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "pc": {
            "name": "QuantDesk-PC.zip",
            "url": f"{FEED}/QuantDesk-PC.zip",
            "sha256": sha256(zip_path),
            "size": os.path.getsize(zip_path),
            "files": len(members),
        },
        "android": {
            "name": "QuantDesk.apk",
            "url": f"{FEED}/QuantDesk.apk",
            "sha256": sha256(APK_PATH),
            "size": os.path.getsize(APK_PATH),
            "version_code": code,
        },
    }
    out = os.path.join(RELEASE_DIR, "update.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(f"\n清单：{out}")
    print(f"整包：{zip_path}（{manifest['pc']['size']:,} 字节，{len(members)} 个文件）")
    print(f"APK ：{APK_PATH}（{manifest['android']['size']:,} 字节）")
    if print_command:
        tag = "v" + manifest["version"]
        iphone = os.path.join(HERE, "手机看盘.html")
        print("\n发布命令（自己执行，本脚本不代你上传）：\n"
              f"  gh release create {tag} --target main --title \"QuantDesk {manifest['version']}\""
              " --notes-file <说明文件>")
        print("  gh release upload %s --clobber "
              '"%s" "%s" "%s" "%s"' % (tag, APK_PATH, zip_path, out, iphone))
        print("  # 无 gh 时：仓库页面上手动把这四个文件拖到同一个 Release 的资源区"
              "（清单里的下载地址就是这两个文件名，改名客户端就取不到了；"
              "iPhone 那份建议在上传框里改名成 QuantDesk-iPhone.html）")
    return manifest


def verify(feed_url: str) -> int:
    """发布完自查：清单能不能取到、两个包在不在、SHA-256 对不对得上。"""
    import urllib.request

    def get(url: str) -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": "QuantDesk-UpdateCheck"})
        return urllib.request.urlopen(req, timeout=20).read()

    m = json.loads(get(feed_url).decode("utf-8"))
    ok = True
    for part in ("pc", "android"):
        info = m[part]
        blob = get(info["url"])
        got = hashlib.sha256(blob).hexdigest()
        same = got == info["sha256"] and len(blob) == info["size"]
        print(f"  {part:8} {info['name']:24} {'一致' if same else '不一致!'} "
              f"{len(blob):,} 字节  sha256={got[:12]}…")
        ok = ok and same
    print("发布验证：" + ("通过" if ok else "有问题，先别让用户点更新"))
    return 0 if ok else 1


def main() -> int:
    p = argparse.ArgumentParser(description="生成 QuantDesk 自动更新清单与整包")
    p.add_argument("--notes", default="", help="这次更新说一句话，显示在客户端更新提示里")
    p.add_argument("--print-command", action="store_true", help="打印上传命令（不执行）")
    p.add_argument("--verify", metavar="URL", help="只验证线上清单与包（发布完自查）")
    a = p.parse_args()
    if a.verify:
        return verify(a.verify)
    build(a.notes, a.print_command)
    return 0


if __name__ == "__main__":
    sys.exit(main())
