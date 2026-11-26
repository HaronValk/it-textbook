#!/usr/bin/env python3
"""
Автоматическая проверка глав учебника.

1) Бесплатные локальные проверки (без API): 8 разделов, иероглифы, незакрытые блоки кода,
   подразделы лабораторной, число шагов, имя снапшота, машины стенда «из будущего»,
   неизвестные IP, ссылки на будущие уроки (для L1), глубокие URL.
2) Проверка моделью DeepSeek (если не указан --local-only): забегания вперёд, технические ошибки,
   выполнимость лабораторной, расхождения со стендом. Есть ограничитель бюджета.

Примеры:
    python audit_book.py l1.txt --local-only                       # бесплатно, сразу
    python audit_book.py l1.txt --only 33-35                       # проба на 3 главах с моделью
    python audit_book.py l1.txt                                    # весь L1
    python audit_book.py l1.txt l2.txt sysadmin.txt devops.txt     # все треки (порядок важен)
    python audit_book.py l1.txt --report-only                      # пересобрать отчёт из сохранённых проверок

Результат: audit/report.md, audit/to_regenerate.txt (готовые команды для перегенерации),
audit/<трек>/NNN.json. Уже проверенные главы пропускаются (флаг --force проверит заново).
"""

import argparse
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE_URL = "https://api.deepseek.com"
ORDER = {"l1": 0, "l2": 1, "sysadmin": 2, "devops": 3}

# где машина появляется впервые: (трек, номер урока) — совпадает с stand.md
VM_FIRST = {
    "WIN11-CL01": ("l1", 3), "WIN10-CL02": ("l1", 4), "UBU01": ("l1", 5), "DC01": ("l1", 6),
    "ASTRA01": ("l1", 30), "FS01": ("l1", 46),
    "SRV1C01": ("l2", 7), "DC02": ("l2", 17), "GW01": ("l2", 30), "MON01": ("l2", 39),
    "ESXI01": ("l2", 42), "PVE01": ("l2", 45), "WEB01": ("l2", 56),
    "PVE02": ("sysadmin", 35), "PBS01": ("sysadmin", 38),
    "GIT01": ("devops", 17), "HARBOR01": ("devops", 31),
    "k8s-cp01": ("devops", 34), "k8s-w01": ("devops", 34), "k8s-w02": ("devops", 34),
}
KNOWN_IPS = (
    {f"10.10.10.{i}" for i in (0, 1, 2, 3, 10, 11, 20, 25, 30, 31, 40, 50, 51, 52, 55, 61, 62, 63, 70, 71, 72, 73, 255)}
    | {f"10.10.10.{i}" for i in range(100, 151)}
    | {"10.10.20.0", "10.10.20.1", "10.10.20.10", "10.10.20.255"}
)
LAB_SUBSECTIONS = ["Предусловия", "Снапшот", "Шаги", "Проверка", "Сломай и почини", "Что изменилось на стенде", "Откат"]
CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")

SYSTEM_AUDIT = """Ты — строгий технический рецензент и методист. Проверяешь ОДНУ главу учебника для новичка (путь L1 → L2 → сисадмин → DevOps, Москва, 2026). Критерии:
A. ЗАБЕГАНИЕ ВПЕРЁД: глава использует как готовое или требует в лабораторной ВМ, роли, домен, учётки, инструменты, команды, понятия, которых читатель ещё не проходил (см. списки «ПРОЙДЕНО» / «НЕ ПРОЙДЕНО» и «Хронологию стенда» в описании стенда). Допустимы: объяснение термина в одну фразу и справка «подробнее в уроке N» вне лабораторной.
B. ТЕХНИЧЕСКАЯ ДОСТОВЕРНОСТЬ: неверные или выдуманные команды, ключи, пути меню, порты, версии, поведение систем.
C. ВЫПОЛНИМОСТЬ ЛАБОРАТОРНОЙ: можно ли выполнить шаги на стенде в состоянии «перед уроком» по хронологии; конфликты имён, IP, пула DHCP, снапшотов; шаги, у которых ожидаемый результат невозможен.
D. СТЕНД И ОФОРМЛЕНИЕ: расхождения с описанием стенда; иероглифы, плейсхолдеры, оборванный текст.
Правила: сообщай только о том, в чём достаточно уверен; сомнительное помечай confidence=low; не выдумывай замечания ради количества; цитаты короче 120 символов. Верни ТОЛЬКО JSON без пояснений и без markdown-обёртки:
{"verdict":"ok|minor|major","forward_refs":[{"quote":"","needs":"","fix":""}],"tech_errors":[{"quote":"","issue":"","confidence":"high|medium|low","fix":""}],"lab_blockers":[{"step":"","issue":"","fix":""}],"stand_mismatches":[{"quote":"","issue":"","fix":""}],"formatting":[{"quote":"","issue":""}],"summary":""}
major — лабораторная невыполнима или есть серьёзные ошибки/забегания; minor — мелкие неточности; ok — замечаний нет."""


# ---------- разбор списков и глав ----------

def parse_lessons(path):
    lessons, module = [], "Без модуля"
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("## "):
            module = line[3:].strip()
            continue
        m = re.match(r"^(\d+)\.\s+(.+)$", line)
        if m:
            lessons.append({"n": int(m.group(1)), "title": m.group(2).strip(), "module": module})
    return lessons


def parse_only(spec):
    out = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            out.update(range(int(a), int(b) + 1))
        elif part:
            out.add(int(part))
    return out


def chapter_path(book, track, n):
    found = sorted((Path(book) / track).glob(f"{n:03d}-*.md"))
    return found[0] if found else None


def split_sections(text):
    secs, head, buf, in_fence = [], "(начало)", [], False
    for ln in text.split("\n"):
        if ln.lstrip().startswith("```"):
            in_fence = not in_fence
        m = None if in_fence else re.match(r"^#{2,3}\s+(.*)$", ln)
        if m:
            secs.append((head, "\n".join(buf)))
            head, buf = m.group(1).strip(), []
        else:
            buf.append(ln)
    secs.append((head, "\n".join(buf)))
    return secs


def short_title(t):
    return t.split(":")[0].strip()


# ---------- локальные (бесплатные) проверки ----------

def local_checks(track, n, text):
    f = []  # (severity, message)
    heads = re.findall(r"^##\s*(\d)\.", text, re.M)
    missing = [i for i in range(1, 9) if str(i) not in heads]
    if missing:
        f.append(("error", f"нет разделов: {missing}"))
    words = len(text.split())
    if words < 1200:
        f.append(("warn", f"короткая глава: {words} слов"))
    m = CJK_RE.search(text)
    if m:
        a = max(0, m.start() - 25)
        f.append(("error", "иероглифы: …" + text[a:m.end() + 25].replace("\n", " ") + "…"))
    fences = sum(1 for ln in text.split("\n") if ln.lstrip().startswith("```"))
    if fences % 2:
        f.append(("error", "нечётное число ``` (блок кода не закрыт)"))
    marks = text.count("[ПРОВЕРИТЬ]")
    if marks:
        f.append(("info", f"пометок [ПРОВЕРИТЬ]: {marks}"))
    deep = [u for u in re.findall(r"https?://[^\s)>\]`]+", text) if re.sub(r"^https?://[^/]+/?", "", u)]
    if deep:
        f.append(("warn", f"глубокие URL (могут быть выдуманы): {len(deep)}, например {deep[0][:80]}"))
    if re.search(r"\d+\.\d+\.[хХxX]\.[хХxX]", text):
        f.append(("warn", "плейсхолдер в адресе (например 192.168.х.х)"))

    secs = split_sections(text)
    h3 = " | ".join(h.lower() for h, _ in secs)
    absent = [s for s in LAB_SUBSECTIONS if s.lower() not in h3]
    if absent:
        f.append(("warn", f"нет подразделов лабораторной: {', '.join(absent)}"))
    for h, body in secs:
        if h.lower().startswith("шаги"):
            steps = len(re.findall(r"^\s{0,3}\d+\.\s+", body, re.M))
            if steps < 8:
                f.append(("warn", f"в «Шаги» только {steps} нумерованных шагов (нужно 8+)"))

    snap_ok = f"before-{track}-{n:03d}"
    if "лаборатор" in text.lower() and snap_ok not in text:
        f.append(("warn", f"нет снапшота {snap_ok}"))
    for lvl, num in set(re.findall(r"before-([a-z0-9]+)-(\d{3})", text)):
        if lvl == track and int(num) > n:
            f.append(("warn", f"снапшот из будущего: before-{lvl}-{num}"))

    core = [(h, b) for h, b in secs if re.match(r"(шаги|проверка|снапшот|предусловия|практика)", h.lower())]
    for vm, first in VM_FIRST.items():
        if (ORDER[first[0]], first[1]) > (ORDER[track], n):
            hits = [h for h, b in core if re.search(rf"(?<![\w-]){re.escape(vm)}(?![\w-])", b)]
            if hits:
                sev = "info" if all(h.lower().startswith("предусловия") for h in hits) else "warn"
                f.append((sev, f"машина {vm} (появится в {first[0]} №{first[1]}) упомянута в: {', '.join(hits)}"))

    for h, b in secs:
        if h.lower().startswith("сломай"):
            continue
        for ip in sorted(set(re.findall(r"\b10\.10\.\d{1,3}\.\d{1,3}\b", b))):
            if ip not in KNOWN_IPS:
                f.append(("warn", f"неизвестный адрес {ip} в «{h[:30]}»"))
    if track == "l1":
        refs = set()
        for a, b in re.findall(r"урок(?:а|е|у|ов|ах|ам)?\s+(?:№\s*)?(\d+)(?:\s*[–-]\s*(\d+))?", text, re.I):
            refs.update(int(x) for x in (a, b) if x)
        fwd = sorted(r for r in refs if r > n)
        if fwd:
            f.append(("info", f"ссылки на будущие уроки: {fwd}"))
    return [{"sev": s, "msg": m} for s, m in f]


# ---------- проверка моделью ----------

class Budget:
    def __init__(self, limit, price_in, price_out):
        self.limit, self.pi, self.po, self.spent = limit, price_in, price_out, 0.0
        self.lock = threading.Lock()

    def add(self, pt, ct):
        with self.lock:
            self.spent += pt * self.pi / 1e6 + ct * self.po / 1e6
            return self.spent

    def exhausted(self):
        with self.lock:
            return self.spent >= self.limit


def build_user_prompt(chain, track, lesson, text, stand):
    prior_tracks = [(t, ls) for t, ls in chain if ORDER[t] < ORDER[track]]
    cur = dict(chain)[track]
    parts = []
    for t, ls in prior_tracks:
        parts.append(f"Трек {t} пройден полностью:\n" + "; ".join(f"{x['n']}. {short_title(x['title'])}" for x in ls))
    parts.append(f"ПРОЙДЕНО в треке {track}:\n" + ("\n".join(
        f"- {x['n']}. {short_title(x['title'])}" for x in cur if x["n"] < lesson["n"]) or "- (первый урок)"))
    parts.append(f"НЕ ПРОЙДЕНО в треке {track}:\n" + ("\n".join(
        f"- {x['n']}. {short_title(x['title'])}" for x in cur if x["n"] > lesson["n"]) or "- (последний урок)"))
    return (f"Трек: {track}. Урок {lesson['n']}: {lesson['title']}\n\n"
            + "\n\n".join(parts)
            + f"\n\n=== ОПИСАНИЕ СТЕНДА И ХРОНОЛОГИЯ ===\n{stand}\n\n=== ГЛАВА ДЛЯ ПРОВЕРКИ ===\n{text}")


def extract_json(s):
    a, b = s.find("{"), s.rfind("}")
    if a < 0 or b < a:
        raise ValueError("в ответе нет JSON")
    return json.loads(s[a:b + 1])


def call_llm(client, args, budget, system, user):
    last = None
    cap = args.max_tokens
    for attempt in range(1, 4):
        if budget.exhausted():
            raise RuntimeError("бюджет исчерпан")
        try:
            kw = dict(model=args.model, max_tokens=cap,
                      messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
            if args.thinking == "off":
                kw["temperature"] = 0.1
                kw["extra_body"] = {"thinking": {"type": "disabled"}}
            else:
                kw["reasoning_effort"] = args.thinking
                kw["extra_body"] = {"thinking": {"type": "enabled"}}
            resp = client.chat.completions.create(**kw)
            u = getattr(resp, "usage", None)
            spent = budget.add(getattr(u, "prompt_tokens", 0) or 0, getattr(u, "completion_tokens", 0) or 0)
            data = extract_json(resp.choices[0].message.content or "")
            data["_spent_total"] = round(spent, 4)
            return data
        except Exception as e:
            last = e
            if getattr(e, "status_code", None) == 400 and "max_tokens" in str(e) and cap > 8192:
                cap = 8192
                continue
            time.sleep(3 * 2 ** attempt)
    raise RuntimeError(f"не удалось получить проверку: {last}")


# ---------- отчёт ----------

def ranges(nums):
    nums, out = sorted(set(nums)), []
    i = 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        out.append(str(nums[i]) if i == j else f"{nums[i]}-{nums[j]}")
        i = j + 1
    return ",".join(out)


def needs_regen(d):
    llm = d.get("llm") or {}
    local_err = any(x["sev"] == "error" for x in d.get("local", []))
    high = any(e.get("confidence") == "high" for e in llm.get("tech_errors", []))
    return local_err or llm.get("verdict") == "major" or bool(llm.get("forward_refs")) or bool(llm.get("lab_blockers")) or high


def build_report(out_dir, tracks, titles, spent):
    lines = ["# Отчёт проверки учебника\n", f"Потрачено на проверку моделью: ≈ ${spent:.2f}\n"]
    regen = {}
    summary = ["| Трек | проверено | ok | minor | major | без LLM | ошибки локальных проверок |", "|---|---|---|---|---|---|---|"]
    details = []
    for t in tracks:
        files = sorted((out_dir / t).glob("*.json"))
        c = {"ok": 0, "minor": 0, "major": 0, "none": 0}
        errs = 0
        for fp in files:
            d = json.loads(fp.read_text(encoding="utf-8"))
            n = int(fp.stem)
            llm = d.get("llm")
            v = (llm or {}).get("verdict", "none") if llm else "none"
            c[v if v in c else "none"] += 1
            le = [x for x in d.get("local", []) if x["sev"] == "error"]
            errs += bool(le)
            if needs_regen(d):
                regen.setdefault(t, []).append(n)
            items = []
            for x in d.get("local", []):
                if x["sev"] in ("error", "warn"):
                    items.append(f"- {'⛔' if x['sev'] == 'error' else '⚠'} локально: {x['msg']}")
            for k, icon, label in (("forward_refs", "⏩", "забегание вперёд"), ("lab_blockers", "🧪", "лабораторная"),
                                   ("tech_errors", "🔧", "техника"), ("stand_mismatches", "🏗", "стенд")):
                for e in (llm or {}).get(k, []):
                    if k == "tech_errors" and e.get("confidence") == "low":
                        continue
                    txt = e.get("issue") or e.get("needs") or ""
                    items.append(f"- {icon} {label}: «{e.get('quote') or e.get('step', '')}» — {txt}. Исправить: {e.get('fix', '')}")
            if items or v in ("major", "minor"):
                details.append((0 if v == "major" else 1, t, n, v, items, (llm or {}).get("summary", "")))
        summary.append(f"| {t} | {len(files)} | {c['ok']} | {c['minor']} | {c['major']} | {c['none']} | {errs} |")
    lines += summary + ["\n## Главы с замечаниями\n"]
    for _, t, n, v, items, summ in sorted(details):
        lines.append(f"### {t}/{n:03d} — {titles.get((t, n), '')} [{v}]")
        if summ:
            lines.append(f"_{summ}_")
        lines += items + [""]
    (out_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    cmds = [f"python gen_lessons.py {t}.txt --level {t} --only {ranges(ns)} --force" for t, ns in regen.items()]
    (out_dir / "to_regenerate.txt").write_text("\n".join(cmds) + ("\n" if cmds else ""), encoding="utf-8")
    return regen


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser(description="Автоматическая проверка глав учебника")
    ap.add_argument("lists", nargs="+", help="файлы со списками уроков в порядке треков: l1.txt l2.txt sysadmin.txt devops.txt")
    ap.add_argument("--track", default=None, help="проверять только этот трек (l1, l2, sysadmin, devops)")
    ap.add_argument("--only", default="", help="только эти номера, например 1-10 или 5,7")
    ap.add_argument("--book", default="book")
    ap.add_argument("--out", default="audit")
    ap.add_argument("--stand", default=str(HERE / "stand.md"))
    ap.add_argument("--local-only", action="store_true", help="только бесплатные локальные проверки")
    ap.add_argument("--report-only", action="store_true", help="только пересобрать отчёт")
    ap.add_argument("--force", action="store_true", help="перепроверить, даже если результат есть")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--model", default="deepseek-flash")
    ap.add_argument("--thinking", choices=["off", "low", "high", "max"], default="high")
    ap.add_argument("--max-tokens", type=int, default=16000)
    ap.add_argument("--budget", type=float, default=3.0, help="потолок расходов, $ (по умолчанию 3.0)")
    ap.add_argument("--price-in", type=float, default=0.14, help="$ за 1 млн входных токенов")
    ap.add_argument("--price-out", type=float, default=0.28, help="$ за 1 млн выходных токенов")
    args = ap.parse_args()

    chain = []
    for p in args.lists:
        stem = Path(p).stem
        if stem not in ORDER:
            sys.exit(f"Имя файла {p} должно быть l1/l2/sysadmin/devops.txt")
        chain.append((stem, parse_lessons(p)))
    chain.sort(key=lambda x: ORDER[x[0]])
    titles = {(t, x["n"]): x["title"] for t, ls in chain for x in ls}
    tracks = [t for t, _ in chain if not args.track or t == args.track]
    out_dir = Path(args.out)
    only = parse_only(args.only) if args.only else None
    budget = Budget(args.budget, args.price_in, args.price_out)

    if not args.report_only:
        client = None
        if not args.local_only:
            key = os.environ.get("DEEPSEEK_API_KEY")
            if not key:
                sys.exit("Не задан DEEPSEEK_API_KEY (или используй --local-only)")
            from openai import OpenAI
            client = OpenAI(api_key=key, base_url=BASE_URL, timeout=900)
        stand = Path(args.stand).read_text(encoding="utf-8") if Path(args.stand).exists() else ""
        jobs = []
        for t, ls in chain:
            if t not in tracks:
                continue
            (out_dir / t).mkdir(parents=True, exist_ok=True)
            for lesson in ls:
                if only and lesson["n"] not in only:
                    continue
                cp = chapter_path(args.book, t, lesson["n"])
                if cp:
                    jobs.append((t, lesson, cp))
                else:
                    print(f"нет файла главы: {t} №{lesson['n']}")

        def work(job):
            t, lesson, cp = job
            fp = out_dir / t / f"{lesson['n']:03d}.json"
            text = cp.read_text(encoding="utf-8")
            data = json.loads(fp.read_text(encoding="utf-8")) if fp.exists() else {}
            data["local"] = local_checks(t, lesson["n"], text)
            if client and (args.force or not data.get("llm")):
                if budget.exhausted():
                    fp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
                    return t, lesson, "пропущено: бюджет"
                try:
                    data["llm"] = call_llm(client, args, budget, SYSTEM_AUDIT,
                                           build_user_prompt(chain, t, lesson, text, stand))
                except Exception as e:
                    fp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
                    return t, lesson, f"ошибка: {e}"
            fp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            v = (data.get("llm") or {}).get("verdict", "-")
            return t, lesson, f"{v}; локально: " + ", ".join(sorted({x['sev'] for x in data['local']})) if data["local"] else f"{v}; локально чисто"

        print(f"Глав к проверке: {len(jobs)}. Бюджет: ${args.budget}", flush=True)
        with ThreadPoolExecutor(max_workers=1 if args.local_only else max(1, args.workers)) as pool:
            for fut in as_completed([pool.submit(work, j) for j in jobs]):
                t, lesson, res = fut.result()
                print(f"{t}/{lesson['n']:03d} {short_title(lesson['title'])[:40]}: {res} (потрачено ${budget.spent:.3f})", flush=True)

    regen = build_report(out_dir, tracks, titles, budget.spent)
    print(f"\nОтчёт: {out_dir / 'report.md'}")
    if regen:
        print("Рекомендуется перегенерировать (команды в to_regenerate.txt):")
        for t, ns in regen.items():
            print(f"  {t}: {ranges(ns)}")


if __name__ == "__main__":
    main()
