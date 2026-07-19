# Backend Development Guidelines

> Best practices for backend development in this project.

---

## Overview

This directory contains guidelines for backend development. Fill in each file with your project's specific conventions.

---

## Guidelines Index

| Guide | Description | Status |
|-------|-------------|--------|
| [Directory Structure](./directory-structure.md) | Module organization and file layout | To fill |
| [Database Guidelines](./database-guidelines.md) | SQLite, models, pagination queries | Active |
| [Concurrency Guidelines](./concurrency-guidelines.md) | Worker pool, the three exclusions, thread-binding constraints | Active |
| [Browser Profile](./browser-profile-guidelines.md) | Profile hygiene, the white-screen failure, what is safe to delete | Active |
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
