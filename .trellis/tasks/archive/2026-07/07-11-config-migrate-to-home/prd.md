# config.yaml 迁移至用户家目录，升级不丢配置

## Goal

打包模式下将 `config.yaml` 读写路径统一至 `~/.cloudflare-auto-task/`，与数据库同目录，用户升级时无需手动迁移任何文件。

## Requirements

- 打包模式下，`config.yaml` 读取路径改为 `~/.cloudflare-auto-task/config.yaml`
- 首次运行时若该文件不存在，自动从程序目录的 `config.example.yaml` 复制过去
- 开发模式（非打包）行为不变，仍读取项目根目录的 `config.yaml`
- `build.py` 生成的 macOS / Windows 启动器同步更新首次初始化逻辑

## Acceptance Criteria

- [ ] 打包后程序从 `~/.cloudflare-auto-task/config.yaml` 读取配置
- [ ] 首次启动时若该文件不存在自动创建
- [ ] 开发模式读取项目根目录 `config.yaml` 不变
- [ ] 启动器脚本不再向程序目录写 `config.yaml`
