"""hCaptcha 点击式求解器（同步版，移植自 multibot-solver/hcaptcha-click-solver）。

与 2captcha token 注入方案的根本区别：**不注入 token**，而是**真实点击**过 hCaptcha 挑战——
把弹出的可见挑战帧（`#frame=challenge` / `.task-grid` / canvas）截图交给 Multibot 做图像分类，
按返回答案用拟人贝塞尔轨迹点击对应格子/坐标、再点提交。挑战解掉后 hCaptcha 内部状态完成、
Stripe 自己的 execute() 流程即随之推进——无需注入、无 sitekey 不匹配问题、全程在 Patchright（stealth）里。

契合 Stripe invisible enterprise：无复选框（提交时才弹挑战），本求解器找不到复选框不影响，
只要挑战帧一出现就按图点击求解。

依赖：requests（Multibot HTTP）+ Patchright sync page。原版用 async+humanMove API，这里改同步、
鼠标轨迹用本地贝塞尔（丢掉 humanMove 以降复杂度/延迟）。
"""
import base64
import json
import math
import random
import time

import requests

_BASE = "https://api.multibot.in"


# ---------------- Multibot API（同步，requests） ----------------

def _create_task(api_key, task_payload, task_type="hCaptchaBase64", timeout=30):
    """提交 Multibot 任务，返回 taskId 或 None。"""
    try:
        r = requests.post(f"{_BASE}/createTask/index.php",
                          data=json.dumps({"clientKey": api_key, "type": task_type,
                                           "task": task_payload}),
                          headers={"Content-Type": "application/json"}, timeout=timeout)
        d = r.json()
    except Exception as e:
        print(f"  [click-solver] createTask 失败: {str(e)[:100]}", flush=True)
        return None
    if d.get("errorId"):
        print(f"  [click-solver] createTask 错误[{d.get('errorCode')}]: {d.get('errorDescription')}", flush=True)
        return None
    return d.get("taskId")


def _wait_result(api_key, task_id, max_wait=25.0, poll=1.5):
    """轮询 Multibot 结果，返回 answers 或 None。"""
    elapsed = 0.0
    while elapsed <= max_wait:
        try:
            r = requests.post(f"{_BASE}/getTaskResult/index.php",
                              data=json.dumps({"clientKey": api_key, "taskId": task_id}),
                              headers={"Content-Type": "application/json"}, timeout=30)
            d = r.json()
        except Exception:
            return None
        if d.get("errorId"):
            return None
        st = d.get("status")
        if st == "ready":
            return d.get("answers")
        if st == "failed":
            print("  [click-solver] Multibot 求解失败", flush=True)
            return None
        time.sleep(poll)
        elapsed += poll
    print(f"  [click-solver] 等结果超时（{max_wait}s）", flush=True)
    return None


# ---------------- 拟人鼠标轨迹（同步贝塞尔） ----------------

def _bezier_path(sx, sy, ex, ey, steps=None):
    dist = math.dist((sx, sy), (ex, ey))
    n = steps or max(12, min(45, int(dist / 12)))
    scale = max(dist * 0.25, 40)
    ang = math.atan2(ey - sy, ex - sx) + random.uniform(-0.9, 0.9)
    c1x, c1y = sx + math.cos(ang) * scale, sy + math.sin(ang) * scale
    c2x, c2y = ex - math.cos(ang) * scale, ey - math.sin(ang) * scale
    pts = []
    for i in range(1, n + 1):
        t = i / n
        x = ((1-t)**3*sx + 3*(1-t)**2*t*c1x + 3*(1-t)*t*t*c2x + t**3*ex)
        y = ((1-t)**3*sy + 3*(1-t)**2*t*c1y + 3*(1-t)*t*t*c2y + t**3*ey)
        j = min(6, max(1.2, dist / 60))
        pts.append((x + random.uniform(-j, j), y + random.uniform(-j, j)))
    pts.append((ex, ey))
    return pts


class _Mouse:
    """同步拟人鼠标：贝塞尔移动 + 点击 + 拖拽。"""
    def __init__(self, page):
        self.page = page
        self.pos = None

    def move(self, x, y):
        sx, sy = self.pos or (x - 50, y - 50)
        for px, py in _bezier_path(sx, sy, x, y):
            try:
                self.page.mouse.move(px, py, steps=1)
            except Exception:
                break
        self.pos = (x, y)

    def click(self, x, y):
        self.move(x, y)
        time.sleep(random.uniform(0.04, 0.12))
        try:
            self.page.mouse.down(); self.page.mouse.up()
        except Exception:
            pass

    def drag(self, sx, sy, ex, ey):
        self.move(sx, sy)
        try:
            self.page.mouse.down()
            for px, py in _bezier_path(sx, sy, ex, ey, steps=35):
                self.page.mouse.move(px, py, steps=1)
            self.page.mouse.up()
        except Exception:
            pass
        self.pos = (ex, ey)


# ---------------- 求解器 ----------------

class HCaptchaClickSolver:
    """同步 hCaptcha 点击求解器。用法：
        solver = HCaptchaClickSolver(driver.page, api_key)
        ok = solver.solve()   # True=拿到 token / 挑战已解
    driver.page 为 Patchright sync Page。
    """

    def __init__(self, page, api_key, attempt=8):
        self.page = page
        self.api_key = api_key
        self.attempt = attempt
        self.mouse = _Mouse(page)

    # --- 帧定位 ---
    def _challenge_frame(self):
        for fr in self.page.frames:
            if "#frame=challenge" in (fr.url or ""):
                try:
                    if fr.query_selector("h2.prompt-text") or fr.query_selector(".prompt-text"):
                        return fr
                except Exception:
                    continue
        return None

    def _checkbox_frame(self):
        for fr in self.page.frames:
            if "#frame=checkbox" in (fr.url or ""):
                return fr
        return None

    def _get_token(self):
        """从任一 hcaptcha 帧取 getResponse() token；有非空即视为已过。"""
        for fr in self.page.frames:
            u = (fr.url or "").lower()
            if "hcaptcha" not in u and "stripecdn" not in u:
                continue
            try:
                tok = fr.evaluate(
                    "() => { try { return (typeof hcaptcha!=='undefined' && hcaptcha) ? "
                    "hcaptcha.getResponse() : null; } catch(e){ return null; } }")
                if tok and str(tok).strip():
                    return str(tok)
            except Exception:
                continue
        return None

    # --- 交互 ---
    def _click_checkbox(self):
        fr = self._checkbox_frame()
        if not fr:
            return False
        try:
            cb = fr.query_selector("#checkbox")
            if not cb:
                return False
            if cb.get_attribute("aria-checked") == "true":
                return True
            box = cb.bounding_box()
            if not box:
                return False
            self.mouse.click(box["x"] + box["width"]/2, box["y"] + box["height"]/2)
            time.sleep(0.8)
            return True
        except Exception:
            return False

    def _el_b64(self, el, quality=90):
        try:
            data = el.screenshot(type="jpeg", quality=quality, animations="disabled")
            return base64.b64encode(data).decode("ascii")
        except Exception:
            return None

    def _collect(self, frame):
        """采集挑战数据（question + grid/canvas 截图 + examples）→ Multibot task payload。"""
        try:
            question = frame.evaluate(
                "() => document.querySelector('.prompt-text')?.textContent?.trim() || null")
        except Exception:
            question = None
        if not question:
            return None
        # Grid（3x3）
        try:
            grid = frame.query_selector(".task-grid")
            tiles = frame.query_selector_all(".task-grid .image")
            if grid and len(tiles) == 9:
                time.sleep(1.0)
                body = self._el_b64(grid)
                if body:
                    examples = self._collect_examples(frame, ".challenge-example .image")
                    return {"question": question, "request_type": "Grid",
                            "body": body, "examples": examples}
        except Exception:
            pass
        # Canvas / Drag
        try:
            canvas = self._primary_canvas(frame)
            if canvas:
                time.sleep(1.0)
                body = self._el_b64(canvas, quality=92)
                if body:
                    has_header = frame.query_selector(".challenge-header") is not None
                    is_canvas = has_header and "drag" not in question.lower()
                    examples = self._collect_examples(frame, ".example-wrapper .image")
                    return {"question": question,
                            "request_type": "Canvas" if is_canvas else "Drag",
                            "body": body, "examples": examples}
        except Exception:
            pass
        return None

    def _collect_examples(self, frame, selector):
        out = []
        try:
            for el in frame.query_selector_all(selector):
                b = self._el_b64(el)
                if b:
                    out.append(b)
        except Exception:
            pass
        return out

    def _primary_canvas(self, frame):
        try:
            for c in frame.query_selector_all("canvas"):
                box = c.bounding_box()
                if box and box["width"] >= 100 and box["height"] >= 100:
                    return c
        except Exception:
            pass
        return None

    def _apply(self, frame, request_type, answers):
        """按 Multibot 答案点击。answers：Grid=索引列表；Canvas=坐标点；Drag=坐标对。
        也兼容 dict{actions:[...]}（含 path/坐标）——从简：只处理索引/坐标两种主流格式。"""
        try:
            # dict 形式：取 answers 列表
            if isinstance(answers, dict):
                answers = answers.get("answers") or answers.get("actions") or []
            if not isinstance(answers, (list, tuple)) or not answers:
                return False
            if request_type == "Grid":
                grid = frame.query_selector(".task-grid")
                tiles = grid.query_selector_all(".image, .task") if grid else []
                for idx in answers:
                    try:
                        i = int(idx)
                    except Exception:
                        continue
                    if 0 <= i < len(tiles):
                        box = tiles[i].bounding_box()
                        if box:
                            self.mouse.click(box["x"]+box["width"]/2+random.uniform(-10,10),
                                             box["y"]+box["height"]/2+random.uniform(-10,10))
                return True
            if request_type == "Canvas":
                canvas = self._primary_canvas(frame)
                box = canvas.bounding_box() if canvas else None
                if not box:
                    return False
                for p in answers:
                    if isinstance(p, (list, tuple)) and len(p) >= 2:
                        self.mouse.click(box["x"]+float(p[0]), box["y"]+float(p[1]))
                return True
            if request_type == "Drag":
                canvas = self._primary_canvas(frame)
                box = canvas.bounding_box() if canvas else None
                if not box:
                    return False
                it = iter(answers)
                for a in it:
                    try:
                        b = next(it)
                    except StopIteration:
                        break
                    if (isinstance(a,(list,tuple)) and isinstance(b,(list,tuple))
                            and len(a)>=2 and len(b)>=2):
                        self.mouse.drag(box["x"]+float(a[0]), box["y"]+float(a[1]),
                                        box["x"]+float(b[0]), box["y"]+float(b[1]))
                return True
        except Exception:
            return False
        return False

    def _click_submit(self, frame):
        try:
            btn = frame.query_selector(".button-submit") or frame.query_selector('button[type="submit"]')
            if btn:
                box = btn.bounding_box()
                if box:
                    self.mouse.click(box["x"]+box["width"]/2, box["y"]+box["height"]/2)
        except Exception:
            pass

    def _ensure_english(self, frame):
        try:
            cur = frame.evaluate(
                "() => document.querySelector('div.display-language.button > div:nth-child(2)')?.innerText || null")
            if cur != "EN":
                frame.evaluate(
                    "() => { const o=document.querySelector('.language-selector .option:nth-child(23)'); o&&o.click(); }")
                time.sleep(0.2)
        except Exception:
            pass

    def solve(self):
        """主循环：点复选框（若有）/ 解挑战 / 取 token。返回是否成功（拿到 token）。"""
        if not (self.api_key and str(self.api_key).strip()):
            print("  [click-solver] 缺 Multibot API key", flush=True)
            return False
        for _ in range(self.attempt):
            tok = self._get_token()
            if tok:
                print(f"  [click-solver] 已拿到 hCaptcha token（len {len(tok)}）", flush=True)
                return True
            frame = self._challenge_frame()
            if frame is None:
                # 无可见挑战 → 试点复选框（invisible 下常无，无害）
                if self._click_checkbox():
                    time.sleep(0.6)
                    continue
                time.sleep(0.6)
                continue
            # 有挑战帧：采集 → Multibot → 点击 → 提交
            self._ensure_english(frame)
            payload = self._collect(frame)
            if not payload:
                self._click_submit(frame)
                time.sleep(0.8)
                continue
            print(f"  [click-solver] 挑战 [{payload['request_type']}] «{payload['question'][:40]}» → Multibot", flush=True)
            task_id = _create_task(self.api_key, payload)
            if not task_id:
                time.sleep(0.8)
                continue
            answers = _wait_result(self.api_key, task_id)
            if not answers:
                self._click_submit(frame)
                time.sleep(0.8)
                continue
            self._apply(frame, payload["request_type"], answers)
            time.sleep(0.4)
            self._click_submit(frame)
            time.sleep(1.2)
        tok = self._get_token()
        if tok:
            return True
        print("  [click-solver] 尝试用尽仍未拿到 token", flush=True)
        return False
