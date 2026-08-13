# astrbot_plugin_genshin_showcase

原神角色展示窗 AstrBot 插件

## 功能

- `/bind_uid <UID>` — 绑定原神UID
- `/my_showcase` — 查询展示窗角色列表
- 发送角色名称 — 自动生成 enka.network 风格角色详情合成卡片（角色立绘、武器、属性面板、圣遗物、套装）

## 目录树

```
astrbot_plugin_genshin_showcase/
├── main.py                  # 主插件代码（AstrBot v3.5.19+ 入口，勿再添加 __init__.py）
├── metadata.yaml            # 插件元数据
├── requirements.txt         # Python依赖
├── README.md                # 本文件
├── assets/
│   ├── alias_map.json       # 角色别名映射
│   ├── item_names.json      # 精简游戏数据（hash→中文名、角色ID→图标名/中文名）
│   ├── char_icons/          # 角色立绘缓存目录（运行时自动从CDN下载）
│   └── icons/               # 武器/圣遗物图标缓存目录（运行时自动下载）
└── data/
    ├── genshin_showcase_uid.json  # UID绑定数据（运行时自动生成）
    └── build_assets.py            # 资产构建脚本（可选，用于更新 item_names.json）
```

## 部署检查清单

### 1. 依赖安装

```bash
pip install -r requirements.txt
```

确保安装: `aiohttp>=3.9.0`, `Pillow>=10.0.0`

### 2. 放置插件

将整个 `astrbot_plugin_genshin_showcase/` 目录复制到 AstrBot 的 `plugins/` 目录下。

### 3. 字体文件（推荐）

卡片渲染需要中文字体。将任一字体文件放入 `assets/` 目录：

- `SourceHanSansSC-Regular.otf`（思源黑体，推荐）
- `msyh.ttc`（微软雅黑）
- `SourceHanSansCN-Regular.otf`

若没有内置字体，插件会回退到系统字体（如 Linux 的 Noto CJK、Windows 的微软雅黑），但建议内置以避免乱码。

### 4. data 目录权限

确保 AstrBot 进程对 `data/`、`assets/` 目录有读写权限（图标缓存需要写入 assets/）。

### 5. API 连通性测试

在绑定UID之前，先确认可访问 Enka.Network：

```bash
curl -I https://enka.network/api/uid/123456789
```

预期返回 `HTTP/2 200` 或 `HTTP/2 404`（404仅表示该UID无数据，但API可达）。

### 6. 插件加载

重启 AstrBot，在管理面板中确认 `genshin_showcase` 插件已加载。

### 7. 功能验证

1. 发送 `/bind_uid 你的UID` 绑定账号
2. 发送 `/my_showcase` 获取角色列表
3. 发送任意角色名称（如"钟离"）验证卡片生成

## 卡片样式

卡片为 enka.network 风格的三栏深色布局：

- **左栏**：角色立绘 + 名称/元素徽章 + 等级/命座 + 天赋等级 + UID
- **中栏**：武器信息（图标/名称/精炼/等级/基础攻击/副属性）+ 属性面板（生命/攻击/防御/精通/双暴/充能/元素伤害）+ 圣遗物套装
- **右栏**：五件圣遗物详情（图标/主词条/等级/星级/副词条）

## 数据说明

- 角色/武器/圣遗物中文名来自 [AnimeGameData](https://gitlab.com/Dimbreath/AnimeGameData)（GitLab 镜像），已精简为 `assets/item_names.json`
- 角色立绘、武器、圣遗物图标运行时从 Enka CDN（`https://enka.network/ui/`）自动下载并缓存
- 如需更新 `item_names.json`：下载 AnimeGameData 的 `TextMap/TextMap_MediumCHS.json` 及相关 ExcelBinOutput JSON 到 `data/` 目录，运行 `data/build_assets.py`

## 技术参考

- [AstrBot 开发文档](https://astrbot.app/dev/plugin-minimal)
- [Enka.Network API 文档](https://enka.network/docs/)
- [AstrBot 持久化存储](https://astrbot.app/dev/persistence)
