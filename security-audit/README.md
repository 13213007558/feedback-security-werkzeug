# 网关原始前缀 vs Werkzeug 路由结果：路径理解分歧排查

针对 Werkzeug 3.2.0.dev 快照。结论基于可运行脚本，证据见 `evidence-audit.json`
与 `evidence-wire.json`，断言见 `test_authorization_placement.py`。

## 一句话结论

只在网关层用**未解码的原始请求目标**比较 `/admin` 前缀**不可靠**：WSGI 服务器会
先做一次百分号解码再写 `PATH_INFO`，Werkzeug 路由完全在解码后的 `PATH_INFO` 上匹配。
授权必须以 Werkzeug `match()` 返回的 **endpoint（路由结果）** 为准；网关的原始前缀
规则只能做粗粒度的纵深防御，不能作为最终授权点。本快照中 Werkzeug 的行为均符合其
公开契约，**未发现需要修复的库缺陷**，因此不改动任何路由规则或库代码。

## 每一层在哪里解码 / 重定向

1. **网关**：看到的是请求行里的原始 request target（字节），通常不做百分号解码，
   也不保证做路径归一化。`naive_gateway_allows()`（`app.py`）就是这种朴素前缀策略。
2. **WSGI 服务器**（PEP 3333 / `werkzeug.serving.make_environ`）：把 request target
   百分号解码**恰好一次**写入 `PATH_INFO`；`SCRIPT_NAME` 同样解码一次。**不**折叠点段
   （`.`/`..`），**不**合并重复斜杠。`REDIRECT_URI`/`RAW_URI` 保留未解码原文。
3. **Werkzeug 路由**（`routing`）：在 `request.path`（即解码后的 `PATH_INFO`，去掉
   `SCRIPT_NAME` 前缀）上用状态机匹配。默认 `merge_slashes=True` 会对连续斜杠产生
   **308 重定向**到归一化 URL；`strict_slashes=True` 对尾斜杠产生 308；`path` 转换器
   （`regex="[^/].*?"`，`part_isolating=False`）会吞掉含 `/` 的剩余部分。
4. **应用**：只有走到 endpoint 之后才做授权（`SecureApp.dispatch`）。308/404/405 在
   endpoint 选定**之前**抛出，应用层无需也无法对其套用 endpoint 权限。

## 可运行证据（完整对照）

`anon/admin` 两列分别是匿名身份与 `web.identity=admin` 时的 HTTP 状态码。

| 原始请求目标 | 网关(原始前缀) | 服务器 PATH_INFO | Werkzeug 路由结果 | 匿名/管理员 | 测试客户端 PATH_INFO | 分歧 |
|---|---|---|---|---|---|---|
| `/public/hello` | 放行 | `/public/hello` | public_hello | 200/200 | `/public/hello` |  |
| `/admin` | 拦截 | `/admin` | admin_home | 403/200 | `/admin` |  |
| `/admin/users/42` | 拦截 | `/admin/users/42` | admin_sub | 403/200 | `/admin/users/42` |  |
| `/%61dmin/users` | **放行** | `/admin/users` | admin_sub | **403**/200 | `/admin/users` |  |
| `/admin%2fusers` | 拦截 | `/admin/users` | admin_sub | 403/200 | `/admin/users` |  |
| `/admin%252fusers` | 拦截 | `/admin/%2fusers` | 404 | 404/404 | `/admin/%2fusers` |  |
| `/%2561dmin` | 放行 | `/%61dmin` | 404 | 404/404 | `/%61dmin` |  |
| `//admin/users` | **放行** | `/admin/users`（见下注） | admin_sub | **403**/200 | `/users` | **是** |
| `/admin//users` | 拦截 | `/admin//users` | 308 → `/admin/users` | 308/308 | `/admin//users` |  |
| `/public/../admin/users` | 放行 | `/public/../admin/users` | 404 | 404/404 | 同左 |  |
| `/public/%2e%2e/admin/users` | 放行 | `/public/../admin/users` | 404 | 404/404 | 同左 |  |
| `/admin/%2e/users` | 拦截 | `/admin/./users` | admin_sub (`sub='./users'`) | 403/200 | 同左 |  |
| `/files/etc/passwd` | 放行 | `/files/etc/passwd` | files | 200/200 | 同左 |  |
| `/files/a%2fb` | 放行 | `/files/a/b` | files (`filename='a/b'`) | 200/200 | 同左 |  |
| `/app/admin/users`（SCRIPT_NAME=/app） | 放行 | `/admin/users` | admin_sub | 403/200 | `/app/admin/users` | **是** |
| `/app/%61dmin/users`（挂载） | 放行 | `/admin/users` | admin_sub | 403/200 | `/app/admin/users` | **是** |
| `/%61pp/admin`（SCRIPT_NAME 自身被编码） | 放行 | `/admin` | admin_home | 403/200 | `/app/admin` | **是** |
| `/public/hello?next=/admin` | 放行 | `/public/hello` | public_hello | 200/200 | 同左 |  |
| `/admin/` | 拦截 | `/admin/` | 404 | 404/404 | 同左 |  |

> 注：`//admin/users` 的服务器 `PATH_INFO` 取决于服务器。`wsgiref` 等通用服务器原样
> 保留为 `//admin/users`（见 `evidence-wire.json`）；Werkzeug 开发服务器因
> `urlsplit` 把前导 `//host` 当作 netloc 再补回，得到 `/admin/users`。两者最终都路由到
> `admin_sub`。而测试客户端 `EnvironBuilder(path="//admin/users")` 因提前 `urlsplit`
> 且**不**做开发服务器那段 netloc 修复，得到 `PATH_INFO=/users` → 404。这正是题目提醒的
> “不要把测试客户端加工后的字符串当成服务器收到的原始字节”。

## 三个短反例（外层原始前缀比较失效）

- **编码首字母**：`GET /%61dmin/users`。网关看到的路径不以字面量 `/admin` 开头 → 放行；
  服务器解码 `%61→a`，`PATH_INFO=/admin/users` → 路由到 `admin_sub`。若授权只在网关，
  匿名用户直达管理端点。应用按 endpoint 授权仍返回 **403**。
- **前导重复斜杠**：`GET //admin/users`。朴素 `startswith("/admin")` 为假 → 网关放行；
  服务器侧 `PATH_INFO` 为 `//admin/users` 或 `/admin/users`，Werkzeug 都路由到
  `admin_sub` → 应用 **403**。
- **挂载点 + 编码**：应用挂在 `/app`，`GET /app/%61dmin/users`。解码发生在剥离
  `SCRIPT_NAME` 之前，剥离后 `PATH_INFO=/admin/users` → `admin_sub`。若网关规则只匹配
  根路径的 `/admin` 字面量，也会漏判；应用 endpoint 授权不受影响（**403**）。

## 合法编码路径的对照

- `GET /files/a%2fb` 是 `path` 转换器的**正当**用法：一次解码后 `filename='a/b'`，
  命中公开的 `files` endpoint，返回 **200**。这说明“解码后再路由”本身不是漏洞——
  漏洞只出在“用未解码字符串做授权判断”。
- 双重编码是安全的控制组：`/admin%252fusers` 一次解码后是字面量 `/admin%2fusers`，
  斜杠没有被还原，**404**，无法借双重编码走私斜杠。
- 点段同样安全：Werkzeug 不做 `..` 归一化，`/public/../admin/users`（含 `%2e%2e`
  形式）字面量不匹配任何规则 → **404**，既不绕过也不越权。

## 授权应该放在哪里

- **应用层（强制点）**：在 `adapter.match()` 得到 endpoint 之后，依据 endpoint 的权限
  元数据判定。参考 `app.py` 的 `PROTECTED_ENDPOINTS` 与 `SecureApp.dispatch`——它从不
  检查原始 URL 字符串，只看路由结果，因此对任意编码/归一化都给出一致的 403/200。
- **WSGI 服务器必须承担的拒绝**：
  - 对**畸形 request target / 非法百分号序列**返回 400（应用拿到的应是已合法解码的
    environ，而不是替服务器兜底解析）。
  - 拒绝或规范化**超出 URI 长度/控制字符/空字节**等传输层异常。
  - 点段归一化（`/.`、`/..`）若要做，必须在服务器/边缘**统一**完成；否则不同服务器
    行为不同（本快照 Werkzeug 不归一化，直接 404）。应用不应假设点段已被清理。
  - `SCRIPT_NAME`/挂载前缀的拆分由服务器负责，应用只信任拆分后的 `PATH_INFO`。
- **网关（仅纵深防御，不能单独担责）**：可以基于 `REQUEST_URI`/`RAW_URI` 做粗粒度拦截、
  WAF、限流，但因为它看到的是未解码原文，无法与路由结果保证一致，必须允许应用再判一次。

## 为什么不改库

- `docs/routing.rst` 明确“路由在 `PATH_INFO` 上应用匹配”，`merge_slashes`、
  `strict_slashes` 的 308 行为均有文档；`serving.make_environ` 与测试客户端的单次
  `unquote` 符合 PEP 3333（`PATH_INFO` 是已解码值）。
- 测试客户端对 `//` 的 `urlsplit` 加工属于其“传入的是待解析 URL 而非线上字节”的既定
  输入语义，`docs/test.rst` 也明确支持手工构造 environ。属使用注意事项，非违约。
- 上游 `tests/test_routing.py` 与 `tests/test_test.py` 共 246 项全部通过，库行为未被改动。

## 运行方式

```sh
.venv/bin/python security-audit/audit.py                       # 19 用例，服务器 vs 测试客户端
.venv/bin/python security-audit/wire_driver.py                # 10 用例，纯字节级 WSGI 驱动
.venv/bin/python security-audit/test_authorization_placement.py   # 8 条可回归断言
.venv/bin/pytest security-audit/test_authorization_placement.py
```

## 环境限制

本沙箱禁止本地 TCP 绑定（`bind` 返回 `EPERM`），无法启动真实 HTTP 服务器做 socket 级
发送。服务器侧真值改用两条等价路径逼近并交叉验证：`audit.server_environ` 逐行复刻
`WSGIRequestHandler.make_environ` 的 request-line 解析与单次解码；`wire_driver.py` 从
原始字节按 PEP 3333 手工构造 environ。两者结论一致。若放开
`sandbox.network.allowLocalBinding`，可再用真实 `run_simple` + 原始 socket 复核。
