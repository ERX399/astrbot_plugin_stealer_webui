# Stealer WebUI

把 `astrbot_plugin_stealer` 当前表情管理 WebUI 从 AstrBot 面板中独立出来，改为外部端口访问

## 功能

- 独立 aiohttp 后端，不嵌入 AstrBot 面板
- 支持密码登录


## 数据保护

默认开启保护模式，防止本独立 WebUI 误删、误移动或替换原 `astrbot_plugin_stealer` 数据。

默认行为：

- 拒绝删除单张/批量删除表情文件
- 拒绝批量移动表情文件
- 拒绝删除分类目录
- 拒绝改写分类配置
- 修改 JSON 索引/配置前自动备份到 `.stealer_webui_backups/`
- 所有文件访问限制在 Stealer 数据目录内部

相关配置：

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `protect_original_data` | `true` | 保护原插件数据 |
| `allow_destructive_operations` | `false` | 是否允许删除、移动、分类删除等破坏性操作 |
| `backup_on_write` | `true` | 写入 JSON 前自动备份 |

如果你只是想安全浏览和预览表情包，保持默认即可。

## 限制

独立版不能直接调用目标插件运行时里的 VLM/缓存服务，因此以下能力当前返回提示性错误：

- 自动分析 `/api/analyze`
- 上传/批量上传 `/api/images/upload`、`/api/images/batch-upload`

浏览、搜索、预览、修改描述/标签/场景/分类、删除、批量移动、批量作用域调整可用

## AstrBot 插件模式

将目录放入 AstrBot 插件目录后启用

默认访问：

```text
http://0.0.0.0:9191
```

## 端口占用处理

默认监听 `9191`。如果目标端口被占用，插件只会尝试释放该目标端口并重新绑定同一端口；如果仍不可用则报错退出。插件不会自动尝试 `9192`、`9193` 等后续端口，避免意外打开多个端口。

## 配置项

| 配置项 | 说明 | 默认值 |
|---|---|---|
| `webui_host` | WebUI 监听地址 | `0.0.0.0` |
| `webui_port` | WebUI 监听端口 | `9191` |
| `webui_password` | WebUI 访问密码 | 空 |
| `stealer_data_dir` | Stealer 数据目录覆盖 | 空 |
| `stealer_plugin_name` | Stealer 插件数据目录名 | `astrbot_plugin_stealer` |
| `release_occupied_port` | 端口占用时尝试释放 | `true` |

## 目录结构

```text
astrbot_plugin_stealer_webui/
├── main.py
├── webui.py
├── metadata.yaml
├── _conf_schema.json
├── requirements.txt
└── web/
    ├── index.html
    ├── login.html
    ├── app.css
    ├── compact.css
    └── app.js
```

## License

MIT
