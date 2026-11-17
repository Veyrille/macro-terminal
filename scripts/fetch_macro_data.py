import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

FRED_API_KEY = os.environ.get("FRED_API_KEY", "").strip()
BASE_URL = "https://api.stlouisfed.org/fred/series/observations"
BJT = timezone(timedelta(hours=8))

# 16 个 FRED 指标配置
SERIES_CONFIG = {
    "CPIAUCSL": {"name": "CPI 消费者价格指数", "units": "lin", "calc": "mom_yoy"},
    "CPILFESL": {"name": "核心 CPI", "units": "lin", "calc": "mom_yoy"},
    "PCEPI": {"name": "PCE 物价指数", "units": "lin", "calc": "mom_yoy"},
    "PCEPILFE": {"name": "核心 PCE", "units": "lin", "calc": "mom_yoy"},
    "PPIFIS": {"name": "PPI 生产者价格指数", "units": "lin", "calc": "mom_yoy"},
    "PPIFES": {"name": "核心 PPI", "units": "lin", "calc": "mom_yoy"},
    "PAYEMS": {"name": "非农就业人数", "units": "lin", "calc": "diff"},
    "UNRATE": {"name": "失业率", "units": "lin", "calc": "level"},
    "CES0500000003": {"name": "平均时薪", "units": "lin", "calc": "mom_yoy"},
    "ICSA": {"name": "初请失业金人数", "units": "lin", "calc": "level_weekly"},
    "JTSJOL": {"name": "JOLTS 职位空缺", "units": "lin", "calc": "level"},
    "A191RL1Q225SBEA": {"name": "GDP 季度增速", "units": "lin", "calc": "level_qtr"},
    "RSAFS": {"name": "零售销售", "units": "lin", "calc": "mom_yoy"},
    "DFEDTARU": {"name": "联邦基金利率上限", "units": "lin", "calc": "level_monthly_end"},
    "DFEDTARL": {"name": "联邦基金利率下限", "units": "lin", "calc": "level_monthly_end"},
    "DGS2": {"name": "2 年期美债收益率", "units": "lin", "calc": "level_monthly_end"},
    "DGS10": {"name": "10 年期美债收益率", "units": "lin", "calc": "level_monthly_end"},
    "DTWEXBGS": {"name": "美元指数 (广义)", "units": "lin", "calc": "level_monthly_end"},
}


def fetch_fred_series(series_id: str, limit: int = 120) -> list[dict]:
    if not FRED_API_KEY:
        raise RuntimeError("Missing FRED_API_KEY environment variable")
    params = urllib.parse.urlencode({
        "series_id": series_id,
        "api_key": FRED_API_KEY,
        "file_type": "json",
        "sort_order": "desc",
        "limit": limit,
    })
    url = f"{BASE_URL}?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": "MacroTerminalBot/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    obs = [
        {"date": o["date"], "value": float(o["value"])}
        for o in payload.get("observations", [])
        if o.get("value") not in (None, ".", "")
    ]
    obs.sort(key=lambda x: x["date"])
    return obs


def compute_mom_yoy(obs: list[dict]) -> dict:
    """计算环比 (%)、同比 (%) 及近 24 个月环比序列"""
    if len(obs) < 14:
        return {"latest": None, "history": []}
    history_mom = []
    for i in range(1, len(obs)):
        prev_val = obs[i - 1]["value"]
        curr_val = obs[i]["value"]
        mom = round(((curr_val - prev_val) / prev_val) * 100, 2) if prev_val else 0.0
        history_mom.append({"date": obs[i]["date"], "value": mom, "raw": curr_val})
    latest_item = history_mom[-1]
    prev_item = history_mom[-2] if len(history_mom) >= 2 else None
    yoy = None
    if len(obs) >= 13 and obs[-13]["value"]:
        yoy = round(((obs[-1]["value"] - obs[-13]["value"]) / obs[-13]["value"]) * 100, 2)
    return {
        "date": latest_item["date"],
        "latest": latest_item["value"],
        "prev": prev_item["value"] if prev_item else None,
        "yoy": yoy,
        "raw": latest_item["raw"],
        "history": [{"date": x["date"], "value": x["value"]} for x in history_mom[-24:]],
    }


def compute_diff(obs: list[dict], scale: float = 0.1) -> dict:
    """计算每月新增差值（如 PAYEMS 千人转万人，默认除以 10 即 * 0.1）"""
    if len(obs) < 3:
        return {"latest": None, "history": []}
    diffs = []
    for i in range(1, len(obs)):
        delta = round((obs[i]["value"] - obs[i - 1]["value"]) * scale, 1)
        diffs.append({"date": obs[i]["date"], "value": delta, "raw": obs[i]["value"]})
    return {
        "date": diffs[-1]["date"],
        "latest": diffs[-1]["value"],
        "prev": diffs[-2]["value"] if len(diffs) >= 2 else None,
        "raw": diffs[-1]["raw"],
        "history": [{"date": x["date"], "value": x["value"]} for x in diffs[-24:]],
    }


def sample_monthly_end(obs: list[dict]) -> list[dict]:
    """对日频/周频序列按自然月取每月最后一个有效值，保留近 24 个月"""
    monthly = {}
    for item in obs:
        ym = item["date"][:7]
        monthly[ym] = item
    sorted_months = sorted(monthly.keys())[-24:]
    return [{"date": monthly[m]["date"], "value": round(monthly[m]["value"], 2)} for m in sorted_months]


def process_series(series_id: str, cfg: dict) -> dict:
    obs = fetch_fred_series(series_id, limit=800 if cfg["calc"] in ("level_monthly_end", "level_weekly") else 48)
    calc = cfg["calc"]
    if calc == "mom_yoy":
        res = compute_mom_yoy(obs)
    elif calc == "diff":
        res = compute_diff(obs, scale=0.1)
    elif calc == "level_monthly_end":
        sampled = sample_monthly_end(obs)
        res = {
            "date": obs[-1]["date"] if obs else None,
            "latest": round(obs[-1]["value"], 2) if obs else None,
            "prev": round(obs[-2]["value"], 2) if len(obs) >= 2 else None,
            "history": sampled,
        }
    elif calc == "level_weekly":
        sampled = sample_monthly_end(obs)
        res = {
            "date": obs[-1]["date"] if obs else None,
            "latest": round(obs[-1]["value"] / 10000, 1) if obs else None,
            "prev": round(obs[-2]["value"] / 10000, 1) if len(obs) >= 2 else None,
            "history": [{"date": x["date"], "value": round(x["value"] / 10000, 1)} for x in sampled],
        }
    elif calc == "level_qtr":
        recent = obs[-8:]
        res = {
            "date": obs[-1]["date"] if obs else None,
            "latest": round(obs[-1]["value"], 1) if obs else None,
            "prev": round(obs[-2]["value"], 1) if len(obs) >= 2 else None,
            "history": recent,
        }
    else:
        recent = obs[-24:]
        scale = 0.1 if series_id == "JTSJOL" else 1.0
        res = {
            "date": obs[-1]["date"] if obs else None,
            "latest": round(obs[-1]["value"] * scale, 1) if obs else None,
            "prev": round(obs[-2]["value"] * scale, 1) if len(obs) >= 2 else None,
            "history": [{"date": x["date"], "value": round(x["value"] * scale, 1)} for x in recent],
        }
    res["name"] = cfg["name"]
    return res


def main():
    output = {
        "updated_at_bjt": datetime.now(BJT).strftime("%Y-%m-%d %H:%M:%S"),
        "series": {},
        "manual_overrides": {},
    }

    # 读取可选的手动覆写文件（用于存放 ISM PMI、市场预期值与 CME FedWatch 概率）
    overrides_path = os.path.join(os.path.dirname(__file__), "overrides.json")
    if os.path.exists(overrides_path):
        with open(overrides_path, "r", encoding="utf-8") as f:
            output["manual_overrides"] = json.load(f)

    for sid, cfg in SERIES_CONFIG.items():
        try:
            output["series"][sid] = process_series(sid, cfg)
            print(f"[OK] {sid} -> latest={output['series'][sid].get('latest')} ({output['series'][sid].get('date')})")
        except Exception as e:
            print(f"[WARN] {sid} fetch failed: {e}")

    out_path = os.environ.get("MACRO_OUT_PATH", "macro_data.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"Saved {out_path} at {output['updated_at_bjt']} BJT")


if __name__ == "__main__":
    main()