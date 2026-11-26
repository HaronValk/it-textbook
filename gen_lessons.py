#!/usr/bin/env python3
"""
Генератор уроков учебника по списку названий через DeepSeek API.

Рядом со скриптом должны лежать:
    master_prompt.md   — мастер-промпт (правила, структура главы)
    stand.md           — описание сквозного учебного стенда

Подготовка:
    pip install openai
    export DEEPSEEK_API_KEY="sk-..."     # Windows PowerShell: $env:DEEPSEEK_API_KEY="sk-..."

Примеры:
    python gen_lessons.py l1.txt --level l1 --dry-run --only 34     # показать итоговый промпт, ничего не отправляя
    python gen_lessons.py l1.txt --level l1 --only 1-3              # пробный прогон
    python gen_lessons.py l1.txt --level l1                         # весь трек
    python gen_lessons.py devops.txt --level devops --workers 4
    python gen_lessons.py --list-models                             # какие модели видит твой ключ
    python gen_lessons.py l1.txt --level l1 --model deepseek-v4-pro --thinking off

Запас токенов: скрипт просит большой лимит ответа и сам снижает его до максимума модели;
если глава всё равно обрезана (finish_reason=length), он просит модель продолжить и склеивает куски.

Результат: book/<level>/NNN-название.md и book/<level>/index.md.
Готовые уроки пропускаются, так что после обрыва просто запусти команду ещё раз.
"""

import argparse
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"  # мощнее и дороже: deepseek-v4-pro (через --model)
RETRIES = 4
DEFAULT_MAX_TOKENS = (
    32000  # просим с большим запасом; если API не даёт столько, лимит подберётся сам
)
MAX_CONTINUATIONS = 5  # сколько раз «доберём» главу, если ответ обрезался по лимиту
CONTINUE_PROMPT = (
    "Продолжи главу ровно с того места, где остановился. Не повторяй уже написанное, "
    "не добавляй вступлений и пояснений, не начинай заново. Сохрани структуру и дойди "
    "до конца раздела 8."
)
_LEARNED_CAPS = {}  # модель -> максимум токенов, который реально разрешил API
_caps_lock = threading.Lock()
HERE = Path(__file__).resolve().parent

TRACK_NAMES = {
    "l1": "L1 Helpdesk",
    "l2": "L2 Инженер поддержки",
    "sysadmin": "Системный администратор",
    "devops": "DevOps-инженер",
}

LEVELS = {
    "l1": (
        "Трек L1 Helpdesk / Service Desk. Объясняй с нуля, ничего не предполагай. "
        "Фокус: диагностика по шагам, работа с заявками, общение с пользователем, "
        "уверенное выполнение типовых действий. Глубина: «понять и уверенно сделать», "
        "внутренности только в том объёме, который нужен для диагностики. "
        "В разделе 4 минимум 8 команд, вопросов на собеседовании 3–5."
    ),
    "l2": (
        "Трек L2 Инженер поддержки. Читатель уже прошёл L1: знает основы Windows, Linux, сетей и AD, "
        "умеет работать с заявками. Не повторяй азы, дай только краткое напоминание. "
        "Фокус: глубокая диагностика, поиск корневой причины, администрирование сервисов, "
        "логи и производительность, автоматизация рутины. "
        "В разделе 4 минимум 10 команд или приёмов, вопросов на собеседовании 4–5."
    ),
    "sysadmin": (
        "Трек Системный администратор (Middle/Senior). Читатель прошёл L1 и L2. "
        "Фокус: проектирование, отказоустойчивость, безопасность, регламенты, миграции, "
        "автоматизация, документирование, соответствие требованиям РФ. "
        "Не объясняй базу, говори на языке инженера, но термины новых областей расшифровывай. "
        "В разделе 4 минимум 10 команд или приёмов, вопросов на собеседовании 5."
    ),
    "devops": (
        "Трек DevOps-инженер. Читатель прошёл сисадминскую базу (Linux, сети, виртуализация). "
        "Фокус: автоматизация, контейнеры, CI/CD, IaC, наблюдаемость, надёжность, DevSecOps. "
        "Всё, что можно, оформляй как код и воспроизводимые конфигурации в Git. "
        "В разделе 4 минимум 10 команд или фрагментов конфигурации, вопросов на собеседовании 5."
    ),
}

USER_TEMPLATE = """Трек: {track}
Уровень: {level}
Модуль: {module}
Номер урока: {n}
Тема урока: {title}
Имя снапшота для лабораторной: before-{level}-{n:03d}
Предыдущие треки: {prereq}

ПРОЙДЕНО в этом треке (опирайся только на это):
{previous}

НЕ ПРОЙДЕНО (будущие уроки этого трека; не используй их содержимое и созданные в них ВМ, роли и сервисы как готовые):
{future}

Состояние стенда перед этим уроком вычисли по «Хронологии стенда» из системного промпта.

Напиши главу по теме «{title}» строго по структуре из системного промпта.
ВНИМАНИЕ: Используй ТОЛЬКО то, что есть в списке «ПРОЙДЕНО». Не используй ВМ, роли и домен из «НЕ ПРОЙДЕНО». Если нужна машина из будущего — используй ту, что уже есть, или оставь теорией. Это строгое правило."""


PREREQ = {
    "l1": "нет (трек для новичка)",
    "l2": "пройден весь трек L1",
    "sysadmin": "пройдены треки L1 и L2",
    "devops": "пройдены треки L1, L2 и сисадмин (Linux-часть гарантирована)",
}


def build_system_prompt(level: str, prompt_path: Path, stand_path: Path) -> str:
    for p in (prompt_path, stand_path):
        if not p.exists():
            sys.exit(
                f"Не найден файл {p}. Положи master_prompt.md и stand.md рядом со скриптом."
            )
    text = prompt_path.read_text(encoding="utf-8")
    text = text.replace("<<LEVEL>>", LEVELS[level]).replace(
        "<<STAND>>", stand_path.read_text(encoding="utf-8").strip()
    )
    return text


def parse_lessons(path: Path):
    lessons, module = [], "Без модуля"
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("## "):
            module = line[3:].strip()
            continue
        m = re.match(r"^(\d+)\.\s+(.+)$", line)
        if m:
            lessons.append(
                {"n": int(m.group(1)), "title": m.group(2).strip(), "module": module}
            )
    return lessons


def parse_only(spec: str):
    """'5,7,10-12' -> {5,7,10,11,12}"""
    result = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            result.update(range(int(a), int(b) + 1))
        else:
            result.add(int(part))
    return result


def file_name(lesson) -> str:
    slug = re.sub(r"[^\w]+", "-", lesson["title"].lower()).strip("-")[:50]
    return f"{lesson['n']:03d}-{slug}.md"


def clean(text: str) -> str:
    """Если модель всё же обернула ответ в ```markdown ... ``` — снимаем обёртку."""
    text = text.strip()
    text = re.sub(r"^```(?:markdown|md)?\s*\n", "", text)
    text = re.sub(r"\n```\s*$", "", text)
    return text.strip() + "\n"


def missing_sections(text: str):
    return [
        i for i in range(1, 9) if not re.search(rf"^##\s*{i}\.", text, re.MULTILINE)
    ]


def short_title(t):
    return t.split(":")[0].strip()


def build_user_prompt(track, level, lesson, all_lessons):
    prev = "\n".join(
        f"- {x['n']}. {short_title(x['title'])}"
        for x in all_lessons
        if x["n"] < lesson["n"]
    )
    fut = "\n".join(
        f"- {x['n']}. {short_title(x['title'])}"
        for x in all_lessons
        if x["n"] > lesson["n"]
    )
    return USER_TEMPLATE.format(
        track=track,
        level=level,
        module=lesson["module"],
        n=lesson["n"],
        title=lesson["title"],
        prereq=PREREQ[level],
        previous=prev or "- (это первый урок трека)",
        future=fut or "- (это последний урок)",
    )


def request_with_cap(client, model, messages, requested, thinking):
    """Один запрос. Если API отклонил max_tokens как слишком большой, узнаём его потолок и повторяем."""
    for _ in range(3):
        with _caps_lock:
            cap = min(requested or DEFAULT_MAX_TOKENS, _LEARNED_CAPS.get(model, 10**9))
        kwargs = dict(model=model, messages=messages, max_tokens=cap)
        if thinking == "off":
            kwargs["temperature"] = 0.3
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        else:  # в режиме рассуждения temperature не действует (API её игнорирует)
            kwargs["reasoning_effort"] = thinking
            kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
        try:
            return client.chat.completions.create(**kwargs)
        except Exception as e:
            text = str(e)
            if getattr(e, "status_code", None) == 400 and "max_tokens" in text:
                m = re.search(r"\[\s*1\s*,\s*(\d+)\s*\]", text)
                new_cap = int(m.group(1)) if m else 8192
                if new_cap < cap:
                    with _caps_lock:
                        _LEARNED_CAPS[model] = new_cap
                    print(
                        f"  лимит ответа для {model}: {new_cap} токенов; длинные главы буду добирать продолжениями",
                        flush=True,
                    )
                    continue
            raise
    raise RuntimeError("не удалось подобрать допустимый max_tokens")


def generate(
    client, model, system_prompt, user_prompt, lesson, max_tokens=None, thinking="high"
):
    for attempt in range(1, RETRIES + 1):
        try:
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]
            parts = []
            for part_no in range(MAX_CONTINUATIONS + 1):
                resp = request_with_cap(client, model, messages, max_tokens, thinking)
                choice = resp.choices[0]
                chunk = choice.message.content or ""
                if not chunk.strip():
                    raise ValueError(f"пустой ответ (finish={choice.finish_reason})")
                parts.append(chunk)
                if choice.finish_reason != "length" or part_no == MAX_CONTINUATIONS:
                    break
                print(
                    f"  [{lesson['n']}] ответ упёрся в лимит, продолжаю ({part_no + 1}/{MAX_CONTINUATIONS})",
                    flush=True,
                )
                messages.append({"role": "assistant", "content": chunk})
                messages.append({"role": "user", "content": CONTINUE_PROMPT})
            text = clean("".join(parts))
            miss = missing_sections(text)
            if miss:
                raise ValueError(f"нет разделов {miss}")
            return text
        except Exception as e:  # сеть, лимиты, неполный ответ
            wait = 3 * 2**attempt
            print(
                f"  [{lesson['n']}] попытка {attempt}/{RETRIES} не удалась: {e}. Жду {wait} c",
                flush=True,
            )
            time.sleep(wait)
    return None


def main():
    ap = argparse.ArgumentParser(
        description="Генератор уроков по списку названий (DeepSeek API)"
    )
    ap.add_argument("lessons_file", nargs="?", help="txt-файл со списком уроков")
    ap.add_argument("--level", choices=sorted(LEVELS), help="Уровень/трек")
    ap.add_argument(
        "--track",
        default=None,
        help="Название трека для оглавления (по умолчанию из --level)",
    )
    ap.add_argument(
        "--out", default=None, help="Папка для глав (по умолчанию book/<level>)"
    )
    ap.add_argument(
        "--only", default="", help="Только эти номера, например 1-3 или 5,7,10-12"
    )
    ap.add_argument(
        "--workers", type=int, default=3, help="Параллельных запросов (по умолчанию 3)"
    )
    ap.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help="Модель: deepseek-flash (по умолчанию) или deepseek-v4-pro",
    )
    ap.add_argument(
        "--thinking",
        choices=["off", "low", "high", "max"],
        default="high",
        help="Режим рассуждения: off — выключен (быстрее и дешевле), low/high/max — глубина (по умолчанию high, как у API)",
    )
    ap.add_argument("--prompt", default=str(HERE / "master_prompt.md"))
    ap.add_argument("--stand", default=str(HERE / "stand.md"))
    ap.add_argument(
        "--max-tokens",
        type=int,
        default=None,
        help="Лимит одного ответа в токенах (по умолчанию 32000; если API не даёт столько, скрипт сам подберёт максимум)",
    )
    ap.add_argument(
        "--list-models",
        action="store_true",
        help="Показать модели, доступные твоему ключу, и выйти",
    )
    ap.add_argument(
        "--force", action="store_true", help="Перегенерировать, даже если файл уже есть"
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Показать итоговый промпт первого урока и выйти",
    )
    args = ap.parse_args()

    if args.list_models:
        key = os.environ.get("DEEPSEEK_API_KEY")
        if not key:
            sys.exit("Не задана переменная окружения DEEPSEEK_API_KEY")
        from openai import OpenAI

        for m in OpenAI(api_key=key, base_url=BASE_URL).models.list().data:
            print(m.id)
        return
    if not args.lessons_file or not args.level:
        ap.error("нужны lessons_file и --level (или используй --list-models)")

    track = args.track or TRACK_NAMES[args.level]
    out_dir = Path(args.out or f"book/{args.level}")

    lessons = parse_lessons(Path(args.lessons_file))
    if not lessons:
        sys.exit("В файле не найдено ни одного урока (формат строки: '12. Название')")

    system_prompt = build_system_prompt(args.level, Path(args.prompt), Path(args.stand))

    only = parse_only(args.only) if args.only else None
    todo = [
        l
        for l in lessons
        if (not only or l["n"] in only)
        and (args.force or not (out_dir / file_name(l)).exists())
    ]

    if args.dry_run:
        if not todo:
            sys.exit(
                "Нечего генерировать (все выбранные уроки уже есть). Добавь --force."
            )
        print("=== SYSTEM ===\n" + system_prompt)
        print(
            "\n=== USER ===\n" + build_user_prompt(track, args.level, todo[0], lessons)
        )
        return

    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        sys.exit("Не задана переменная окружения DEEPSEEK_API_KEY")

    from openai import (
        OpenAI,
    )  # импорт здесь, чтобы --dry-run работал без установленного пакета

    client = OpenAI(api_key=api_key, base_url=BASE_URL, timeout=900)

    by_num = {l["n"]: build_user_prompt(track, args.level, l, lessons) for l in todo}
    out_dir.mkdir(parents=True, exist_ok=True)
    print(
        f"Всего в списке: {len(lessons)}. К генерации: {len(todo)}. Папка: {out_dir}",
        flush=True,
    )

    failed = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {
            pool.submit(
                generate,
                client,
                args.model,
                system_prompt,
                by_num[l["n"]],
                l,
                args.max_tokens,
                args.thinking,
            ): l
            for l in todo
        }
        for fut in as_completed(futures):
            l = futures[fut]
            text = fut.result()
            if text is None:
                failed.append(l)
                print(f"✗ {l['n']}. {l['title']}", flush=True)
                continue
            (out_dir / file_name(l)).write_text(text, encoding="utf-8")
            flags = text.count("[ПРОВЕРИТЬ]")
            note = f"  (пометок [ПРОВЕРИТЬ]: {flags})" if flags else ""
            print(f"✓ {l['n']}. {l['title']}{note}", flush=True)

    # оглавление
    lines = [f"# {track}: оглавление\n"]
    current = None
    for l in lessons:
        if l["module"] != current:
            current = l["module"]
            lines.append(f"\n## {current}\n")
        f = file_name(l)
        if (out_dir / f).exists():
            lines.append(f"- [{l['n']}. {l['title']}]({f})")
        else:
            lines.append(f"- {l['n']}. {l['title']} _(ещё не сгенерирован)_")
    (out_dir / "index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    if failed:
        (out_dir / "failed.txt").write_text(
            "\n".join(f"{l['n']}. {l['title']}" for l in failed), encoding="utf-8"
        )
        print(
            f"\nНе удалось: {len(failed)} (список в failed.txt). Запусти команду ещё раз: готовые пропустятся."
        )
    else:
        print("\nГотово.")


if __name__ == "__main__":
    main()
