# VideoMind 前端

Vue 3 / Vite 客户端包含媒体库、分析工作台与 Pixel Future Academy 设计实验室。完整产品介绍和后端启动步骤见 [根目录 README](../README.md) 与 [启动指南](../docs/QUICK_START.md)。

```bash
npm ci
npm run dev
```

开发代理默认指向 `http://127.0.0.1:8000`；更改后端地址可设置 `VITE_DEV_PROXY_TARGET`。独立部署前端时可设置 `VITE_API_BASE_URL`。测试与构建命令是 `npm test`、`npm run build`。

`/design-lab` 只展示设计示例，不访问后端。设计规则见 [DESIGN_SYSTEM.md](../docs/design/DESIGN_SYSTEM.md)，上游改编声明见 [NOTICE.md](NOTICE.md)。
