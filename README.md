[README.md](https://github.com/user-attachments/files/35442796/README.md)
# CDNCheck

该脚本可以进行 CDN 检测。主要原理是调用站长之家（chinaz.com）的全球多地 Ping，
拿到各地解析到的 IP 后，**查询每个 IP 的 ASN/归属，结合归属厂商与地域分布**判断
是否使用 CDN，而不是单纯数 IP 个数。支持单个域名和多个域名检测。

> 说明：站长之家已下线旧的 `iframe.ashx` + 固定 guid 节点接口，旧版写死的 35 个节点
> （如 `278d10bf-cbc5-494a-bbab-e76c05b4f3cd`）已全部失效。当前版本改用站长之家
> 新的 Ping WebSocket 接口（`wss://tooldata.chinaz.com/pingwebsocket`），节点由服务端
> 动态下发，无需再维护节点列表。

### 一、项目简介

#### 1. 项目介绍

脚本使用 python3 编写，支持单个域名或文件方式检测，域名支持带 http(https)，也可以不带。
每次检测会先访问 `https://ping.chinaz.com/<域名>` 获取本次查询 token 和节点列表，
再通过 WebSocket 实时接收各地节点结果；之后对解析出的每个 IP 查询归属（ASN、ISP、
机构名），按“归属 + 地域分布”给出结论。

#### 2. 判定规则

只看 IP 数量会把 Anycast、DNS 轮询、双线机房都误判成 CDN，因此以归属为准
（与代码 `_owner_rules()` 一致）：

| 现象 | 结论 |
|---|---|
| 多地解析到不同 IP，且归属同一家 CDN 厂商（Cloudflare、Akamai、Fastly、CloudFront、网宿、七牛等） | **上了 CDN** |
| 所有地方解析到同一组 IP、组内轮询，归属同一运营商/IDC | **大概率没上 CDN**，是 DNS 轮询/负载均衡 |
| 不同地区解析到不同 IP，但归属是不同运营商（电信/联通/移动）的自有地址段 | **双线/多线机房**，不是 CDN |
| 全球解析到同一个 IP（Anycast/就近路由），归属不是 CDN 厂商 | **不是 CDN**；若多国 RTT 同时很低 → Anycast 服务地址，不是源站真实 IP；若 RTT 随距离递增 → 单点 unicast，该 IP 即真实 IP |
| 云厂商（阿里云/腾讯云/百度云/华为云等）IP 分布在多个省份 | 云 CDN，**上了 CDN** |
| 云厂商 IP 集中在同一地区、组内轮询 | 云主机轮询，**不是 CDN** |
| 单 IP、归属运营商/IDC/云主机 | **不是 CDN**，该 IP 即真实 IP |
| 国内有 CDN、海外只有 1 个非 CDN 厂商 IP（反之亦然） | 该单 IP 即**可能的真实 IP** |
| 归属查询失败 | 退化为按 IP 数量粗略判断，并在结果中注明 |

> Anycast 的坑：同一 IP 段全球可达、各地延迟不同，是路由就近接入，不是 CDN；
> 是否 CDN 要看这个 IP 段归谁所有。

#### 3. Anycast 与单点真实 IP 怎么区分

同一个 IP 从各地都能 ping 通，并不代表它是真实 IP。单靠“是不是同一个 IP”无法区分
Anycast 和单点服务器，需要看下面的证据：

| 证据 | Anycast | 单点 unicast |
|---|---|---|
| 多地 RTT | 多个相距很远的国家 RTT 同时很低（都有本地 PoP） | RTT 随地理距离递增，远端明显高 |
| 路由路径（traceroute/mtr） | 不同地区末跳落在不同城市/不同 AS | 不同地区路径最终收敛到同一城市 |
| BGP / 数据源 | 前缀在多地宣告；ipinfo 等服务会标记 `anycast` | 前缀只在源站所在地宣告 |

工具目前用前两项里可自动获取的部分做判断（`guess_anycast()`）：

- `ipinfo.io` 返回 `anycast: true` → 判 Anycast；
- 多个海外国家的节点 RTT 都 ≤50ms → 单台物理主机不可能同时离这么多国家这么近，判 Anycast；
- 海外最远节点 RTT ≥150ms、国内 RTT ≤80ms → 符合单点机房的物理距离衰减，判单点，该 IP 即真实 IP；
- 证据不足时按单点处理，但要人工复核。

局限：纯国内 Anycast（只在境内多地宣告）和国内单点机房从 RTT 上很难区分；
RTT 还受国际线路质量影响。要确诊 Anycast，最可靠的是多 vantage point 的
traceroute 或 BGP looking glass，本工具没有做这部分。

#### 4. 依赖

```
pip install -r requirements.txt
```

即需要 `requests` 和 `websocket-client`。

IP 归属查询使用免费接口，无需注册：

- 首选 `ip-api.com` 批量接口（HTTP，每批最多 100 个 IP，限 15 次/分钟）；
- 失败时回退 `ipwho.is`（HTTPS，逐个查询）；
- 查询结果缓存到系统临时目录 `cdncheck_ip_owner_cache.json`，重复检测不再请求。

#### 5. 说明

(1) 节点数量与服务端当前下发的节点一致（通常 40+ 个，随站长之家调整而变化），
如需加快速度可调小 `-t/--timeout`。

(2) 脚本执行完成之后，会在命令行打印是否存在 CDN，也会在当前目录下生成 `result.txt`，
采用追加方式写入；如果重新检测，可以删除 `result.txt`。

(3) 使用 `-v/--verbose` 会额外打印每个节点的 IP 与归属，`--json` 会输出包含 `owners`
字段的完整结果，便于二次处理。

### 二、用法说明

#### 1. 使用方法

(1) 查看帮助

```
python3 cdncheck.py -h
python3 cdncheck.py --help
```

(2) 检测单个域名

```
python3 cdncheck.py -u domain
python3 cdncheck.py --url domain
```

(3) 检测多个域名

```
python3 cdncheck.py -f url.txt
python3 cdncheck.py --filename url.txt
```

(4) 可选参数

```
-t/--timeout   单域名超时时间（秒，默认 45）
-v/--verbose   显示每个节点的 Ping 结果和 IP 归属
--json         以 JSON 输出检测结果（含每个 IP 的归属信息）
```

例如：

```
python3 cdncheck.py -u www.example.com -v
python3 cdncheck.py -f urls.txt -t 60 --json
```

### 三、免责声明

本工具仅用于授权的安全测试与学习研究，请勿用于未授权的目标。
