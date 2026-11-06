"""可安装包（QuantDesk-App）的结构自检。

这几条都是本轮真实踩过的坑，钉成用例而不是靠人记：
· 更新包的目录树与装机布局不一致 → 文件铺到别处，真正的本体没更新，界面上看不出任何异常；
· 页面里两个同名函数 → 后写的把先写的顶掉，下单面板安静地显示「涨停 不限」；
· 版本号有两个事实源（后端 __init__ 与安卓 build.py），不一致时会出现
  「手机已经 1.2、电脑还是 1.1」这种讲不清的状态。
"""
import io
import os
import re

from quantdesk import __version__

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "QuantDesk-App")


def _read(*parts) -> str:
    return io.open(os.path.join(APP, *parts), encoding="utf-8").read()


def test_update_package_layout_matches_installed_layout():
    """发布脚本往包里写的相对路径，必须与启动器铺文件时认的那棵树一模一样。

    装机后本体在 软件目录\\QuantDesk-App\\QuantDesk.exe，所以包内也必须是这个相对位置。
    两边各写一套时客户端会「校验通过、下载成功、什么都没更新」。
    """
    publish = _read("发布.py")
    launcher = _read("pc", "Launcher.cs")

    m = re.search(r'ExeRelPath\s*=\s*@"([^"]+)"', launcher)
    assert m, "Launcher.cs 里找不到 ExeRelPath"
    rel = m.group(1).replace("\\", "/")

    arcs = set(re.findall(r'\((?:PC_EXE|PC_SETUP),\s*"([^"]+)"\)', publish))
    assert rel in arcs, f"包里本体是 {sorted(arcs)}，装机布局要的是 {rel}"
    assert not any(a.split("/")[-1] == "QuantDesk.exe" and "/" not in a for a in arcs), \
        "本体不许躺在包根目录（那样铺到软件目录顶层，装机的那份不会变）"


def test_apk_and_manifest_names_agree_with_publish_script():
    """清单里 android.url 的文件名与本地 APK 必须是同一个名字——改名要两边一起改。"""
    publish = _read("发布.py")
    apk = re.search(r'APK_PATH\s*=\s*os\.path\.join\(HERE,\s*"([^"]+)"\)', publish)
    assert apk, "发布.py 里找不到 APK_PATH"
    name = apk.group(1)
    assert os.path.isfile(os.path.join(APP, name)), f"成品 APK 不存在：{name}"
    assert f"{{FEED}}/{name}" in publish, "清单下载地址与本地 APK 文件名不是同一个"
    build = _read("android", "build.py")
    assert f'OUT_APK = os.path.join(APP_DIR, "{name}")' in build, \
        "android/build.py 的产物名与发布脚本认的不是一个文件"


def test_page_has_no_duplicate_top_level_functions():
    """顶层同名函数会互相顶掉，被顶掉那一路安静失效（实测：涨跌停区间全显示「不限」）。"""
    html = _read("手机看盘.html")
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    names = re.findall(r"^function\s+([A-Za-z0-9_$]+)", "\n".join(scripts), re.M)
    dup = sorted({n for n in names if names.count(n) > 1})
    assert not dup, f"顶层重名函数：{dup}"


def test_page_order_path_checks_rules_before_filling():
    """下单这条路的规则判据只许有一处：预估与确认共用 orderReject。

    只靠按钮 disabled 挡人＝脚本或一次竞态就能绕过；两条各写一套则会漂成两套规则。
    """
    html = _read("手机看盘.html")
    assert "function orderReject(" in html, "规则判据没有集中成一个函数"
    assert html.count("orderReject(sym") >= 2, "osEstimate 与 osConfirm 没有共用同一条判据"
    assert "function limitBand(" in html, "涨跌停区间函数不见了（与逐笔分层的 bandOf 不是一回事）"


def test_backend_and_android_versions_are_one_number():
    assert f'VERSION_NAME = "{__version__}"' in _read("android", "build.py"), \
        f"安卓 VERSION_NAME 与后端 __version__={__version__} 不一致"


def test_apk_version_has_one_source_and_is_injected():
    """版本号只许在 android/build.py 定义一次，由它注入构建副本。

    实测踩过：清单自带旧值时，aapt2 link 的构建参数被顶掉，源码升到 1.3、装出来的包还是 1.2，
    老 App 比较 version_code 认为「我就是最新」，从此收不到任何更新，而界面上一切正常。
    """
    manifest = _read("android", "AndroidManifest.xml")
    assert "android:versionCode" not in manifest, "清单里不要再写 versionCode，唯一事实源是 android/build.py"
    assert "android:versionName" not in manifest, "清单里不要再写 versionName，唯一事实源是 android/build.py"
    build = _read("android", "build.py")
    assert 'manifest.index(">", manifest.index("<manifest"))' in build, "没有把版本号注入构建副本"
    assert "版本核对" in build and "页面核对" in build, "出包后没有回读包体断言版本与随包页面一致"
