# Backend Development Guidelines

> Best practices for backend development in this project.

---

## Overview

This directory contains guidelines for backend development. Fill in each file with your project's specific conventions.

> **改造说明**：本项目由 Cloudflare 自动化项目改造而来，现已支持多平台——
> 目标站点由适配器决定（首个实现是 https://opencode.ai）。读任何指南前先看
> [Multi-Platform](./multi-platform-guidelines.md)，它定义了「什么按平台隔离、
> 什么保持全局」，是其余指南的前提。
>
> `src/browser/driver.py` 里 5100 行 Cloudflare 遗留实现已删除（2026-08-03），
> 该文件现在只放站点无关的浏览器基建。仍描述 Cloudflare 专属细节、待按实际站点
> 复核的文档：Captcha（Turnstile 部分）、Email Verification（Cloudflare 登录码/2FA）、
> Browser Profile 中 `dash.cloudflare.com` 相关的 SPA 白屏说明。

---

## Guidelines Index

| Guide | Description | Status |
|-------|-------------|--------|
| [Multi-Platform](./multi-platform-guidelines.md) | Adapter layering, what is isolated per platform vs global, adding a platform | Active |
| [Directory Structure](./directory-structure.md) | Module organization and file layout | To fill |
| [Database Guidelines](./database-guidelines.md) | SQLite, identity/platform split, migrations, row-name pitfalls | Active |
| [Concurrency Guidelines](./concurrency-guidelines.md) | Worker pool, the three exclusions, thread-binding constraints | Active |
| [Browser Profile](./browser-profile-guidelines.md) | Profile hygiene, the white-screen failure, what is safe to delete | Active |
| [AdsPower](./adspower-guidelines.md) | Fingerprint-browser mode: 12-profile quota, reclaim, eager release on delete, proxy binding, CDP takeover | Active |
| [Captcha](./captcha-guidelines.md) | Token delivery to React, Turnstile vs hCaptcha APIs, anti-detection constraints | Active |
| [Email Verification](./email-verification-guidelines.md) | mail.tm 收码、验证码长度、旧码时间闸门、2FA 登录 | Active |
| [Error Handling](./error-handling.md) | Error types, handling strategies | To fill |
| [Quality Guidelines](./quality-guidelines.md) | Code standards, forbidden patterns | To fill |
| [Logging Guidelines](./logging-guidelines.md) | Structured logging, log levels | To fill |

## Related Specs

| Spec | Description |
|------|-------------|
| [API Layer](../api/index.md) | Flask routes, pagination contract, Excel export |
| [Frontend](../frontend/index.md) | Vue 3 architecture, page patterns |

---

## How to Fill These Guidelines

For each guideline file:

1. Document your project's **actual conventions** (not ideals)
2. Include **code examples** from your codebase
3. List **forbidden patterns** and why
4. Add **common mistakes** your team has made

The goal is to help AI assistants and new team members understand how YOUR project works.

---

**Language**: All documentation should be written in **English**.
