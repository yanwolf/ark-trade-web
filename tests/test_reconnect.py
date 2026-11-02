"""
ark-trade-web 斷線自動重新登入測試(stub 掉 shioaji,不需要真的連永豐)。
執行: cd ark-trade-web && python -m unittest tests/test_reconnect.py -v
"""
import os
import sys
import tempfile
import threading
import time
import types
import unittest

# ── 測試環境:不用 Postgres、不啟動背景健康檢查 ──────────────
os.environ.pop("DATABASE_URL", None)
os.environ.update(SJ_API_KEY="k", SJ_SECRET_KEY="s", SJ_SIMULATION="true",
                  SJ_HEALTH_INTERVAL="0", ORDER_CONFIRM_PASSWORD="")
_TMP = tempfile.mkdtemp()
os.chdir(_TMP)  # local.db 建在暫存資料夾

SESSION_ERR = ("Shioaji error Session error SolClient send request api/v1/portfolio/position_unit, "
               "code: NotReady, SubCode(SessionNotEstablished), "
               "\"Unable to wait for session '(c0,s1)_sinopac' to be established\"")


# ── shioaji stub ────────────────────────────────────────────
class Scenario:
    lock = threading.Lock()
    dead = set()          # 哪幾個 FakeAPI 實例(index)的伺服器呼叫會丟斷線錯誤
    login_delay = 0.0
    instances = []
    logins = 0
    logouts = 0
    ca_activations = 0
    place_calls = 0
    place_fail_codes = set()   # 這些代號 place_order 丟斷線

    @classmethod
    def reset(cls):
        cls.dead = set(); cls.login_delay = 0.0; cls.instances = []
        cls.logins = cls.logouts = cls.ca_activations = cls.place_calls = 0
        cls.place_fail_codes = set()


class FakeContract:
    def __init__(self, code, name):
        self.code, self.name = code, name
        self.reference, self.limit_up, self.limit_down = 30.0, 33.0, 27.0
        self.category = "00"
        self.security_type = "STK"


class FakeStocks:
    NAMES = {"0050": "元大台灣50", "00878": "國泰永續高股息", "00631L": "元大台灣50正2"}

    def __getitem__(self, code):
        return FakeContract(code, self.NAMES.get(code, ""))


class Pos:
    def __init__(self, code, qty):
        self.code, self.quantity, self.price, self.last_price, self.pnl = code, qty, 30.0, 31.0, 10.0


class FakeTrade:
    _n = 0

    def __init__(self, code):
        FakeTrade._n += 1
        self.order = types.SimpleNamespace(id=f"oid{FakeTrade._n}", seqno=f"s{FakeTrade._n}", ordno="",
                                           action="Action.Buy", quantity=10, price=30.0)
        self.status = types.SimpleNamespace(status="Status.PendingSubmit")
        self.contract = types.SimpleNamespace(code=code, security_type="STK")


class FakeAPI:
    def __init__(self, simulation=True):
        with Scenario.lock:
            self.idx = len(Scenario.instances)
            Scenario.instances.append(self)
        self.Contracts = types.SimpleNamespace(Stocks=FakeStocks())
        self.stock_account = "STOCK_ACC"
        self.cb = None

    def _check(self):
        if self.idx in Scenario.dead:
            raise Exception(SESSION_ERR)

    def login(self, api_key, secret_key, **kw):
        time.sleep(Scenario.login_delay)
        with Scenario.lock:
            Scenario.logins += 1

    def logout(self):
        with Scenario.lock:
            Scenario.logouts += 1

    def activate_ca(self, **kw):
        Scenario.ca_activations += 1
        return True

    def set_order_callback(self, cb):
        self.cb = cb

    def list_positions(self, account, unit=None):
        self._check()
        return [Pos("0050", 94), Pos("00878", 645)]

    def update_status(self, account):
        self._check()

    def list_trades(self):
        return []

    def Order(self, **kw):
        return kw

    def place_order(self, contract, order):
        with Scenario.lock:
            Scenario.place_calls += 1
        if contract.code in Scenario.place_fail_codes:
            raise Exception(SESSION_ERR)
        return FakeTrade(contract.code)


sj = types.ModuleType("shioaji")
sj.Shioaji = FakeAPI
sj.__version__ = "stub"
sj.Unit = types.SimpleNamespace(Share="Share", Common="Common")
const = types.ModuleType("shioaji.constant")
const.Action = types.SimpleNamespace(Buy="Buy", Sell="Sell")
const.StockPriceType = types.SimpleNamespace(LMT="LMT")
const.OrderType = types.SimpleNamespace(ROD="ROD")
const.StockOrderLot = types.SimpleNamespace(IntradayOdd="IntradayOdd")
sj.constant = const
sys.modules["shioaji"] = sj
sys.modules["shioaji.constant"] = const

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
import shioaji_client as sc  # noqa: E402
import db  # noqa: E402

sc.WARMUP_DELAY_SEC = 0.01
sc.RELOGIN_BACKOFF_REQUEST = (0.01, 0.01)
sc.RELOGIN_BACKOFF_BACKGROUND = (0.01, 0.01, 0.01)
sc.LOGOUT_TIMEOUT_SEC = 1

import main  # noqa: E402


def reset_client():
    Scenario.reset()
    sc._api = None
    sc._status.update(state="idle", last_relogin_at=None, last_relogin_reason=None,
                      last_error=None, last_attempt_at=None, last_health_ok_at=None, relogin_count=0)
    sc._events.clear()
    sc._own_order_ids.clear()
    sc._name_cache.clear()


def trade_log_count():
    conn = db.get_conn(); cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM trade_log"); n = cur.fetchone()[0]
    conn.close(); return n


class SessionDeadTest(unittest.TestCase):
    def test_detection(self):
        for m in [SESSION_ERR, "SessionNotEstablished", "code: NotReady", "Unable to wait for session x",
                  "User not logged in", "token expired", "Token is invalid", "HTTP 401", "session expired"]:
            self.assertTrue(sc.session_dead(Exception(m)), m)
        for m in ["price out of range", "insufficient balance", "contract not found 2330", "code 4010"]:
            self.assertFalse(sc.session_dead(Exception(m)), m)


class ReconnectTest(unittest.TestCase):
    def setUp(self):
        reset_client()

    def test_positions_relogin_then_retry_ok(self):
        sc.get_api()                     # 第一次登入(實例 0)
        Scenario.dead = {0}              # 實例 0 失效(例如 txf-sim 重新登入把它擠掉)
        details = sc.get_position_details()
        self.assertEqual([d["code"] for d in details], ["0050", "00878"])
        self.assertEqual(details[0]["name"], "元大台灣50")
        self.assertEqual(Scenario.logins, 2)       # 初次 + 1 次重新登入
        self.assertEqual(Scenario.logouts, 1)      # 先 logout 舊的
        self.assertIsNotNone(sc._api.cb)           # 新實例有重新註冊 callback
        st = sc.get_status()
        self.assertEqual(st["state"], "connected")
        self.assertEqual(st["relogin_count"], 1)
        self.assertIn("list_positions", st["last_relogin_reason"])

    def test_dashboard_page_gets_positions_after_relogin(self):
        sc.get_api()
        Scenario.dead = {0}
        client = main.app.test_client()
        html = client.get("/").get_data(as_text=True)
        self.assertIn("國泰永續高股息", html)       # 名稱欄位
        self.assertIn("<th>名稱</th>", html)
        self.assertIn("已連線", html)
        self.assertIn("最後重新登入", html)
        self.assertNotIn("無法取得目前持股", html)

    def test_ten_concurrent_requests_single_relogin(self):
        sc.get_api()
        Scenario.dead = {0}
        Scenario.login_delay = 0.3       # 讓重新登入期間其他請求確實擠在一起
        results, errors = [], []
        barrier = threading.Barrier(10)

        def worker():
            barrier.wait()
            try:
                results.append(sc.get_current_positions())
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        [t.start() for t in threads]; [t.join() for t in threads]
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 10)
        self.assertEqual(Scenario.logins, 2, "10 個斷線請求只能觸發 1 次重新登入")
        self.assertEqual(sc.get_status()["relogin_count"], 1)

    def test_relogin_backoff_and_failure_status(self):
        sc.get_api()
        Scenario.dead = {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15}  # 新登入的也都不通
        with self.assertRaises(Exception):
            sc.get_current_positions()
        st = sc.get_status()
        self.assertEqual(st["state"], "failed")
        self.assertEqual(Scenario.logins, 1 + 3)   # 初次 + (立即 + 2 次退避)
        logins_before = Scenario.logins
        with self.assertRaises(Exception):          # 冷卻期內不會再觸發登入
            sc.get_current_positions()
        self.assertEqual(Scenario.logins, logins_before)
        # 背景健康檢查不受冷卻限制,連線恢復後會救回來
        Scenario.dead = set()
        sc.health_check_once()
        self.assertEqual(sc.get_status()["state"], "connected")

    def test_place_order_session_dead_no_resend(self):
        sc.get_api()
        Scenario.place_fail_codes = {"0050"}
        items = [
            {"id": 1, "code": "00878", "action": "buy", "qty": 10, "held_qty": 645, "note": "", "price": 30.0},
            {"id": 2, "code": "0050", "action": "buy", "qty": 5, "held_qty": 94, "note": "", "price": 30.0},
            {"id": 3, "code": "00631L", "action": "sell", "qty": 7, "held_qty": 724, "note": "", "price": 30.0},
        ]
        results = sc.place_orders(items)
        self.assertEqual(Scenario.place_calls, 2, "斷線那筆不能重送,後面的也不能自動送")
        self.assertEqual(Scenario.logins, 2, "只重新登入一次")
        self.assertEqual([r["status"] for r in results][1:], ["ConnectionLost", "NotSent"])
        self.assertIn(sc.ORDER_LOST_MSG, results[1]["error"])
        self.assertEqual([r["consume"] for r in results], [True, True, False])

    def test_confirm_route_keeps_unsent_pending(self):
        sc.get_api()
        for row in db.get_pending_suggestions():
            db.delete_suggestion(row["id"])
        db.add_suggestion("00878", "buy", 10, "", 30.0)
        db.add_suggestion("0050", "buy", 5, "", 30.0)
        db.add_suggestion("00631L", "sell", 7, "", 30.0)
        Scenario.place_fail_codes = {"0050"}
        client = main.app.test_client()
        resp = client.post("/confirm", follow_redirects=True)
        html = resp.get_data(as_text=True)
        self.assertIn("下單時連線中斷", html)
        pending = [p["code"] for p in db.get_pending_suggestions()]
        self.assertEqual(pending, ["00631L"])
        self.assertEqual(Scenario.place_calls, 2)


class LiveModeAndHealthTest(unittest.TestCase):
    def setUp(self):
        reset_client()

    def tearDown(self):
        sc.SIMULATION = True
        sc.CA_PATH = None

    def test_relogin_reactivates_ca(self):
        sc.SIMULATION = False
        sc.CA_PATH = "/tmp/fake.pfx"
        sc.get_api()
        Scenario.dead = {0}
        sc.get_current_positions()
        self.assertEqual(Scenario.ca_activations, 2, "重新登入後要重新啟用 CA")

    def test_health_check_relogins_before_user_opens_page(self):
        sc.get_api()
        Scenario.dead = {0}
        sc.health_check_once()
        self.assertEqual(Scenario.logins, 2)
        self.assertEqual(sc.get_status()["state"], "connected")
        self.assertIn("背景健康檢查", sc.get_status()["last_relogin_reason"])
        sc.get_current_positions()       # 使用者開頁面時已是好的,不會再登入
        self.assertEqual(Scenario.logins, 2)


class CallbackFilterTest(unittest.TestCase):
    def setUp(self):
        reset_client()
        sc.get_api()

    def test_ignore_futures_and_foreign_orders(self):
        before = trade_log_count()
        fut = types.SimpleNamespace(name="FuturesDeal", value="FDEAL")
        stk_deal = types.SimpleNamespace(name="StockDeal", value="SDEAL")
        stk_order = types.SimpleNamespace(name="StockOrder", value="SORDER")
        # txf-sim 的期貨成交
        self.assertFalse(sc.handle_order_event(fut, {"trade_id": "x1", "code": "TMF"}))
        # 同帳戶但不是本程式送出的零股成交(例如 App 手動下單)
        self.assertFalse(sc.handle_order_event(stk_deal, {"trade_id": "zzz", "seqno": "999", "code": "2330"}))
        # 透過 api 實例上的 callback 走一次(證明註冊的就是這個過濾器)
        self.assertFalse(sc._api.cb(fut, {"trade_id": "x2"}))
        # 本程式送出的委託 → 會被辨識,但只記錄在記憶體/日誌
        trade = sc.place_orders([{"id": 9, "code": "00878", "action": "buy", "qty": 1,
                                  "held_qty": 0, "note": "", "price": 30.0}])
        own_id = next(iter(sc._own_order_ids))
        self.assertTrue(sc.handle_order_event(stk_order, {"order": {"id": own_id}, "contract": {"code": "00878", "security_type": "STK"}}))
        self.assertEqual(trade_log_count(), before, "回報不能寫入 trade_log")


if __name__ == "__main__":
    unittest.main(verbosity=2)
