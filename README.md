# 美股期权策略推荐器 · US Options Advisor

> 输入「股票代码 + 持仓」→ 自动从 CBOE 延迟行情拉链 + 从 Longbridge(长桥)读取账户真实持仓与机构一致预期 → 推荐中长期期权策略并按到期股价测算收益。

仅供学习研究参考,**不构成投资建议**。

![首页](reports/ui_home.png)
![连接设置 / API Key](reports/ui_cfg_apikey.png)
![连接设置 / OAuth 授权](reports/ui_cfg_oauth_url.png)

---

## ✨ 核心特性

- 📊 **真实行情数据**:直接拉 CBOE 官方延迟行情接口(免费、稳定),含期权链、隐含波动率、希腊字母
- 🧠 **中长期策略全覆盖**:LEAPS、价差、备兑、现金担保卖沽(CSP)、领口、铁鹰、铁蝶等,看涨/看跌/波动中性/与持仓联动四象限分类
- 💰 **自动接入账户**:接 Longbridge(长桥)OpenAPI → 一键带入该股真实持仓、美元现金、券商一致目标价、财报日历
- 🤖 **AI 白话解读**:确定性规则引擎把"持仓 × 目标价 × 财报窗口 × 资金占用 × 策略收益"交叉成听得懂的人话,而不是甩希腊字母表
- 🖥️ **双端可用**:命令行版(`options_advisor.py`)直接出控制台+HTML 报告;Web 版(`web_server.py`)浏览器内点一点即可
- 🔐 **零手动配置**:页面右上角「连接设置」弹窗 —— API Key 直连 / OAuth 账号授权 二选一,无需改环境变量、无需重启服务

---

## 📂 项目结构

```
us-options-advisor/
├── options_advisor.py        # 核心:策略引擎 + CBOE 行情 + 报告生成
├── longbridge_api.py         # Longbridge(长桥)OpenAPI 集成 + OAuth 子进程状态机
├── web_server.py             # 本地 HTTP 服务 (ThreadingHTTPServer, 零外部依赖)
├── web/
│   ├── index.html            # 单页前端 (内联样式 + JS + ECharts)
│   └── echarts.min.js        # ECharts 5 本地副本,无需联网
├── reports/                  # 示例截图与输出
├── packaging/                # 构建 Windows 安装包的工具链
│   ├── launcher.py           # 桌面启动器:起服务 + 开浏览器 + 单实例保护
│   ├── make_icon.py          # 纯标准库绘制 app.ico(红绿 K 线图标)
│   ├── us-options-advisor.spec   # PyInstaller 打包配置(onedir)
│   ├── us-options-advisor.iss    # Inno Setup 安装脚本
│   ├── ChineseSimplified.isl     # 简体中文语言包(自包含)
│   ├── app.ico               # 应用图标
│   └── build.bat             # Windows 一键构建脚本
└── .gitignore
```

代码体量:`options_advisor.py` ~1010 行 · `web_server.py` ~760 行 · `longbridge_api.py` ~620 行 · `web/index.html` ~1010 行。**仅 Python 标准库 + ECharts + `longbridge` SDK 三个外部依赖**(`longport` 为兼容 shim,打包时一并带上)。

---

## 🚀 快速开始

### 环境要求
- **Python 3.10+**(使用了 PEP 604 `Optional[X] | None` 联合类型语法)
- Windows / macOS / Linux 均可,代码不依赖平台特定 API

### 安装
```bash
git clone https://github.com/weiyuan0917-a11y/us-options-advisor.git us-options-advisor
cd us-options-advisor

# 推荐在虚拟环境中安装
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux

pip install longbridge          # 仅第三方依赖(长桥官方新版 SDK, 默认走 openapi.longbridge.cn)
```

> ⚠️ **SDK 包名注意**:长桥官方 SDK 已由旧包 `longport` 迁移为 **`longbridge`**。旧包(≤4.3.7)二进制内硬编码了已下线的 `openapi.longport.cn`,升级也无法修复;本项目**优先使用 `longbridge`**,并在检测到只有旧包时自动注入 shim + 用 `http_url` 覆盖到新域名,向后兼容。

### 命令行版(无 Web)
```bash
# 交互模式:逐步提问
python options_advisor.py

# 一行参数跑完
python options_advisor.py --ticker AAPL --position 0 --target-price +10%
python options_advisor.py --ticker NVDA --position 200 --target-price 200 --dte 270
```

会输出:控制台策略表 + 同名 `.html` 报告(收益曲线图、希腊字母、到期情景表),默认保存到 `reports/`。

### Web 版(推荐)
```bash
python web_server.py --port 8123
# 浏览器打开 http://127.0.0.1:8123/
```

---

## 🖥️ Web UI 用法

打开 `http://127.0.0.1:8123/` 后:

1. **输入区**:填股票代码 → (可选)填持仓股数 → (可选)填目标价 → 点「分析」
2. **结果区**:「单策略」/「多策略对比」两个 Tab,各自带收益曲线表、到期 ROI、希腊字母、最大风险
4. **右侧 LongBridge 区**(可选):
   - 「从账户读取」→ 拉取该股真实持仓与美元现金
   - 「读取一致预期」→ 拉取券商一致目标价与下次财报日期
   - 自动触发页面上的「AI 策略解读」交叉验证
5. **右上角「连接设置」按钮**(⚙ 图标):
   - **方式 A · API Key 直连**:填 App Key / App Secret / Access Token → 保存即生效
   - **方式 B · OAuth 账号授权**:点「✨ 一键注册并授权」→ 自动向 `openapi.longbridge.cn` 动态注册一个 OAuth 客户端(无需去开放平台后台申请)→ 自动填入 Client ID 并弹出官方授权页 → 登录长桥账号同意 → 完成;也可点「🔗 用现有 Client ID 授权」复用已有应用
   - 「退出登录」一键清除本机凭据与 SDK 缓存令牌

> 凭据优先级:**本地配置文件 > 环境变量**。环境变量同时兼容新旧前缀:
> `LONGBRIDGE_APP_KEY` / `LONGBRIDGE_APP_SECRET` / `LONGBRIDGE_ACCESS_TOKEN` / `LONGBRIDGE_CLIENT_ID`(推荐)以及 `LONGPORT_*`(旧前缀,仍生效)。
> 自定义接口地址可用 `LONGBRIDGE_HTTP_URL`(默认 `https://openapi.longbridge.cn`)。

---

## 📡 HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/analyze?ticker=...&position=0&dte=180&min_dte=90&near_dte=45` | 主分析:返回策略表 + insight + 持仓/一致预期 |
| GET | `/api/lp/positions?ticker=...` | Longbridge:该股账户持仓 + 美元现金 |
| GET | `/api/lp/analyst?ticker=...` | Longbridge:券商一致目标价 + 财报日期 |
| GET | `/api/lp/config` | 当前凭据状态(脱敏) |
| GET | `/api/lp/oauth/status` | OAuth 授权子进程状态机 |
| POST | `/api/lp/config` | `{action:"save_apikey"\|"save_oauth"\|"register_oauth"\|"clear", ...}` |
| GET | `/api/iv?ticker=...` | 仅取 IV 历史 |
| GET | `/api/report?ticker=...` | 单策略 HTML 报告(浏览器直接打开) |
| GET | `/api/csv?ticker=...` | 策略 CSV 导出 |

所有响应均为 JSON(报告/CSV 端点除外,返回 `text/html` / `text/csv`)。

---

## ⚙️ 命令行参数

```text
options_advisor.py [-h] [--ticker TICKER] [--position POSITION]
                   [--target-price TARGET] [--dte DTE]
                   [--min-dte MIN_DTE] [--near-dte NEAR_DTE]
                   [--chart-top N] [--data-file FILE] [--out OUT]
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--ticker` | 交互 | 股票代码,如 `AAPL` |
| `--position` | 0 | 当前持仓股数(可为 0 或负) |
| `--target-price` | 现价 | 绝对价 `350` 或涨跌幅 `+10%`/`-5%` |
| `--dte` | 180 | 主到期日目标天数 |
| `--min-dte` | 90 | 中长期最短过滤 |
| `--near-dte` | 45 | 日历/对角策略近月腿天数 |
| `--chart-top` | 6 | HTML 报告中画收益曲线的策略数 |
| `--data-file` | - | 调试:本地 CBOE JSON 缓存 |
| `--out` | - | HTML 报告输出路径 |

---

## 🧱 架构

```
浏览器(index.html + ECharts)
        │  fetch /api/*
        ▼
ThreadingHTTPServer(web_server.py)
   ├── /api/analyze ──► options_advisor.build_strategies + make_insight
   ├── /api/lp/*   ──► longbridge_api (长桥 SDK `longbridge`, endpoint=openapi.longbridge.cn)
   │                   ├── 凭据优先级: 文件 > 环境变量
   │                   ├── OAuth 一键注册: POST /oauth2/register (动态客户端注册)
   │                   └── OAuth 子进程 ──► 状态文件 ──► 父进程轮询 (避免 GIL 冻结服务)
   └── /api/report, /api/csv ──► HTML/CSV 模板
        │
        ▼
   CBOE 延迟行情 (urllib, 无需 key)
```

### 关键设计

- **零外部 Web 依赖**:服务端仅用 `http.server` + `urllib`;前端 ECharts 走本地 `web/echarts.min.js`
- **SDK 双版本兼容层**:优先 `longbridge`(新包,默认新域名),缺失时兜底 `longport`(旧包)并注入 `sys.modules` shim + `http_url` 覆盖,所有 URL 统一做旧域名替换
- **OAuth 一键注册**:走 RFC 7591 动态客户端注册(`POST /oauth2/register`),用户无需去开放平台后台申请 client_id
- **OAuth 子进程隔离**:`OAuthBuilder(...).build()` 在等浏览器授权期间会持有 GIL 锁死解释器 → 拆到独立子进程跑 + 状态文件(`~/.longport/oauth_state.json`)同步,主服务一直可响应
- **凭据安全清除**:Windows 下自写文件常被杀软句柄锁,导致 `os.remove` 自己刚写的文件失败 → 改为"原子覆写为空 `{}` + 尽力删除",清除语义不依赖物理删除
- **前端去重**:OAuth URL 弹窗的去重保护,避免每次轮询都开新标签页

---

## ⚠️ 已知问题与注意事项

- 📡 **本沙箱环境无法访问 Yahoo / yfinance**:网络层被限流 → 本工具改用 CBOE 延迟行情,无需 key 即稳定
- 🌐 **接口域名已迁移**:长桥 OpenAPI 由 `openapi.longport.cn` / `openapi.longportapp.com` 迁移至 **`https://openapi.longbridge.cn`**。本项目已全面适配:默认 endpoint、环境变量兜底、OAuth 授权链接、token 缓存目录均指向新域名。**若你在沙箱内测试,新域名可达性取决于沙箱网络策略;在正常网络环境下账户/持仓/一致预期读取均可用**
- 🔑 **旧 Client ID 可能失效**:域名迁移后,在旧平台申请的 OAuth client_id 在新授权服务器上可能返回 `oauth client not found` → 直接用「✨ 一键注册并授权」重新获取即可
- 💵 **期权杠杆高、风险大**:任何策略建议务必先在模拟盘验证;本工具仅供学习研究,不构成投资建议
- 🪟 **跨平台提示**:Windows 上读写用户目录走 `%USERPROFILE%` 等价 `~`;Linux/macOS 同理

---

## 🔄 域名迁移说明(2026)

长桥官方已把 OpenAPI 入口从 `openapi.longport.cn` 切换到 `openapi.longbridge.cn`,并同步发布了新 SDK 包 `longbridge`。本项目做了四件事:

| 项 | 迁移前 | 迁移后 |
|---|---|---|
| SDK 包 | `longport` ≤4.3.7(二进制内硬编码旧域名) | **`longbridge`**(默认即新域名),旧包自动 shim 兼容 |
| 接口地址 | `openapi.longport.cn` | **`https://openapi.longbridge.cn`**(可用 `LONGBRIDGE_HTTP_URL` 覆盖) |
| 环境变量 | `LONGPORT_*` | **`LONGBRIDGE_*`**(旧前缀仍兼容) |
| OAuth 客户端 | 需去开放平台后台申请 | **页面一键动态注册**(RFC 7591 `POST /oauth2/register`) |

> 兼容层实现:`longbridge_api.py` 顶部 `try: import longbridge ... except ImportError:` 兜底 `longport` + 注入 `sys.modules` shim,并对所有 URL 做旧域名 → 新域名替换,确保新旧环境都能跑。

---

## 🛠️ 开发

```bash
# 本地直接运行(已配置好 python 路径即可)
python web_server.py --port 8123

# 验证 LongBridge 配置链路(假 client_id 也可,只看状态机)
curl http://127.0.0.1:8123/api/lp/config
curl http://127.0.0.1:8123/api/lp/oauth/status

# 动态注册一个 OAuth 客户端(无需后台申请), 返回 client_id 并自动启动授权
curl -X POST http://127.0.0.1:8123/api/lp/config \
     -H "Content-Type: application/json" \
     -d '{"action":"register_oauth","callback_port":60355}'

# 触发一次分析
curl "http://127.0.0.1:8123/api/analyze?ticker=AAPL&position=0&dte=180&min_dte=90&near_dte=45" | head
```

代码遵循单一职责:
- 算法/数学 → `options_advisor.py`(纯函数,无 IO 副作用)
- 券商集成 → `longbridge_api.py`(所有副作用收敛于此)
- HTTP/前端 → `web_server.py` + `web/index.html`
- 安装打包 → `packaging/`(桌面启动器、图标、Inno Setup 脚本)

---

## 📦 打包为 Windows 安装包

```bat
packaging\build.bat
```

一键脚本会自动完成三步:

1. **生成应用图标** → `packaging\app.ico`(纯标准库绘制红绿 K 线)
2. **PyInstaller 打包** → `dist\USOptionsAdvisor\`(onedir,带 Python 运行时与 SDK)
3. **Inno Setup 编译** → `dist\USOptionsAdvisor-Setup-<版本>-win64.exe`(lzma2/max 压缩)

**前置条件**:
- Python 3.10+ + `pip install pyinstaller`
- Inno Setup 6.3+(提供 `ISCC.exe`,从 https://jrsoftware.org/isdl.php 下载)

**产物体积**:

| 阶段 | 内容 | 体积 |
|---|---|---|
| 源代码 | `*.py` + `web/` | ~1.6 MB |
| PyInstaller onedir | 源代码 + Python 3.13 运行时 + longbridge/longport SDK | ~97 MB |
| **Inno Setup 安装包** | 上述全部压成单一 exe | **~23 MB** |

**安装包特性**:
- 现代向导界面(简体中文 + 英文回退)
- 仅 64 位 Windows 10+(用 `longbridge` 4.5 的 Rust 扩展)
- 无需管理员权限安装(`PrivilegesRequired=lowest`,普通用户也能装)
- 双击桌面图标自动起服务、开浏览器
- **单实例保护**:重复双击复用同一窗口(命名 mutex)
- 卸载程序自动清理安装目录、桌面/开始菜单快捷方式、注册表项
- 自动写入「应用和功能」卸载入口

**端到端验证**(在 Windows 10/11 实机或同等环境):

```bat
:: 静默安装到自定义目录(便于测试)
dist\USOptionsAdvisor-Setup-1.0.0-win64.exe /VERYSILENT /SUPPRESSMSGBOXES /DIR=D:\Test\UOpts

:: 安装后双击 USOptionsAdvisor.exe → 浏览器自动打开 http://127.0.0.1:8123/
:: 通过控制面板「应用和功能」卸载即可,或运行安装目录下的 unins000.exe
```

---

## 📜 License

MIT。代码仅供学习研究,**不构成投资建议**。

---

## 🙋 常见问题

**Q: 长桥接口地址从 `openapi.longport.cn` 换成 `openapi.longbridge.cn` 了,要改什么?**
A: 什么都不用改。项目默认已指向新域名;若要指向别的环境,设 `LONGBRIDGE_HTTP_URL` 即可。SDK 请用 `pip install longbridge`(新包)。

**Q: OAuth 授权报 `oauth client not found`?**
A: 说明你的 client_id 是旧域名平台时期申请的,在新授权服务器上已失效 → 在「连接设置 → 方式 B」点「✨ 一键注册并授权」重新获取一个即可,秒级完成、无需申请。

**Q: 必须配 Longbridge 才能用吗?**
A: 不配也能用,只是无法自动带入持仓/一致预期,所有参数都要手填。

**Q: 不想给 Longbridge 凭据怎么办?**
A: 不点「连接设置」即可,工具完全本地运行,不会向长桥发送任何请求(除非你主动点那两个读取按钮)。

**Q: 我已有别的券商账户,能换数据源吗?**
A: `longbridge_api.py` 是单一收敛点,按其函数签名替换为其他券商 SDK 即可(`effective_creds / oauth_begin / fetch_positions / fetch_analyst` 等)。

**Q: 推荐器给出的策略我可以直接下单吗?**
A: 请务必人工复核。本工具仅输出策略建议与到期测算,不会自动下单,也不应被理解为投资建议。