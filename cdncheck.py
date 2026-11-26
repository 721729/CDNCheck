#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CDNCheck - 基于站长之家（chinaz.com）多地 Ping 的 CDN 检测工具

原理：
    1. 访问 https://ping.chinaz.com/<域名>，获取本次查询 token 以及节点列表；
    2. 连接站长之家 Ping WebSocket（wss://tooldata.chinaz.com/pingwebsocket），
       发送 {"keyword": 域名, "token": token}；
    3. 实时接收各节点 Ping 结果，查询每个 IP 的 ASN/归属（ip-api / ipwho.is），
       结合归属与地域分布判断 CDN，而不是只看 IP 数量：
         - 多地解析到同一 CDN 厂商地址段        -> CDN；
         - 各地解析到同一组自有 IP，组内轮询     -> DNS 轮询/负载均衡，非 CDN；
         - 不同地区解析到不同运营商自有地址段    -> 双线/多线机房，非 CDN；
         - 全球同一 IP：用多国 RTT 距离衰减 + ipinfo anycast 标记
           区分 Anycast 服务地址与单点真实 IP。

说明：
    站长之家已下线旧的 iframe.ashx + 固定 guid 节点接口（旧版写死的 35 个节点
    已全部失效），本脚本改用当前的 WebSocket 接口。节点由服务端动态下发，
    无需再维护节点 GUID，也不依赖固定节点列表。

依赖：
    pip install requests websocket-client
"""

import argparse
import json
import os
import re
import sys
import tempfile
import time
from urllib.parse import urlsplit

try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit("缺少依赖 requests，请先执行：pip install requests")

try:
    import websocket  # websocket-client
except ImportError:  # pragma: no cover
    sys.exit("缺少依赖 websocket-client，请先执行：pip install websocket-client")


banner = r'''
                                ,--.                          ,--,                         ,--. 
  ,----..     ,---,          ,--.'|          ,----..       ,--.'|   ,---,. ,----..    ,--/  /| 
 /   /   \  .'  .' `\    ,--,:  : |         /   /   \   ,--,  | : ,'  .' |/   /   \,---,': / ' 
|   :     ,---.'     \,`--.'`|  ' :        |   :     ,---.'|  : ,---.'   |   :     :   : '/ /  
.   |  ;. |   |  .`\  |   :  :  | |        .   |  ;. |   | : _' |   |   ..   |  ;. |   '   ,   
.   ; /--`:   : |  '  :   |   \ | :        .   ; /--`:   : |.'  :   :  |-.   ; /--`'   |  /    
;   | ;   |   ' '  ;  |   : '  '; |        ;   | ;   |   ' '  ; :   |  ;/;   | ;   |   ;  ;    
|   : |   '   | ;  .  '   ' ;.    ;        |   : |   '   |  .'. |   :   .|   : |   :   '   \   
.   | '___|   | :  |  |   | | \   |        .   | '___|   | :  | |   |  |-.   | '___|   |    '  
'   ; : .''   : | /  ;'   : |  ; .'        '   ; : .''   : |  : '   :  ;/'   ; : .''   : |.  \ 
'   | '/  |   | '` ,/ |   | '`--'          '   | '/  |   | '  ,/|   |    '   | '/  |   | '_\.' 
|   :    /;   :  .'   '   : |              |   :    /;   : ;--' |   :   .|   :    /'   : |     
 \   \ .' |   ,.'     ;   |.'               \   \ .' |   ,/     |   | ,'  \   \ .' ;   |,'     
  `---`   '---'       '---'                  `---`   '---'      `----'     `---`   '---'       
                                                                         --by721729
'''

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

PING_PAGE = "https://ping.chinaz.com/{host}"
PING_WS = "wss://tooldata.chinaz.com/pingwebsocket"

TOKEN_RE = re.compile(r'let\s+token\s*=\s*"([0-9a-fA-F]{16,})"')
TOKEN_FALLBACK_RE = re.compile(r'token\s*=\s*"([^"]{16,})"')
ROW_RE = re.compile(r"<tr\b([^>]*)>(.*?)</tr>", re.S)
ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')
TD_RE = re.compile(r"<td\b[^>]*>(.*?)</td>", re.S)
TAG_RE = re.compile(r"<[^>]+>")

# 大陆省级行政区（含简称），用于把节点划分为国内/海外
MAINLAND_PROVINCES = (
    "北京", "天津", "上海", "重庆",
    "河北", "山西", "辽宁", "吉林", "黑龙江",
    "江苏", "浙江", "安徽", "福建", "江西", "山东",
    "河南", "湖北", "湖南", "广东", "海南",
    "四川", "贵州", "云南", "陕西", "甘肃", "青海",
    "内蒙古", "内蒙", "广西", "西藏", "宁夏", "新疆",
)

# 专用 CDN 厂商关键字（命中即视为 CDN 地址段）
CDN_VENDOR_KEYWORDS = (
    ("cloudflare", "Cloudflare"),
    ("akamai", "Akamai"),
    ("fastly", "Fastly"),
    ("cloudfront", "AWS CloudFront"),
    ("imperva", "Imperva"),
    ("incapsula", "Imperva"),
    ("sucuri", "Sucuri"),
    ("stackpath", "StackPath"),
    ("bunny", "Bunny CDN"),
    ("keycdn", "KeyCDN"),
    ("gcore", "Gcore"),
    ("ddos-guard", "DDoS-Guard"),
    ("limelight", "Limelight"),
    ("edgecast", "Edgecast"),
    ("cdn77", "CDN77"),
    ("cachefly", "CacheFly"),
    ("medianova", "Medianova"),
    ("beluga", "BelugaCDN"),
    ("wangsu", "网宿"),
    ("chinanetcenter", "网宿"),
    ("chinacache", "蓝汛"),
    ("baishan", "白山云"),
    ("qiniu", "七牛云"),
    ("upyun", "又拍云"),
)

# 云厂商关键字（可能承载 CDN，也可能只是云主机，需结合地域分布判断）
CLOUD_VENDOR_KEYWORDS = (
    ("alibaba", "阿里云"),
    ("aliyun", "阿里云"),
    ("alibabacloud", "阿里云"),
    ("tencent", "腾讯云"),
    ("qcloud", "腾讯云"),
    ("baidu", "百度云"),
    ("huawei", "华为云"),
    ("kingsoft", "金山云"),
    ("ksyun", "金山云"),
    ("ucloud", "UCloud"),
    ("volcengine", "火山引擎"),
    ("bytedance", "火山引擎"),
    ("amazon", "AWS"),
    ("aws", "AWS"),
    ("microsoft", "Azure"),
    ("azure", "Azure"),
    ("google", "Google Cloud"),
)

# 基础运营商关键字
ISP_KEYWORDS = (
    ("电信", "中国电信"),
    ("china telecom", "中国电信"),
    ("chinanet", "中国电信"),
    ("联通", "中国联通"),
    ("china unicom", "中国联通"),
    ("china169", "中国联通"),
    ("unicom", "中国联通"),
    ("移动", "中国移动"),
    ("china mobile", "中国移动"),
    ("cmnet", "中国移动"),
    ("chinamobile", "中国移动"),
    ("教育网", "教育网"),
    ("cernet", "教育网"),
    ("cstnet", "科技网"),
    ("academy of sciences", "科技网"),
    ("cnnic", "CNNIC"),
    ("铁通", "中国铁通"),
    ("广电", "广电"),
)

# ASN 兜底分类（关键字匹配不到时使用）
ASN_MAP = {
    13335: ("cdn", "Cloudflare"),
    20940: ("cdn", "Akamai"),
    16625: ("cdn", "Akamai"),
    21342: ("cdn", "Akamai"),
    32787: ("cdn", "Akamai"),
    54113: ("cdn", "Fastly"),
    16509: ("cloud", "AWS"),
    14618: ("cloud", "AWS"),
    15169: ("cloud", "Google Cloud"),
    396982: ("cloud", "Google Cloud"),
    8075: ("cloud", "Azure"),
    8068: ("cloud", "Azure"),
    37963: ("cloud", "阿里云"),
    45102: ("cloud", "阿里云"),
    132203: ("cloud", "腾讯云"),
    45090: ("cloud", "腾讯云"),
    55967: ("cloud", "百度云"),
    38365: ("cloud", "百度云"),
    136907: ("cloud", "华为云"),
    55990: ("cloud", "华为云"),
    4134: ("isp", "中国电信"),
    4809: ("isp", "中国电信"),
    4812: ("isp", "中国电信"),
    23724: ("isp", "中国电信"),
    4837: ("isp", "中国联通"),
    4808: ("isp", "中国联通"),
    9929: ("isp", "中国联通"),
    9808: ("isp", "中国移动"),
    56040: ("isp", "中国移动"),
    56041: ("isp", "中国移动"),
    4538: ("isp", "教育网"),
}

OWNER_CACHE_PATH = os.path.join(tempfile.gettempdir(), "cdncheck_ip_owner_cache.json")
_OWNER_CACHE = None
_LAST_LOOKUP_TS = 0.0


class CheckError(Exception):
    """检测过程中的可预期错误。"""


def normalize_target(target):
    """把用户输入统一成主机名（去协议、路径、端口）。"""
    target = (target or "").strip()
    if not target:
        return ""
    if "://" not in target:
        target = "http://" + target
    host = urlsplit(target).hostname or ""
    return host.strip(".").lower()


def is_mainland(province, address=""):
    """根据页面节点省份判断是否为大陆节点，兜底使用结果里的地址。"""
    text = province or address or ""
    return any(name in text for name in MAINLAND_PROVINCES)


def build_session():
    session = requests.Session()
    session.headers.update({
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": "https://ping.chinaz.com/",
    })
    return session


def parse_nodes(html):
    """解析页面中的节点表格：guid -> {name, province, linetype}。"""
    nodes = {}
    for attrs, body in ROW_RE.findall(html):
        info = dict(ATTR_RE.findall(attrs))
        guid = info.get("id", "")
        if not re.fullmatch(r"[0-9A-Fa-f-]{36}", guid):
            continue
        cells = TD_RE.findall(body)
        name = TAG_RE.sub("", cells[0]).strip() if cells else ""
        province = (info.get("province") or "").strip()
        if not province and "[" in name:
            province = name.split("[")[0][:4]
        try:
            linetype = int(info.get("linetype") or 0)
        except ValueError:
            linetype = 0
        nodes[guid.upper()] = {
            "name": name or province or guid,
            "province": province,
            "linetype": linetype,
        }
    return nodes


def fetch_page(session, host, timeout):
    """获取 token 和节点列表。"""
    url = PING_PAGE.format(host=host)
    try:
        resp = session.get(url, timeout=timeout)
    except requests.exceptions.RequestException as exc:
        raise CheckError("请求站长之家失败：{}".format(exc)) from exc

    if resp.status_code != 200:
        raise CheckError("站长之家返回 HTTP {}".format(resp.status_code))
    if len(resp.text) < 5000 and ("403" in resp.text or "禁止" in resp.text):
        raise CheckError("站长之家拒绝访问（可能触发了频率限制）")

    match = TOKEN_RE.search(resp.text) or TOKEN_FALLBACK_RE.search(resp.text)
    if not match:
        raise CheckError("站长之家未返回检测任务（域名可能无效/无解析记录，"
                         "或页面结构已调整）")
    return match.group(1), parse_nodes(resp.text)


def collect_results(host, timeout):
    """通过 WebSocket 收集各地 Ping 结果。"""
    session = build_session()
    try:
        token, nodes = fetch_page(session, host, timeout)
    finally:
        session.close()

    try:
        ws = websocket.create_connection(
            PING_WS,
            timeout=timeout,
            origin="https://ping.chinaz.com",
            header=["User-Agent: " + UA],
            enable_multithread=False,
        )
    except Exception as exc:  # noqa: BLE001
        raise CheckError("连接站长之家 Ping 服务失败：{}".format(exc)) from exc

    rows = []
    failed = 0
    try:
        ws.send(json.dumps({"keyword": host, "token": token}))
        deadline = time.time() + timeout
        while True:
            remain = deadline - time.time()
            if remain <= 0:
                break
            ws.settimeout(remain)
            try:
                message = ws.recv()
            except (websocket.WebSocketTimeoutException,
                    websocket.WebSocketConnectionClosedException):
                break
            if not message:
                break
            try:
                data = json.loads(message)
            except (ValueError, TypeError):
                continue

            code = data.get("code")
            if code == 10002:          # 服务端发送的结束标记
                break
            if code != 1:
                continue
            if not data.get("ip"):
                failed += 1
                continue

            node = nodes.get(str(data.get("guid", "")).upper(), {})
            data["name"] = node.get("name") or data.get("address", "?")
            data["province"] = node.get("province", "")
            data["mainland"] = is_mainland(data["province"], data.get("address", ""))
            rows.append(data)
    finally:
        try:
            ws.close()
        except Exception:
            pass
    return rows, failed


# ---------------------------------------------------------------------------
# IP 归属查询
# ---------------------------------------------------------------------------

def _load_owner_cache():
    global _OWNER_CACHE
    if _OWNER_CACHE is not None:
        return _OWNER_CACHE
    try:
        with open(OWNER_CACHE_PATH, "r", encoding="utf-8") as file:
            data = json.load(file)
            _OWNER_CACHE = data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        _OWNER_CACHE = {}
    return _OWNER_CACHE


def _save_owner_cache(cache):
    try:
        with open(OWNER_CACHE_PATH, "w", encoding="utf-8") as file:
            json.dump(cache, file, ensure_ascii=False)
    except OSError:
        pass


def _parse_asn(as_field):
    match = re.search(r"AS(\d+)", str(as_field or ""), re.I)
    return int(match.group(1)) if match else 0


def _make_owner(asn, asname, isp, org, country, hosting, category, vendor):
    return {
        "asn": asn,
        "asname": asname,
        "isp": isp,
        "org": org,
        "country": country,
        "hosting": bool(hosting),
        "category": category,
        "vendor": vendor,
    }


def classify_owner(raw):
    """根据 ASN/ISP/机构名判断 IP 归属类别。"""
    asn = int(raw.get("asn") or 0)
    asname = str(raw.get("asname") or "")
    isp = str(raw.get("isp") or "")
    org = str(raw.get("org") or "")
    country = str(raw.get("country") or "")
    hosting = bool(raw.get("hosting"))
    text = "{} {} {}".format(asname, isp, org).lower()

    for keyword, vendor in CDN_VENDOR_KEYWORDS:
        if keyword in text:
            return _make_owner(asn, asname, isp, org, country, hosting, "cdn", vendor)
    for keyword, vendor in CLOUD_VENDOR_KEYWORDS:
        if keyword in text:
            return _make_owner(asn, asname, isp, org, country, hosting, "cloud", vendor)
    for keyword, vendor in ISP_KEYWORDS:
        if keyword in text:
            return _make_owner(asn, asname, isp, org, country, hosting, "isp", vendor)

    if asn in ASN_MAP:
        category, vendor = ASN_MAP[asn]
        return _make_owner(asn, asname, isp, org, country, hosting, category, vendor)

    if hosting:
        return _make_owner(asn, asname, isp, org, country, hosting, "idc", "IDC/托管")
    return _make_owner(asn, asname, isp, org, country, hosting, "unknown", "未知")


def _query_ip_api(ips, timeout):
    """ip-api.com 批量查询（HTTP，免费，15 次/分钟）。"""
    session = requests.Session()
    session.trust_env = False
    fields = ("status,message,country,regionName,city,isp,org,as,asname,"
              "hosting,query")
    result = {}
    try:
        for start in range(0, len(ips), 100):
            chunk = ips[start:start + 100]
            global _LAST_LOOKUP_TS
            wait = 4.2 - (time.time() - _LAST_LOOKUP_TS)
            if wait > 0:
                time.sleep(wait)
            resp = session.post(
                "http://ip-api.com/batch?fields=" + fields,
                json=chunk,
                timeout=timeout,
            )
            _LAST_LOOKUP_TS = time.time()
            resp.raise_for_status()
            for entry in resp.json():
                if entry.get("status") != "success":
                    continue
                result[entry.get("query")] = {
                    "asn": _parse_asn(entry.get("as")),
                    "asname": entry.get("asname") or "",
                    "isp": entry.get("isp") or "",
                    "org": entry.get("org") or "",
                    "country": entry.get("country") or "",
                    "hosting": bool(entry.get("hosting")),
                }
    finally:
        session.close()
    return result


def _query_ipwhois(ips, timeout):
    """ipwho.is 逐个查询（HTTPS，免费额度，作为兜底）。"""
    session = requests.Session()
    session.headers.update({"User-Agent": UA})
    result = {}
    for ip in ips:
        try:
            resp = session.get("https://ipwho.is/" + ip, timeout=timeout)
            data = resp.json()
            if data.get("success"):
                conn = data.get("connection") or {}
                result[ip] = {
                    "asn": int(conn.get("asn") or 0),
                    "asname": conn.get("org") or "",
                    "isp": conn.get("isp") or "",
                    "org": conn.get("org") or "",
                    "country": data.get("country") or "",
                    "hosting": False,
                }
        except Exception:  # noqa: BLE001
            continue
        time.sleep(0.15)
    session.close()
    return result


def lookup_owners(ips, timeout=10):
    """查询并缓存 IP 归属，返回 {ip: owner}。"""
    cache = _load_owner_cache()
    missing = [ip for ip in ips if ip not in cache]
    if missing:
        fetched = {}
        try:
            fetched = _query_ip_api(missing, timeout)
        except Exception:  # noqa: BLE001
            fetched = {}
        if not fetched:
            try:
                fetched = _query_ipwhois(missing, timeout)
            except Exception:  # noqa: BLE001
                fetched = {}
        if fetched:
            cache.update(fetched)
            _save_owner_cache(cache)

    owners = {}
    for ip in ips:
        raw = cache.get(ip)
        if raw:
            owners[ip] = classify_owner(raw)
    return owners


def query_anycast_flag(ip, timeout=5):
    """查询 ipinfo.io 的 anycast 标记：True/False/None（未知）。"""
    cache = _load_owner_cache()
    raw = cache.get(ip) or {}
    if "anycast" in raw:
        return raw["anycast"]

    try:
        resp = requests.get(
            "https://ipinfo.io/{}/json".format(ip),
            headers={"User-Agent": UA},
            timeout=timeout,
        )
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data.get("anycast"), bool):
                raw = dict(raw)
                raw["anycast"] = data["anycast"]
                cache[ip] = raw
                _save_owner_cache(cache)
                return data["anycast"]
    except Exception:  # noqa: BLE001
        pass
    return None


def rtt_stats(rows, ip):
    """统计单个 IP 在各节点的 RTT 分布。"""
    domestic, overseas = [], []
    country_min = {}
    for row in rows:
        if row.get("ip") != ip:
            continue
        try:
            rtt = int(str(row.get("timeMs", "")).strip())
        except (TypeError, ValueError):
            continue
        if row["mainland"]:
            domestic.append(rtt)
        else:
            overseas.append(rtt)
            country = row.get("province") or "海外"
            if country not in country_min or rtt < country_min[country]:
                country_min[country] = rtt
    return {
        "domestic_min": min(domestic) if domestic else None,
        "domestic_avg": round(sum(domestic) / len(domestic), 1) if domestic else None,
        "overseas_min": min(overseas) if overseas else None,
        "overseas_avg": round(sum(overseas) / len(overseas), 1) if overseas else None,
        "country_min": country_min,
        "low_countries": sorted(c for c, rtt in country_min.items() if rtt <= 50),
        "samples": len(domestic) + len(overseas),
    }


def guess_anycast(stats, flag=None):
    """结合 ipinfo 标记与 RTT 分布判断是否 Anycast。

    返回 (is_anycast, reason)：
        - ipinfo 标记为 True -> Anycast；
        - 多个海外国家 RTT 同时很低（<=50ms）-> 单台主机不可能同时离多国这么近，判 Anycast；
        - 海外最低 RTT 明显高于国内且随距离递增 -> 单点 unicast；
        - 其余 -> 无法判断。
    """
    if flag is True:
        return True, "ipinfo 将其标记为 Anycast"
    low = stats.get("low_countries") or []
    if len(low) >= 3:
        return True, "多个海外国家 RTT 均 ≤50ms（{}）".format("、".join(low))
    domestic_min = stats.get("domestic_min")
    country_min = stats.get("country_min") or {}
    far_min = max(country_min.values()) if country_min else None
    if domestic_min is not None and far_min is not None:
        if far_min >= 150 and domestic_min <= 80:
            return False, "海外最远节点 RTT {}ms 明显高于国内 {}ms，符合单点机房".format(
                far_min, domestic_min)
    if flag is False:
        return False, "ipinfo 未标记 Anycast"
    return None, ""


# ---------------------------------------------------------------------------
# 判定
# ---------------------------------------------------------------------------

def _region_sets(rows):
    domestic = [row for row in rows if row["mainland"]]
    overseas = [row for row in rows if not row["mainland"]]
    domestic_regions = {row.get("province", "") for row in domestic if row.get("province")}
    overseas_regions = {row.get("province", "") for row in overseas if row.get("province")}
    return domestic, overseas, domestic_regions, overseas_regions


def _owner_brief(owner):
    name = owner.get("vendor") or owner.get("asname") or owner.get("isp") or "未知"
    asn = owner.get("asn")
    return "{}（AS{}）".format(name, asn) if asn else name


def _count_rules(host, domestic_ips, overseas_ips):
    """旧的数量判定，仅在 IP 归属完全查不到时兜底。"""
    d, o = len(domestic_ips), len(overseas_ips)
    real_ips = []
    if d > 1 and o == 1:
        verdict = "cdn"
        real_ips = overseas_ips
        message = ("[-] 经检测 {} 国内有多个 IP、海外仅 1 个 IP，"
                   "海外节点 IP 可能为真实 IP：{}").format(host, "、".join(overseas_ips))
    elif o > 1 and d == 1:
        verdict = "cdn"
        real_ips = domestic_ips
        message = ("[-] 经检测 {} 海外有多个 IP、国内仅 1 个 IP，"
                   "国内节点 IP 可能为真实 IP：{}").format(host, "、".join(domestic_ips))
    elif d > 1 or o > 1:
        verdict = "cdn"
        message = ("[-] 经检测 {} 解析到多个 IP（国内 {} / 海外 {}），"
                   "请进一步判断真实 IP").format(host, d, o)
    elif d == 1 and o == 1 and domestic_ips == overseas_ips:
        verdict = "direct"
        real_ips = domestic_ips
        message = "[+] 经检测 {} 解析到同一 IP：{}".format(host, "、".join(domestic_ips))
    elif d == 1 and o == 1:
        verdict = "cdn"
        message = ("[-] 经检测 {} 国内外解析到不同 IP（国内 {} / 海外 {}），"
                   "请进一步判断").format(host, domestic_ips[0], overseas_ips[0])
    elif d == 1:
        verdict = "direct"
        real_ips = domestic_ips
        message = "[+] 经检测 {} 国内解析到单一 IP：{}".format(host, "、".join(domestic_ips))
    elif o == 1:
        verdict = "direct"
        real_ips = overseas_ips
        message = "[+] 经检测 {} 海外解析到单一 IP：{}".format(host, "、".join(overseas_ips))
    else:
        verdict = "unknown"
        message = "[-] {} 节点结果异常，无法判断".format(host)
    return verdict, message, real_ips


def _owner_rules(host, owners, ips, domestic_ips, overseas_ips,
                 domestic_regions, overseas_regions, rtt_profiles=None,
                 anycast_flags=None):
    """基于 IP 归属 + 地域分布 + RTT 分布的判定。"""
    rtt_profiles = rtt_profiles or {}
    anycast_flags = anycast_flags or {}
    cats = {ip: owners[ip]["category"] for ip in ips if ip in owners}
    cdn_ips = [ip for ip in ips if cats.get(ip) == "cdn"]
    cloud_ips = [ip for ip in ips if cats.get(ip) == "cloud"]
    isp_ips = [ip for ip in ips if cats.get(ip) == "isp"]
    idc_ips = [ip for ip in ips if cats.get(ip) == "idc"]
    other_ips = [ip for ip in ips
                 if cats.get(ip) not in ("cdn", "cloud", "isp", "idc")]

    vendors = {
        ip: (owners[ip].get("vendor") or owners[ip].get("asname")
             or owners[ip].get("isp") or "未知")
        for ip in ips if ip in owners
    }
    domestic_count = len(domestic_regions)
    overseas_count = len(overseas_regions)

    def brief(ip):
        owner = owners.get(ip)
        return _owner_brief(owner) if owner else "未知归属"

    # 1) 命中专用 CDN 厂商地址段
    if cdn_ips:
        verdict = "cdn"
        vendor_desc = "、".join(sorted({vendors[ip] for ip in cdn_ips}))
        message = "[-] 经检测 {} 使用了 CDN：IP 归属 CDN 厂商（{}），共 {} 个边缘 IP".format(
            host, vendor_desc, len(cdn_ips))
        other = [ip for ip in ips if ip not in cdn_ips]
        if other:
            message += "；其余 {} 个 IP 归属 {}".format(
                len(other), "、".join(sorted({brief(ip) for ip in other})))
        real_ips = []
        for side_ips, side_name in ((domestic_ips, "国内"), (overseas_ips, "海外")):
            if len(side_ips) == 1 and side_ips[0] not in cdn_ips:
                ip = side_ips[0]
                if cats.get(ip) in ("isp", "idc", "cloud"):
                    real_ips = side_ips
                    message += "；{}节点仅有该 1 个非 CDN 归属 IP，可能为真实 IP：{}".format(
                        side_name, brief(ip))
                    break
        return verdict, message, real_ips

    # 2) 全部是云厂商地址段：多省多 IP 才按 CDN 处理
    if cloud_ips and len(cloud_ips) == len(ips):
        uniq_vendors = sorted({vendors[ip] for ip in cloud_ips})
        if len(ips) == 1:
            ip = ips[0]
            anycast, reason = guess_anycast(
                rtt_profiles.get(ip, {}), anycast_flags.get(ip))
            real_ips = []
            if anycast:
                verdict = "anycast"
                message = ("[?] 经检测 {} 所有节点解析到同一 IP {}（归属 {}）；{}，"
                           "判定为 Anycast/就近接入地址，不是源站真实 IP").format(
                               host, ip, brief(ip), reason)
            else:
                verdict = "direct"
                real_ips = ips
                message = ("[+] 经检测 {} 未使用 CDN：所有节点解析到同一 IP {}，"
                           "归属 {}；{}，该 IP 即真实 IP").format(
                               host, ip, brief(ip), reason or "RTT 分布无异常")
            return verdict, message, real_ips
        if len(uniq_vendors) == 1 and domestic_count >= 3:
            verdict = "cdn"
            message = ("[-] 经检测 {} 使用 CDN：{} 个 IP 覆盖 {} 个省份，"
                       "全部归属同一云厂商（{}），符合 CDN 特征").format(
                           host, len(ips), domestic_count, uniq_vendors[0])
            return verdict, message, []
        if len(uniq_vendors) > 1 and (domestic_count >= 3 or overseas_count >= 2):
            verdict = "cdn"
            message = ("[-] 经检测 {} 疑似使用 CDN：多个 IP 分属不同云厂商（{}），"
                       "且跨多地区").format(host, "、".join(uniq_vendors))
            return verdict, message, []
        verdict = "round-robin"
        real_ips = ips
        message = ("[+] 经检测 {} 未使用 CDN：{} 个 IP 均归属 {}，"
                   "集中在 {} 个地区，属于 DNS 轮询/负载均衡，不是 CDN").format(
                       host, len(ips), "、".join(uniq_vendors),
                       max(domestic_count, overseas_count))
        return verdict, message, real_ips

    # 3) 全是运营商/IDC 自有地址段（含归属未知，未知按自有线路处理）
    if ips and len(isp_ips) + len(idc_ips) + len(other_ips) == len(ips):
        uniq_vendors = sorted({vendors.get(ip, "未知归属") for ip in ips})
        if len(ips) == 1:
            ip = ips[0]
            anycast, reason = guess_anycast(
                rtt_profiles.get(ip, {}), anycast_flags.get(ip))
            real_ips = []
            if anycast:
                verdict = "anycast"
                message = ("[?] 经检测 {} 所有节点解析到同一 IP {}（归属 {}）；{}，"
                           "判定为 Anycast/就近接入地址，不是源站真实 IP").format(
                               host, ip, brief(ip), reason)
            else:
                verdict = "direct"
                real_ips = ips
                message = ("[+] 经检测 {} 未使用 CDN：所有节点解析到同一 IP {}，"
                           "归属 {}；{}，该 IP 即真实 IP").format(
                               host, ip, brief(ip), reason or "RTT 分布无异常")
            return verdict, message, real_ips
        if len(uniq_vendors) == 1:
            verdict = "round-robin"
            real_ips = ips
            message = ("[+] 经检测 {} 未使用 CDN：{} 个 IP 全部归属 {}，"
                       "是同一线路内的轮询/负载均衡，不是 CDN").format(
                           host, len(ips), uniq_vendors[0])
            if domestic_count + overseas_count >= 5:
                message += "；同一组 IP 在多地区可达，更接近 Anycast/就近接入"
            return verdict, message, real_ips
        verdict = "dual-line"
        real_ips = ips
        message = ("[+] 经检测 {} 未使用 CDN：不同地区解析到不同 IP，"
                   "分别归属 {}，属于双线/多线机房或多机房部署，不是 CDN").format(
                       host, "、".join(uniq_vendors))
        return verdict, message, real_ips

    # 4) 混合归属：CDN/云 + 运营商/IDC/未知
    if cloud_ips and (isp_ips or idc_ips or other_ips):
        uniq_vendors = sorted({vendors.get(ip, "未知归属") for ip in ips})
        if len(cloud_ips) >= 2 and domestic_count >= 3:
            verdict = "cdn"
            message = ("[-] 经检测 {} 疑似使用 CDN：部分 IP 归属云厂商（{}），"
                       "且跨 {} 个省份").format(
                           host, "、".join(sorted({vendors[ip] for ip in cloud_ips})),
                           domestic_count)
            return verdict, message, []
        verdict = "multi-line"
        message = ("[+] 经检测 {} 未发现典型 CDN：IP 混合归属 {}，"
                   "更像多线/多机房部署").format(host, "、".join(uniq_vendors))
        return verdict, message, ips

    verdict = "unknown"
    message = ("[-] {} 的 IP 归属无法可靠归类，无法判断是否使用 CDN").format(host)
    return verdict, message, []


def analyze(host, rows, failed=0, owners=None, anycast_flags=None):
    """根据节点结果判定 CDN 并给出可能的真实 IP。"""
    domestic, overseas, domestic_regions, overseas_regions = _region_sets(rows)
    domestic_ips = sorted({row["ip"] for row in domestic})
    overseas_ips = sorted({row["ip"] for row in overseas})
    all_ips = sorted(set(domestic_ips) | set(overseas_ips))
    anycast_flags = anycast_flags or {}
    rtt_profiles = {ip: rtt_stats(rows, ip) for ip in all_ips}

    summary = [
        "[+] 收到 {} 个节点结果（国内 {} / 海外 {}），失败 {} 个".format(
            len(rows), len(domestic), len(overseas), failed),
        "[+] 国内节点 IP：{}".format("、".join(domestic_ips) if domestic_ips else "无"),
        "[+] 海外节点 IP：{}".format("、".join(overseas_ips) if overseas_ips else "无"),
    ]

    owners = owners or {}
    known = {ip: owners[ip] for ip in all_ips if ip in owners}
    partial = 0 < len(known) < len(all_ips)

    if not known:
        verdict, message, real_ips = _count_rules(host, domestic_ips, overseas_ips)
        message = "（IP 归属查询失败，仅按 IP 数量粗略判断）" + message
    else:
        rule_ips = all_ips if not partial else sorted(known)
        verdict, message, real_ips = _owner_rules(
            host, known, rule_ips, domestic_ips, overseas_ips,
            domestic_regions, overseas_regions, rtt_profiles, anycast_flags)
        if partial:
            message += "（{} 个 IP 归属未知，结论仅供参考）".format(
                len(all_ips) - len(known))

    return {
        "domain": host,
        "verdict": verdict,
        "message": message,
        "summary": summary,
        "domestic_ips": domestic_ips,
        "overseas_ips": overseas_ips,
        "real_ips": real_ips,
        "owners": {ip: owners[ip] for ip in all_ips if ip in owners},
        "rtt": rtt_profiles,
        "anycast": {ip: anycast_flags[ip] for ip in all_ips if ip in anycast_flags},
        "nodes": len(rows),
        "failed": failed,
    }


def ping(domain, timeout=45, verbose=False):
    """检测单个域名，返回结果字典；无法检测时返回 None。"""
    host = normalize_target(domain)
    if not host:
        print("[-] 无法识别的域名/IP：{}".format(domain))
        return None

    print("[+] {} 域名正在进行 CDN 检测".format(host))

    rows, failed = [], 0
    last_error = None
    for attempt in range(2):
        try:
            rows, failed = collect_results(host, timeout)
            if rows:
                break
            last_error = CheckError("未收到任何节点结果")
        except Exception as exc:  # noqa: BLE001
            last_error = exc
        if attempt == 0:
            time.sleep(2)

    if not rows:
        print("[-] {} 检测失败：{}".format(host, last_error))
        print("    请检查网络、域名是否有效，或稍后重试")
        return None

    ips = sorted({row["ip"] for row in rows})
    owners = lookup_owners(ips, timeout=min(15, timeout))

    anycast_flags = {}
    if len(ips) == 1:
        owner = owners.get(ips[0])
        if not owner or owner.get("category") != "cdn":
            anycast_flags[ips[0]] = query_anycast_flag(ips[0])

    if verbose:
        for row in rows:
            where = "国内" if row["mainland"] else "海外"
            print("    [{}] {:<22} {:<16} {}ms".format(
                where, row["name"], row["ip"], row.get("timeMs", "?")))
        for ip in ips:
            owner = owners.get(ip)
            if owner:
                print("    [归属] {:<16} {}  {}".format(
                    ip, _owner_brief(owner), owner.get("isp") or ""))
            else:
                print("    [归属] {:<16} 查询失败".format(ip))
        if len(ips) == 1:
            stats = rtt_stats(rows, ips[0])
            print("    [RTT] 国内最低 {}ms / 海外最低 {}ms；海外各国最低：{}".format(
                stats["domestic_min"], stats["overseas_min"],
                "、".join("{} {}ms".format(k, v)
                         for k, v in sorted(stats["country_min"].items())) or "无"))
            if ips[0] in anycast_flags:
                print("    [Anycast] ipinfo 标记：{}".format(
                    anycast_flags[ips[0]]))

    result = analyze(host, rows, failed, owners, anycast_flags)
    for line in result["summary"]:
        print(line)
    print(result["message"])
    return result


def write_result(message):
    try:
        with open("result.txt", "a+", encoding="utf-8") as file:
            file.write("[{}] {}\n".format(time.strftime("%Y-%m-%d %H:%M:%S"), message))
    except OSError as exc:
        print("[-] 写入 result.txt 失败：{}".format(exc))


def main():
    parser = argparse.ArgumentParser(
        prog="cdncheck.py",
        add_help=False,
        description="基于站长之家多地 Ping 的 CDN 检测工具",
    )
    parser.add_argument("-h", "--help", action="store_true", help="显示帮助信息")
    parser.add_argument("-u", "--url", metavar="DOMAIN", help="检测单个域名")
    parser.add_argument("-f", "--filename", metavar="FILE", help="从文件批量检测域名")
    parser.add_argument("-t", "--timeout", type=int, default=45,
                        help="单域名检测超时秒数（默认 45）")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="显示每个节点的结果和 IP 归属")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出检测结果")

    try:
        args = parser.parse_args()
    except SystemExit:
        print(banner)
        print("参数错误，请使用 -h 或 --help 参数查看帮助")
        return 2

    if args.help or (not args.url and not args.filename):
        print(banner)
        print("对单个域名进行 CDN 检测：")
        print("    python3 cdncheck.py -u domain")
        print("    python3 cdncheck.py --url domain\n")
        print("对多个域名进行 CDN 检测：")
        print("    python3 cdncheck.py -f url.txt")
        print("    python3 cdncheck.py --filename url.txt\n")
        print("可选参数：")
        print("    -t/--timeout   单域名超时时间（秒，默认 45）")
        print("    -v/--verbose   显示每个节点的结果和 IP 归属")
        print("    --json         以 JSON 输出结果\n")
        print("判定依据：IP 的 ASN/归属 + 地域分布，而不是单纯数 IP。")
        print("示例：")
        print("    python3 cdncheck.py -u www.example.com")
        print("    python3 cdncheck.py -f urls.txt -v --json")
        return 0

    if args.timeout <= 0:
        print("[-] 超时时间必须大于 0")
        return 2

    domains = []
    if args.url:
        domains.append(args.url)
    if args.filename:
        try:
            with open(args.filename, "r", encoding="utf-8", errors="ignore") as file:
                for line in file:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        domains.append(line)
        except OSError as exc:
            print("[-] 读取文件失败：{}".format(exc))
            return 2

    if not args.filename:
        print(banner)

    results = []
    for domain in domains:
        result = ping(domain, timeout=args.timeout, verbose=args.verbose)
        if result is None:
            continue
        results.append(result)
        write_result(result["message"])

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))

    return 0 if results else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[-] 用户中断")
        sys.exit(130)
