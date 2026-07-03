# astrbot_plugin_genshin_showcase

原神角色展示窗 AstrBot 插件

## 功能

- `/bind_uid <UID>` — 绑定原神UID
- `/my_showcase` — 查询展示窗角色列表
- 发送角色名称 — 自动生成角色详情合成卡片（武器、圣遗物、天赋）

## 目录树

```
astrbot_plugin_genshin_showcase/
├── __init__.py              # 主插件代码
├── metadata.yaml            # 插件元数据
├── requirements.txt         # Python依赖
├── README.md                # 本文件
├── assets/
│   ├── alias_map.json       # 角色别名映射
│   └── char_icons/          # 角色预置图标目录（可留空，自动从CDN下载）
└── data/
    └── genshin_showcase_uid.json  # UID绑定数据（运行时自动生成）
```

## 部署检查清单

### 1. 依赖安装

```bash
pip install -r requirements.txt
```

确保安装: `aiohttp>=3.9.0`, `Pillow>=10.0.0`

### 2. 放置插件

将整个 `astrbot_plugin_genshin_showcase/` 目录复制到 AstrBot 的 `plugins/` 目录下。

### 3. 字体文件（可选但推荐）

下载 [思源黑体](https://github.com/adobe-fonts/source-han-sans) 的 `SourceHanSansSC-Regular.otf` 放入 `assets/` 目录。
若无字体文件，插件会回退到系统字体（如微软雅黑），但仍建议内置以避免乱码。

### 4. data 目录权限

确保 AstrBot 进程对 `data/` 目录有读写权限。

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

## 技术参考

- [AstrBot 开发文档](https://astrbot.app/dev/plugin-minimal)
- [Enka.Network API 文档](https://enka.network/docs/)
- [AstrBot 持久化存储](https://astrbot.app/dev/persistence)
