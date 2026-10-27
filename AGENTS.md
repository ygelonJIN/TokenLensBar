# TokenLensBar

## 读取规则
- 改价格表、峰谷判定、消费口径、余额接口之前，先读 `经验文档/` 里对应的文件。
- 判断不了属于哪个领域，就把 `经验文档/` 全读一遍。
- 动 `~/.tokenlensbar/usage.db`、重装、删配置之前，必须先读 `采集与部署.md` 的回退一节。

## 是什么
macOS 菜单栏 token 用量监控。Python 采集器（只用标准库）+ Swift 原生菜单栏 App + 本地看板。
纯本地、零网络；余额查询是可选功能，开启后才联网。
个人仓库，`origin` 指向 GitHub 上的 `ygelonJIN/TokenLensBar`。

## 目录
- `tokenlensbar.py` — 采集、归一化、计价、余额（含 MiMo Cookie 自动续期）、消费、report 的全部逻辑
- `menu/` — 菜单栏 App 的 Swift 源码；`GeneratedConfig.swift` 由 `build.sh` 生成
- `web/index.html` — 本地看板，静态页，读同一份 SQLite
- `tests/` — 余额 / 计价 / 扫描 / 消费 / Cookie 五组回归，`python3 -m unittest discover tests`
- `docs/` — 只放截图和图片，不放说明文档
- `assets/` — 应用图标
- `build.sh` / `install.sh` — 构建 App、装到本机
- `prices.json` — 本机实际用的价格表；`prices.example.json` 是模板
- `balances.json` / `secrets.json` — 本机余额配置与密钥，权限 600，不进版本库
- `不推送/` — 个人临时文件，全局 ignore 已挡住

## 经验索引

- `经验文档/峰谷与计价.md`
  - 什么时候读：改价格、加模型、核对账单、动 `is_off_peak` / `event_cost`、金额对不上
  - 包含：官方两档口径、三桶公式、判定实现与边界、历史计价不变的原因、五个计价坑（含同前缀型号别串价）
  - 不用读：只改菜单栏文案、图标、颜色

- `经验文档/余额与消费.md`
  - 什么时候读：接新平台余额、余额显示 0、消费金额对不上、加 provider、MiMo Cookie 过期或要配续期
  - 包含：两家的余额接口差异、缓存与刷新、余额差分法、它为什么不是账单、扩 adapter 的改法、浏览器代查、Cookie 自动续期与手动兜底
  - 不用读：只调面板布局

- `经验文档/采集与部署.md`
  - 什么时候读：采集漏数、换机器、launchd 不跑、构建失败、多机接远程源
  - 包含：文件变化触发、zstd 路径、构建缓存位置、去重主键、远程源、回退步骤
  - 不用读：只改看板样式

- `经验文档/性能与开销.md`
  - 什么时候读：刷新变卡、采集期间 CPU/内存高、会话文件多、接了远程源后采集变慢、关心数据库写入量与 SSD 寿命
  - 包含：开销基线、四个热点（型号匹配缓存 / 稳定窗口与 `can_settle` / 远程退避 / hermes 指纹跳过重写）、各自回退开关、日志位置与自截断、写入量总账、内存、余额移出采集路径
  - 不用读：只改文案、图标、颜色

## 禁止
- 不把 Key、Cookie、真实余额数值写进文档、日志或提交信息。
- 不手工改 `~/.tokenlensbar/usage.db` 的表结构；要改走代码里的迁移。
- `docs/` 只放图，说明写进 `经验文档/`。
- 不提交 `secrets.json`、`balances.json`、`*.db`。
