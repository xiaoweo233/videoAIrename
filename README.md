# VAIR · 视频 AI 重命名（前端高保真原型）

基于浏览器的视频文件智能重命名工作台高保真前端原型，覆盖工作台、实时监控、审批、日志与设置等页面。

## 目录结构
```
├── index.html        # 单页应用入口
├── css/              # 样式（base / layout / components / pages）
├── js/               # 模块脚本（app / workbench / realtime / approvals / logs / settings）
├── 产品设计.md        # 产品需求与页面能力说明
├── 技术设计.md        # 架构、模块与数据流说明
├── ffmpeg/           # 本地 ffmpeg 可执行文件（不入库，见 .gitignore）
└── exiftool/         # 本地 exiftool 可执行文件（不入库，见 .gitignore）
```

## 运行方式
直接用浏览器打开 `index.html`，或用任意静态服务器托管根目录（如 `npx serve .`）。
前端不依赖后端即可演示完整链路。

## 约定
- `ffmpeg/`、`exiftool/` 为本地第三方二进制，已通过 `.gitignore` 排除。
- `.shots/` 为调试截图产物，不入库。
