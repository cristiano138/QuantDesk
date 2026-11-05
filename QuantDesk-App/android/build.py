"""QuantDesk 手机看盘 —— Android 安装包构建脚本。

用法（在本机，工具链已经在 %TEMP%\\qd_build 里）：
    uv run python QuantDesk-App/android/build.py

产物：QuantDesk-App/QuantDesk-看盘-1.0.apk —— 手机上直接安装的原生 App，
不依赖电脑、不依赖浏览器、不自建服务器；页面随包，行情由手机自己联网取。

构建只用到临时目录里的 aapt2 / d8 / zipalign / apksigner 与 android.jar，
工作区里只留成品 APK 和这几份源码。
"""
import glob
import os
import shutil
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(HERE)
HTML_SOURCE = os.path.join(APP_DIR, "手机看盘.html")
OUT_APK = os.path.join(APP_DIR, "QuantDesk-看盘-1.0.apk")
KEYSTORE = os.path.join(HERE, "qd.keystore")
KEY_ALIAS = "quantdesk"
KEY_PASS = "quantdesk"

MIN_SDK = "21"
TARGET_SDK = "33"
VERSION_CODE = "1"
VERSION_NAME = "1.0"
ICON_SIZES = {"mdpi": 48, "hdpi": 72, "xhdpi": 96, "xxhdpi": 144, "xxxhdpi": 192}


def toolchain():
    root = os.environ.get("QD_TOOLCHAIN") or os.path.join(os.environ["TEMP"], "qd_build")
    bt = sorted(glob.glob(os.path.join(root, "bt", "android-*")))
    jar = sorted(glob.glob(os.path.join(root, "pl", "*", "android.jar")))
    if not bt or not jar:
        sys.exit("找不到打包工具链：%s 下需要 bt/android-*/ 与 pl/*/android.jar" % root)
    return root, bt[0], jar[0]


def java_bin(name):
    home = os.environ.get("JAVA_HOME")
    if home:
        p = os.path.join(home, "bin", name + ".exe")
        if os.path.exists(p):
            return p
    found = shutil.which(name)
    if not found:
        sys.exit("找不到 %s（需要 JDK 才能打包 APK）" % name)
    return found


def run(cmd, cwd=None, quiet=False):
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        print("命令失败：%s" % " ".join(cmd))
        print(proc.stdout)
        print(proc.stderr)
        sys.exit(1)
    if not quiet:
        names = " ".join(os.path.basename(a) if a.endswith((".jar", ".exe", ".zip", ".apk", ".dex")) else a for a in cmd[1:])
        print("$ %s %s" % (os.path.basename(cmd[0]), names[:400]))
    return proc.stdout


def make_icons(res_dir):
    sys.path.insert(0, APP_DIR)
    import appicon
    for folder, size in ICON_SIZES.items():
        d = os.path.join(res_dir, "mipmap-%s" % folder)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "ic_launcher.png"), "wb") as fh:
            fh.write(appicon.png_bytes(size))


def ensure_keystore():
    if os.path.exists(KEYSTORE):
        return
    run([java_bin("keytool"), "-genkeypair", "-v", "-keystore", KEYSTORE,
         "-alias", KEY_ALIAS, "-keyalg", "RSA", "-keysize", "2048", "-validity", "10950",
         "-storepass", KEY_PASS, "-keypass", KEY_PASS,
         "-dname", "CN=QuantDesk Watch, OU=watch, O=QuantDesk, L=Beijing, C=CN"], quiet=True)
    print("已生成自签名密钥 %s（以后升级覆盖安装要靠它，别删）" % os.path.relpath(KEYSTORE, APP_DIR))


def add_dex(src_apk, dex_file, dst_apk):
    with zipfile.ZipFile(src_apk) as src, zipfile.ZipFile(dst_apk, "w") as dst:
        for item in src.infolist():
            if item.filename == "resources.arsc":
                # Android 11+ 要求 resources.arsc 不压缩，否则装不上
                item.compress_type = zipfile.ZIP_STORED
            dst.writestr(item, src.read(item.filename))
        info = zipfile.ZipInfo("classes.dex", date_time=(2026, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_STORED
        info.external_attr = 0o644 << 16
        with open(dex_file, "rb") as fh:
            dst.writestr(info, fh.read())


def main():
    root, bt, android_jar = toolchain()
    work = os.path.join(root, "work")
    if os.path.isdir(work):
        shutil.rmtree(work)
    os.makedirs(work)
    print("工具链：%s" % root)

    # 1) 工程搬到纯 ASCII 路径构建（cmd 对中文路径的批处理解析不可靠）
    shutil.copy2(os.path.join(HERE, "AndroidManifest.xml"), work)
    shutil.copytree(os.path.join(HERE, "src"), os.path.join(work, "src"))
    shutil.copytree(os.path.join(HERE, "res"), os.path.join(work, "res"))
    make_icons(os.path.join(work, "res"))
    assets = os.path.join(work, "assets")
    os.makedirs(assets)
    shutil.copy2(HTML_SOURCE, os.path.join(assets, "index.html"))
    print("页面随包：%s → assets/index.html（%d 字节）"
          % (os.path.basename(HTML_SOURCE), os.path.getsize(os.path.join(assets, "index.html"))))

    aapt2 = os.path.join(bt, "aapt2.exe")
    zipalign = os.path.join(bt, "zipalign.exe")
    java = java_bin("java")
    d8_jar = os.path.join(bt, "lib", "d8.jar")
    signer_jar = os.path.join(bt, "lib", "apksigner.jar")

    # 2) 资源与清单 → 未含代码的 apk
    run([aapt2, "compile", "--dir", os.path.join(work, "res"), "-o", os.path.join(work, "res.zip")], cwd=work)
    link = [aapt2, "link", "-o", os.path.join(work, "base.apk"),
            "--manifest", os.path.join(work, "AndroidManifest.xml"),
            "-I", android_jar,
            "--min-sdk-version", MIN_SDK, "--target-sdk-version", TARGET_SDK,
            "--version-code", VERSION_CODE, "--version-name", VERSION_NAME,
            "-A", assets, "-0", "arsc",
            os.path.join(work, "res.zip")]
    run(link, cwd=work)

    # 3) Java → class → dex
    classes = os.path.join(work, "classes")
    os.makedirs(classes)
    srcs = []
    for base, _, files in os.walk(os.path.join(work, "src")):
        for f in files:
            if f.endswith(".java"):
                srcs.append(os.path.join(base, f))
    run([java_bin("javac"),
         "-encoding", "UTF-8", "-source", "8", "-target", "8",
         "-bootclasspath", android_jar, "-classpath", android_jar,
         "-d", classes] + srcs, cwd=work)
    dex_out = os.path.join(work, "dex")
    os.makedirs(dex_out)
    class_files = []
    for base, _, files in os.walk(classes):
        for f in files:
            if f.endswith(".class"):
                class_files.append(os.path.join(base, f))
    run([java, "-cp", d8_jar, "com.android.tools.r8.D8",
         "--min-api", MIN_SDK, "--lib", android_jar, "--output", dex_out] + class_files, cwd=work)
    dex_file = os.path.join(dex_out, "classes.dex")
    if not os.path.exists(dex_file):
        sys.exit("d8 没有产出 classes.dex")
    print("classes.dex %d 字节（%d 个 class）" % (os.path.getsize(dex_file), len(class_files)))

    # 4) 合包 → 对齐 → 签名
    with_dex = os.path.join(work, "with-dex.apk")
    add_dex(os.path.join(work, "base.apk"), dex_file, with_dex)
    aligned = os.path.join(work, "aligned.apk")
    run([zipalign, "-f", "-p", "4", with_dex, aligned], cwd=work)
    run([zipalign, "-c", "-v", "4", aligned], cwd=work, quiet=True)

    ensure_keystore()
    unsigned = os.path.join(work, "unsigned.apk")
    shutil.copy2(aligned, unsigned)
    run([java, "-jar", signer_jar, "sign",
         "--ks", KEYSTORE, "--ks-key-alias", KEY_ALIAS,
         "--ks-pass", "pass:" + KEY_PASS, "--key-pass", "pass:" + KEY_PASS,
         "--v2-signing-enabled", "true", "--v3-signing-enabled", "true",
         "--out", os.path.join(work, "final.apk"), unsigned], cwd=work)

    print(run([java, "-jar", signer_jar, "verify", "--verbose", os.path.join(work, "final.apk")], cwd=work, quiet=True))
    aapt = os.path.join(bt, "aapt.exe")
    badge = run([aapt, "dump", "badging", os.path.join(work, "final.apk")], cwd=work, quiet=True)
    for line in badge.splitlines():
        if line.startswith(("package:", "application-label:", "launchable-activity:", "sdkVersion", "targetSdkVersion", "uses-permission")):
            print("  " + line)

    shutil.copy2(os.path.join(work, "final.apk"), OUT_APK)
    print("\n成品：%s（%.0f KB）" % (OUT_APK, os.path.getsize(OUT_APK) / 1024))


if __name__ == "__main__":
    main()
