"""直接执行随包页面的真实 JS；Node vm 中使用内存存储和无外呼 DOM。"""

import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest


NODE_RUNNER = r'''
const vm = require('vm');
let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', x => input += x);
process.stdin.on('end', () => {
  const req = JSON.parse(input);
  const values = new Map(Object.entries(req.storage || {}));
  const elements = new Map();
  let attached = 0;
  const head = {
    appendChild(s) { s.parentNode = head; attached++; s.onload(); },
    removeChild(s) { if (s.parentNode === head) { s.parentNode = null; attached--; } }
  };
  const context = {
    localStorage: {getItem: k => values.get(String(k)) || null,
                   setItem: (k, v) => values.set(String(k), String(v))},
    navigator: {onLine: true},
    document: {head,
      getElementById(id) { if (!elements.has(id)) elements.set(id, {
          innerHTML: '', value: '', addEventListener() {}, childNodes: [],
          querySelectorAll() {return []}, classList: {toggle() {}}
        }); return elements.get(id); },
      addEventListener() {}, createElement() { return {}; }, querySelectorAll() {return []}
    },
    setTimeout() {return 1}, clearTimeout() {},
    setInterval() {return 1}, clearInterval() {},
    console
  };
  context.window = context;
  vm.createContext(context);
  // 仅省去文件末尾的自动启动调用；前面的状态初始化与业务函数原样执行。
  vm.runInContext(req.script, context);
  vm.runInContext(req.check, context);
  Promise.resolve(context.result).then(result => {
    process.stdout.write(JSON.stringify({result, attached, storage: Object.fromEntries(values)}));
  }).catch(e => { console.error(e); process.exitCode = 1; });
});
'''


@pytest.fixture(scope="module")
def mobile_js():
    node = shutil.which("node")
    if not node:
        pytest.skip("需要已有 Node 运行真实手机页面脚本")
    root = Path(__file__).resolve().parents[1]
    html = (root / "QuantDesk-App/手机看盘.html").read_text(encoding="utf-8")
    script = re.search(r"<script>(.*?)</script>", html, re.S).group(1)
    script = script.rsplit("\nbind();", 1)[0]

    def run(check, storage=None):
        proc = subprocess.run([node, "-e", NODE_RUNNER], input=json.dumps({
            "script": script, "check": check, "storage": storage or {}}, ensure_ascii=False),
            capture_output=True, text=True, encoding="utf-8", timeout=10)
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout)
    return run


def test_mobile_saved_wallet_restores_during_real_page_initialization(mobile_js):
    saved = {"cash": {"CNY": 1234.56, "USD": 222, "HKD": 333},
             "positions": {"00700.HK": {"lots": [{"d": "20261105", "vol": 10, "price": 300}]}},
             "trades": [{"sym": "00700.HK", "price": 300, "vol": 10, "side": "buy"}]}
    out = mobile_js("result = state.sim", {"qd.sim.v3": json.dumps(saved)})
    assert out["result"] == saved


@pytest.mark.parametrize("price", ["0", "-1", "NaN", "Infinity"])
def test_mobile_confirmation_rejects_non_positive_or_non_finite_price(mobile_js, price):
    out = mobile_js("result = orderReject('00700.HK', 'buy', " + price + ", 100)")
    assert out["result"]


def test_mobile_odd_lot_sell_is_allowed_but_buy_keeps_market_minimum(mobile_js):
    out = mobile_js("""
state.sim.positions['688981.SH'] = {lots: [{d: '20000101', vol: 50, price: 10}]};
state.rows['688981.SH'] = {prev_close: 10};
result = {sell: orderReject('688981.SH', 'sell', 10, 50),
          buy: orderReject('688981.SH', 'buy', 10, 50)};
""")
    assert out["result"]["sell"] == ""
    assert out["result"]["buy"]


def test_successful_quote_scripts_are_removed_after_load(mobile_js):
    out = mobile_js("result = loadScript('https://offline.test/q', 'GBK', 1).then(() => true)")
    assert out["result"] is True
    assert out["attached"] == 0


def test_mobile_empty_watchlist_stays_empty_after_reload(mobile_js):
    assert mobile_js("result = state.pool", {"qd.pool.v1": "[]"})["result"] == []


def test_mobile_saved_watchlist_codes_are_normalized_and_deduplicated(mobile_js):
    out = mobile_js("result = state.pool", {"qd.pool.v1": json.dumps([
        "600000", "600000.SH", "700.HK", "bad code"])})
    assert out["result"] == ["600000.SH", "00700.HK"]
