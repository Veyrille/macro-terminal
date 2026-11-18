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


# ============ ForexFactory 财经日历（faireconomy 公开 JSON，无需 Key） ============
# 每天由 GitHub Actions 随 FRED 一起抓取未来两周的市场预期，写入 macro_data.json 的
# expectations / future_expectations / future_expectation_details，供前端日历直接展示。
# 注意：nextweek 文件并非时刻存在（实测会 404），单个接口失败只跳过该源，绝不拖垮整轮更新；
# 超过两周的远期月份投行尚未发布一致预期，抓不到属正常，前端对应显示 --。
FF_CALENDAR_URLS = [
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
    "https://nfs.faireconomy.media/ff_calendar_nextweek.json",
]

# ForexFactory 事件名 -> 本站指标代码（FRED 代码；ISM 无 FRED 序列，用 ism_mfg / ism_svc）
FF_TITLE_MAP = {
    "CPI m/m": "CPIAUCSL",
    "Core CPI m/m": "CPILFESL",
    "PCE Price Index m/m": "PCEPI",
    "Core PCE Price Index m/m": "PCEPILFE",
    "PPI m/m": "PPIFIS",
    "Core PPI m/m": "PPIFES",
    "Non-Farm Employment Change": "PAYEMS",
    "Average Hourly Earnings m/m": "CES0500000003",
    "Unemployment Rate": "UNRATE",
    "Unemployment Claims": "ICSA",
    "ISM Manufacturing PMI": "ism_mfg",
    "ISM Services PMI": "ism_svc",
    "Retail Sales m/m": "RSAFS",
    "Advance GDP q/q": "A191RL1Q225SBEA",
    "Prelim GDP q/q": "A191RL1Q225SBEA",
    "Final GDP q/q": "A191RL1Q225SBEA",
    "JOLTS Job Openings": "JTSJOL",
}

# 前端以「万」为展示单位的指标：FF 的 K（千）/ M（百万）需换算成万
_FF_WAN_CODES = {"PAYEMS", "ICSA", "JTSJOL"}


def parse_ff_number(raw, code: str):
    """把 FF 的 forecast / previous 字符串解析成与前端 META 同口径的数值。

    格式示例：'0.3%' -> 0.3；'54.6' -> 54.6；'198K' -> 19.8（万人）；
    '182K' -> 18.2（万人）；'7.72M' -> 772.0（万个）。空串 / 非数字返回 None。
    """
    if raw is None:
        return None
    s = str(raw).strip().replace(",", "")
    if not s or s in ("-", "--"):
        return None
    if s.endswith("%"):
        s = s[:-1].strip()
    suffix = ""
    if s and s[-1].upper() in ("K", "M", "B"):
        suffix = s[-1].upper()
        s = s[:-1].strip()
    try:
        v = float(s)
    except ValueError:
        return None
    if code in _FF_WAN_CODES:
        if suffix == "K":
            v = v / 10.0
        elif suffix == "M":
            v = v * 100.0
        elif suffix == "B":
            v = v * 100000.0
        elif code == "JTSJOL" and abs(v) < 100:
            v = v * 100.0  # 无后缀的 JOLTS 裸数按百万口径（如 7.7 -> 770 万）
        elif code in ("PAYEMS", "ICSA") and abs(v) > 50:
            v = v / 10.0  # 无后缀的千人裸数（如 198000 不会出现，198 视为千人)
        return round(v, 1)
    return round(v, 2)


def fetch_forex_factory_expectations() -> tuple[dict, dict]:
    """抓本周 + 下周日历，只取 country == 'USD' 且标题命中映射表的事件。

    每个指标只保留「未来 15 天内最早一场未公布」的 forecast（已过去的事件预期已过期，
    不回填）；返回 (future_expectations 数值表, details 明细表)。
    """
    events: list[dict] = []
    for url in FF_CALENDAR_URLS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "MacroTerminalBot/1.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            if isinstance(payload, list):
                events.extend(payload)
                print(f"[OK] ForexFactory {url.rsplit('/', 1)[-1]} -> {len(payload)} events")
        except Exception as e:
            print(f"[WARN] ForexFactory fetch failed ({url}): {e}")

    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=15)
    best: dict[str, dict] = {}
    for ev in events:
        if not isinstance(ev, dict) or ev.get("country") != "USD":
            continue
        code = FF_TITLE_MAP.get(str(ev.get("title") or "").strip())
        if not code:
            continue
        forecast = parse_ff_number(ev.get("forecast"), code)
        if forecast is None:
            continue
        try:
            when = datetime.fromisoformat(str(ev.get("date")))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        if when < now or when > horizon:
            continue  # 已公布过的旧预期 / 超出两周窗口的远期事件都不写入
        item = {
            "forecast": forecast,
            "previous": parse_ff_number(ev.get("previous"), code),
            "date": ev.get("date"),
            "title": ev.get("title"),
            "impact": ev.get("impact"),
        }
        if code not in best or str(item["date"]) < str(best[code]["date"]):
            best[code] = item

    future = {code: item["forecast"] for code, item in best.items()}
    print(f"[OK] ForexFactory future expectations -> {future}")
    return future, best


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
        "expectations": {},
        "future_expectations": {},
        "future_expectation_details": {},
    }

    # 读取可选的手动覆写文件（用于存放 ISM PMI、已公布期市场预期与 CME FedWatch 概率）
    overrides_path = os.path.join(os.path.dirname(__file__), "overrides.json")
    if os.path.exists(overrides_path):
        with open(overrides_path, "r", encoding="utf-8") as f:
            output["manual_overrides"] = json.load(f)

    # ForexFactory 未来两周预期：自动抓取的最新一致预期同时写入 expectations 与
    # future_expectations（同源同值）；前端把它们只用于「未来一期预期」（日历），
    # 已公布期的 expected 仍由 manual_overrides 提供，两者互不覆盖。
    try:
        ff_future, ff_details = fetch_forex_factory_expectations()
        output["expectations"] = ff_future
        output["future_expectations"] = ff_future
        output["future_expectation_details"] = ff_details
    except Exception as e:
        print(f"[WARN] ForexFactory expectations failed: {e}")

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