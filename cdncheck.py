#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CDNCheck - 基于站长之家（chinaz.com）多地 Ping 的 CDN 检测工具

原理：
    1. 访问 https://ping.chinaz.com/<域名>，获取本次查询 token 以及节点列表；
    2. 连接站长之家 Ping WebSocket（wss://tooldata.chinaz.com/pingwebsocket），
       发送 {"keyword": 域名, "token": token}；
    3. 实时接收各节点 Ping 结果，汇总国内外解析 IP：IP 分散则判定使用了 CDN。

说明：
    站长之家已下线旧的 iframe.ashx + 固定 guid 节点接口（旧版写死的 35 个节点
    已全部失效），本脚本改用当前的 WebSocket 接口。节点由服务端动态下发，
    无需再维护节点 GUID，也不依赖固定节点列表。

依赖：
    pip install requests websocket-client
"""

import argparse
import json
import re
import sys
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
                                                                         --by 想走安全的小白
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

    if verbose:
        for row in rows:
            where = "国内" if row["mainland"] else "海外"
            print("    [{}] {:<22} {:<16} {}ms".format(
                where, row["name"], row["ip"], row.get("timeMs", "?")))

    result = analyze(host, rows, failed)
    for line in result["summary"]:
        print(line)
    print(result["message"])
    return result


def analyze(host, rows, failed=0):
    """根据节点结果判定 CDN 并给出可能的真实 IP。

    判定规则（与 README 一致）：
        - 国内有 CDN、海外没有  -> 海外节点解析到的 IP 即可能的真实 IP；
        - 海外有 CDN、国内没有  -> 国内节点解析到的 IP 即可能的真实 IP；
        - 两侧都有多个 IP        -> 两侧都有 CDN，无法直接定位真实 IP；
        - 两侧都只有 1 个相同 IP -> 未使用 CDN，该 IP 即真实 IP。
    """
    domestic = [row for row in rows if row["mainland"]]
    overseas = [row for row in rows if not row["mainland"]]
    domestic_ips = sorted({row["ip"] for row in domestic})
    overseas_ips = sorted({row["ip"] for row in overseas})
    d, o = len(domestic_ips), len(overseas_ips)

    summary = [
        "[+] 收到 {} 个节点结果（国内 {} / 海外 {}），失败 {} 个".format(
            len(rows), len(domestic), len(overseas), failed),
        "[+] 国内节点 IP：{}".format("、".join(domestic_ips) if domestic_ips else "无"),
        "[+] 海外节点 IP：{}".format("、".join(overseas_ips) if overseas_ips else "无"),
    ]

    real_ips = []
    if d > 1 and o == 1:
        verdict = "cdn"
        real_ips = overseas_ips
        message = ("[-] 经检测 {} 国内有 CDN、海外未使用 CDN（国内 {} 个不同 IP），"
                   "可通过海外节点判断真实 IP：{}").format(
                       host, d, "、".join(overseas_ips))
    elif o > 1 and d == 1:
        verdict = "cdn"
        real_ips = domestic_ips
        message = ("[-] 经检测 {} 海外有 CDN、国内未使用 CDN（海外 {} 个不同 IP），"
                   "可通过国内节点判断真实 IP：{}").format(
                       host, o, "、".join(domestic_ips))
    elif d > 1 or o > 1:
        verdict = "cdn"
        message = ("[-] 经检测 {} 可能使用 CDN 加速（国内 {} 个不同 IP / "
                   "海外 {} 个不同 IP），请进一步判断真实 IP").format(host, d, o)
    elif d == 1 and o == 1:
        if domestic_ips == overseas_ips:
            verdict = "direct"
            real_ips = domestic_ips
            message = "[+] 经检测 {} 国内外均未使用 CDN 加速，真实 IP 为：{}".format(
                host, "、".join(domestic_ips))
        else:
            verdict = "cdn"
            message = ("[-] 经检测 {} 国内外解析到不同 IP（国内 {} / 海外 {}），"
                       "疑似 CDN，请进一步判断真实 IP").format(
                           host, domestic_ips[0], overseas_ips[0])
    elif d == 1:
        verdict = "direct"
        real_ips = domestic_ips
        message = ("[+] 经检测 {} 国内未使用 CDN 加速（海外节点无结果），"
                   "可能的真实 IP 为：{}").format(host, "、".join(domestic_ips))
    elif o == 1:
        verdict = "direct"
        real_ips = overseas_ips
        message = ("[+] 经检测 {} 海外未使用 CDN 加速（国内节点无结果），"
                   "可能的真实 IP 为：{}").format(host, "、".join(overseas_ips))
    else:
        verdict = "unknown"
        message = "[-] {} 节点结果异常，无法判断是否使用 CDN".format(host)

    return {
        "domain": host,
        "verdict": verdict,
        "message": message,
        "summary": summary,
        "domestic_ips": domestic_ips,
        "overseas_ips": overseas_ips,
        "real_ips": real_ips,
        "nodes": len(rows),
        "failed": failed,
    }


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
    parser.add_argument("-v", "--verbose", action="store_true", help="显示每个节点的结果")
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
        print("    -v/--verbose   显示每个节点的 Ping 结果")
        print("    --json         以 JSON 输出结果\n")
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
