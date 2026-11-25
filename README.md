# CDNCheck

该脚本可以进行 CDN 检测。主要原理是调用站长之家（chinaz.com）的全球多地 Ping，
根据国内外各节点解析到的 IP 是否分散来判断目标是否使用 CDN，同时支持单个域名和多个域名检测。

> 说明：站长之家已下线旧的 `iframe.ashx` + 固定 guid 节点接口，旧版写死的 35 个节点
> （如 `278d10bf-cbc5-494a-bbab-e76c05b4f3cd`）已全部失效。当前版本改用站长之家
> 新的 Ping WebSocket 接口（`wss://tooldata.chinaz.com/pingwebsocket`），节点由服务端
> 动态下发，无需再维护节点列表。

### 一、项目简介

#### 1. 项目介绍

脚本使用 python3 编写，支持单个域名或文件方式检测，域名支持带 http(https)，也可以不带。
每次检测会先访问 `https://ping.chinaz.com/<域名>` 获取本次查询 token 和节点列表，
再通过 WebSocket 实时接收各地节点结果，按国内 / 海外分组统计不同 IP 数量：

- 任意一组出现多个不同 IP → 判定可能使用了 CDN；
- 仅一组只有 1 个 IP → 输出该 IP 作为可能的真实 IP；
- 国内外各 1 个但 IP 不同 → 提示疑似 CDN。

#### 2. 依赖

```
pip install -r requirements.txt
```

即需要 `requests` 和 `websocket-client`。

#### 3. 说明

(1) 节点数量与服务端当前下发的节点一致（通常 40+ 个，随站长之家调整而变化），
如需加快速度可调小 `-t/--timeout`。

(2) 脚本执行完成之后，会在命令行打印是否存在 CDN，也会在当前目录下生成 `result.txt`，
采用追加方式写入；如果重新检测，可以删除 `result.txt`。

(3) 将国内外节点区分开，按以下规则判断（与代码 `analyze()` 一致）：

- 国内有多个 IP、海外只有 1 个 IP：国内用了 CDN，海外没走 CDN，**海外节点的 IP 即可能的真实 IP**；
- 海外有多个 IP、国内只有 1 个 IP：海外用了 CDN，国内没走 CDN，**国内节点的 IP 即可能的真实 IP**；
- 国内、海外都有多个 IP：两侧都用了 CDN，无法直接定位真实 IP，需继续用其他手段；
- 国内、海外都只有 1 个相同 IP：未使用 CDN，该 IP 即真实 IP；
- 国内、海外各 1 个但 IP 不同：疑似 CDN（两地解析到不同节点），需进一步判断；
- 某一侧没有节点结果：只根据有结果的一侧给出参考 IP，并明确标注。

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
-v/--verbose   显示每个节点的 Ping 结果
--json         以 JSON 输出检测结果
```

例如：

```
python3 cdncheck.py -u www.example.com -v
python3 cdncheck.py -f urls.txt -t 60 --json
```

### 三、免责声明

本工具仅用于授权的安全测试与学习研究，请勿用于未授权的目标。
