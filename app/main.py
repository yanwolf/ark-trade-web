"""
main.py — Flask dashboard
- 輸入今日建議(取代原本手動編輯CSV)
- 顯示建議 vs 目前持股 的比對清單
- 按下確認才會真的呼叫 Shioaji 下單
- 顯示歷史紀錄
"""
from flask import Flask, request, redirect, url_for, render_template, flash
import os

import db
import shioaji_client as sc

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "ark-trade-dashboard-secret")
db.init_db()


@app.route("/")
def dashboard():
    suggestions = db.get_pending_suggestions()

    positions_error = None
    confirm_list = []
    try:
        current_positions = sc.get_current_positions()
        for s in suggestions:
            confirm_list.append({
                **s,
                "held_qty": current_positions.get(s["code"], 0),
            })
    except Exception as e:
        positions_error = str(e)
        confirm_list = [{**s, "held_qty": "?"} for s in suggestions]

    logs = db.get_recent_logs(30)

    return render_template(
        "dashboard.html",
        confirm_list=confirm_list,
        logs=logs,
        mode_label=sc.mode_label(),
        positions_error=positions_error,
    )


@app.route("/add", methods=["POST"])
def add_suggestion():
    code = request.form["code"].strip()
    action = request.form["action"].strip().lower()
    qty = int(request.form["qty"])
    note = request.form.get("note", "").strip()
    if action in ("buy", "sell") and code and qty > 0:
        db.add_suggestion(code, action, qty, note)
    return redirect(url_for("dashboard"))


@app.route("/delete/<int:sid>", methods=["POST"])
def delete_suggestion(sid):
    db.delete_suggestion(sid)
    return redirect(url_for("dashboard"))


@app.route("/confirm", methods=["POST"])
def confirm_orders():
    suggestions = db.get_pending_suggestions()
    if not suggestions:
        return redirect(url_for("dashboard"))

    current_positions = sc.get_current_positions()
    confirm_list = [
        {**s, "held_qty": current_positions.get(s["code"], 0)}
        for s in suggestions
    ]

    results = sc.place_orders(confirm_list)

    log_entries = [
        {
            "mode": sc.mode_label(),
            "code": r["code"],
            "action": r["action"],
            "qty": r["qty"],
            "held_qty": r["held_qty"],
            "status": r["status"],
            "error": r["error"],
            "note": r["note"],
        }
        for r in results
    ]
    db.write_log(log_entries)
    db.mark_consumed([s["id"] for s in suggestions])

    return redirect(url_for("dashboard"))


@app.route("/api-test/login", methods=["POST"])
def api_test_login():
    try:
        version, accounts = sc.test_login()
        lines = [f"shioaji 版本: {version}", ""]
        for acc in accounts:
            signed = getattr(acc, "signed", None)
            mark = "✅ 簽署+API測試皆已通過" if signed else "❌ 尚未完成(簽署或API測試審核未過)"
            lines.append(f"{type(acc).__name__} {acc.account_id}: {mark}")
        flash("\n".join(lines))
    except Exception as e:
        flash(f"❌ 登入測試失敗: {e}")
    return redirect(url_for("dashboard"))


@app.route("/api-test/order", methods=["POST"])
def api_test_order():
    try:
        trade = sc.test_place_order()
        flash(f"✅ 下單測試已送出\n{trade}")
    except Exception as e:
        flash(f"❌ 下單測試失敗: {e}")
    return redirect(url_for("dashboard"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=True)
