#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Avito Business Scraper — Скрейпер объявлений "Готовый бизнес" с Avito (Краснодар)

Использует два LLM-субагента:
1. Классификация продавца (брокер/владелец) на основе истории объявлений
2. Анализ карточки бизнеса (извлечение финансовых данных)

Зависимости: playwright, playwright-stealth, beautifulsoup4, httpx, rich, lxml

Автор: AI Assistant
"""

import argparse
import csv
import json
import logging
import os
import random
import re
import signal
import sys
import threading
import time
# concurrent.futures убран — Playwright sync API несовместим с ThreadPoolExecutor
from datetime import date, datetime, timedelta
from typing import Optional
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from playwright_stealth import Stealth

# Глобальный экземпляр stealth
_stealth_instance = Stealth()
from rich.console import Console
from rich.table import Table
from rich.logging import RichHandler
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeRemainingColumn

# ===================== CONFIG =====================
PROXY_API_KEY = "sk-xDCfQfiMv89R6RzI6o144zv6kxrXGT8O"
BASE_URL = "https://api.proxyapi.ru/openai/v1"
MODEL_NAME = "gpt-5-nano-2025-08-07"
SEARCH_URL = "https://www.avito.ru/krasnodar/gotoviy_biznes?cd=1&f=ASgBAgICAUTw3w~C~fUC"
PRICE_MIN = 700_000
BROKERS_LIST = [
    "Мэлс", "Brokerin", "Витрина Бизнеса",
    "Консалтинговое Агенство Готовых Бизнесов",
    "Актив плюс", "Активы для экспертов",
    "Готовый прибыльный бизнес", "Анатолий Солопов"
]
BROKERS_FILE = "brokers.json"
OUTPUT_CSV = "results.csv"
MAX_PAGES = 50  # Лимит страниц пагинации
COOKIES_FILE = "cookies.json"
ENABLE_UPDATE_ONLY = False  # True — выводить только новые объявления (по базе)
FRESH_DAYS = 2  # Сколько дней считаем "свежими" (только при ENABLE_UPDATE_ONLY=True)
ADS_DB_FILE = "seen_ads_db.json"  # База уже встреченных объявлений (по ad_number)
# Прокси (HTTP/SOCKS5). Оставить None для прямого подключения.
# Примеры: "http://user:pass@proxy.example.com:8080", "socks5://user:pass@proxy.example.com:1080"
PROXY_URL = None
# Опциональный API-ключ для автоматического решения CAPTCHA (2Captcha / AntiCaptcha)
# Оставить None для ручного режима (скрипт поставит паузу и ждёт)
CAPTCHA_API_KEY = None
CAPTCHA_SERVICE = "2captcha"  # "2captcha" или "anticaptcha"
HEADLESS = True  # True — без окна браузера, False — с окном (для ручного прохождения CAPTCHA)
# ===================== END CONFIG =====================

# ===================== USER-AGENT POOL =====================
# Пул реалистичных User-Agent строк для Chrome/Edge на Windows/Mac 2025-2026
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 OPR/116.0.0.0",
    "Mozilla/5.0 (Windows NT 11.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
]
# ===================== END USER-AGENT POOL =====================

# ===================== SELECTORS =====================
# Версионированные CSS-селекторы для парсинга Avito.
# При изменении вёрстки — обновить только здесь.
SELECTORS = {
    # Страница поиска — карточки
    "search_cards": [
        '[data-marker="item"]',
        'div[itemtype="http://schema.org/Product"]',
        '.iva-item-root',
    ],
    # Страница поиска — кнопка "в избранное" (иконка сердца)
    "card_favorite": [
        '[data-marker="favorite"]',
        '[data-marker*="favorite"]',
        'button[title*="Избран"]',
        'button[aria-label*="Избран"]',
    ],
    # Карточка на странице поиска — заголовок
    "card_title": [
        '[data-marker="item-title"]',
        'h3[itemprop="name"]',
        'h3 a',
        'h3',
    ],
    # Карточка на странице поиска — URL
    "card_url": [
        'a[itemprop="url"]',
        'a[data-marker="item-title"]',
        'h3 a',
    ],
    # Карточка на странице поиска — цена (meta)
    "card_price_meta": [
        'meta[itemprop="price"]',
    ],
    # Карточка на странице поиска — цена (элемент)
    "card_price": [
        '[data-marker="item-price"]',
        '[itemprop="price"]',
        'span[data-marker="item-price"]',
    ],
    # Карточка на странице поиска — автор/продавец
    "card_seller_name": [
        '[data-marker="seller-info/name"]',
        '[data-marker="item-contact-bar/info"]',
        '[data-marker="item-seller-info"] [data-marker="seller-info/name"]',
        'a[href*="/user/"]',
        'a[href*="/profile/"]',
    ],
    # Пагинация — следующая страница
    "pagination_next": [
        '[data-marker="pagination-button/nextPage"]',
        'a[class*="pagination-page"][rel="next"]',
        'a.pagination-page_next',
    ],
    # Страница карточки бизнеса — заголовок
    "business_title": [
        'h1[data-marker="item-view/title-info"]',
        'h1[itemprop="name"]',
        'span[data-marker="item-view/title-info"]',
        'h1',
    ],
    # Страница карточки — цена
    "business_price": [
        'span[data-marker="item-view/item-price"]',
        'span[itemprop="price"]',
        '[data-marker="item-view/item-price"]',
        '[itemprop="price"]',
        'span[class*="price-value"]',
    ],
    # Страница карточки — дата
    "business_date": [
        'span[data-marker="item-view/item-date"]',
        'span[data-marker="item-date"]',
        'span[class*="date-info"]',
    ],
    # Страница карточки — описание
    "business_description": [
        '[data-marker="item-view/item-description"]',
        '[itemprop="description"]',
        'div[class*="item-description"]',
        '.item-description',
    ],
    # Страница карточки — параметры
    "business_params": [
        '[data-marker="item-view/item-params"]',
        '[class*="params-paramsList"]',
    ],
    # Страница карточки — имя продавца
    "seller_name": [
        '[data-marker="seller-info/name"]',
        '[data-marker="seller-link/link"]',
        'a[class*="seller-info-name"]',
        'div[data-marker="seller-info/name"] a',
        '.seller-info-name a',
    ],
    # Страница карточки — блок продавца (fallback)
    "seller_block": [
        '[class*="seller-info"]',
        '[data-marker="seller-info"]',
    ],
    # Профиль продавца — объявления
    "profile_listings": [
        '[data-marker="item"]',
        '.profile-item',
        '.iva-item-root',
    ],
    # Профиль — заголовок элемента
    "profile_item_title": [
        '[data-marker="item-title"]',
        'a[itemprop="url"]',
        'h3',
    ],
}
# ===================== END SELECTORS =====================


def select_first(element, selector_key: str, attr: str = None):
    """
    Пробует CSS-селекторы из SELECTORS[selector_key] по порядку.
    Возвращает первый найденный элемент (или значение атрибута если attr задан).
    Логирует на debug-уровне какой селектор сработал.
    """
    selectors = SELECTORS.get(selector_key, [])
    for selector in selectors:
        try:
            result = element.select_one(selector)
            if result:
                logger.debug(f"Селектор {selector!r} сработал для '{selector_key}'")
                if attr:
                    return result.get(attr)
                return result
        except Exception:
            continue
    logger.debug(f"Ни один селектор не сработал для '{selector_key}'")
    return None


def select_all(element, selector_key: str) -> list:
    """Пробует CSS-селекторы из SELECTORS[selector_key], возвращает все найденные элементы."""
    selectors = SELECTORS.get(selector_key, [])
    for selector in selectors:
        try:
            results = element.select(selector)
            if results:
                logger.debug(f"Селектор {selector!r} нашёл {len(results)} элементов для '{selector_key}'")
                return results
        except Exception:
            continue
    logger.debug(f"Ни один селектор не нашёл элементы для '{selector_key}'")
    return []


def parse_llm_json(response: str, defaults: dict = None) -> dict:
    """
    Робастный парсинг JSON из LLM-ответа.
    Пробует: прямой json.loads → удаление markdown fences → regex extraction.
    """
    if defaults is None:
        defaults = {"revenue": 0, "expenses": 0, "profit": 0, "responsibility": 0}
    
    if not response:
        return defaults
    
    text = response.strip()
    
    # Попытка 1: прямой парсинг
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass
    
    # Попытка 2: убрать markdown code fences (```json ... ``` или ``` ... ```)
    cleaned = re.sub(r'^```(?:json)?\s*\n?', '', text, flags=re.MULTILINE)
    cleaned = re.sub(r'\n?```\s*$', '', cleaned, flags=re.MULTILINE)
    cleaned = cleaned.strip()
    try:
        return json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        pass
    
    # Попытка 3: regex extraction с DOTALL (для вложенных объектов)
    json_match = re.search(r'\{.*\}', text, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group())
        except (json.JSONDecodeError, ValueError):
            pass
    
    logger.warning(f"Не удалось распарсить JSON из LLM-ответа: {text[:200]}")
    return defaults


# Глобальные переменные для graceful shutdown
shutdown_requested = False
intermediate_results = []
results_lock = threading.Lock()
logger = logging.getLogger("avito_scraper")

# Счётчики для статистики обработки
stats_lock = threading.Lock()
stats = {
    "total_processed": 0,
    "brokers_skipped": 0,
    "owners_found": 0,
    "errors": 0,
}

# Глобальная переменная режима headless (устанавливается в main() из args)
headless_mode = True


class AdaptiveRateLimiter:
    """Адаптивный контроль скорости запросов."""
    
    def __init__(self, base_delay=2.0, max_delay=120.0, backoff_factor=1.5, recovery_factor=0.9):
        self.base_delay = base_delay
        self.current_delay = base_delay
        self.max_delay = max_delay
        self.backoff_factor = backoff_factor
        self.recovery_factor = recovery_factor
        self.consecutive_successes = 0
        self._lock = threading.Lock()
    
    def on_success(self):
        """Вызвать после успешного запроса — уменьшить задержку."""
        with self._lock:
            self.consecutive_successes += 1
            if self.consecutive_successes >= 3:
                self.current_delay = max(self.base_delay, self.current_delay * self.recovery_factor)
                self.consecutive_successes = 0
                logger.debug(f"Rate limiter: задержка уменьшена до {self.current_delay:.1f}с")
    
    def on_block(self):
        """Вызвать при блокировке — увеличить задержку."""
        with self._lock:
            self.consecutive_successes = 0
            self.current_delay = min(self.max_delay, self.current_delay * self.backoff_factor)
            logger.warning(f"Rate limiter: задержка увеличена до {self.current_delay:.1f}с")
    
    def wait(self):
        """Подождать текущую задержку (с гауссовым шумом)."""
        with self._lock:
            delay = self.current_delay
        jitter = random.gauss(0, delay * 0.1)
        actual = max(0.5, delay + jitter)
        time.sleep(actual)
    
    def get_delay(self) -> float:
        """Возвращает текущую задержку."""
        with self._lock:
            return self.current_delay


# Глобальный экземпляр rate limiter
rate_limiter = AdaptiveRateLimiter()


def setup_logging(debug: bool = False) -> None:
    """Настройка логирования с использованием rich."""
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(rich_tracebacks=True, markup=True)]
    )


def signal_handler(signum, frame):
    """Обработчик сигнала для graceful shutdown."""
    global shutdown_requested
    logger.warning("Получен сигнал прерывания. Сохраняю промежуточные результаты...")
    shutdown_requested = True
    with results_lock:
        if intermediate_results:
            save_to_csv(intermediate_results, "intermediate_results.csv")
            logger.info(f"Промежуточные результаты сохранены в intermediate_results.csv ({len(intermediate_results)} записей)")
    sys.exit(0)


# Регистрация обработчика сигналов
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


def call_proxyapi(messages: list, temperature: float = None) -> Optional[str]:
    """
    Вызов LLM через ProxyAPI с retry логикой.
    
    Args:
        messages: Список сообщений для API
        temperature: Температура генерации (None = использовать default API)
    
    Returns:
        Строка ответа или None при ошибке
    """
    url = f"{BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {PROXY_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": MODEL_NAME,
        "messages": messages,
    }
    # Добавить temperature только если явно задана (не None)
    if temperature is not None:
        payload["temperature"] = temperature
    
    retries = 3
    delays = [2, 4, 8]  # Exponential backoff
    
    # Логирование payload на debug уровне для диагностики
    logger.debug(f"LLM payload: model={payload.get('model')}, messages_count={len(payload.get('messages', []))}")
    
    for attempt in range(retries):
        try:
            logger.debug(f"LLM API вызов (попытка {attempt + 1}/{retries})")
            with httpx.Client(timeout=60.0) as client:
                response = client.post(url, headers=headers, json=payload)
                
                if response.status_code == 429:
                    # Rate limit — retry с backoff
                    wait_time = delays[attempt] if attempt < len(delays) else delays[-1]
                    logger.warning(f"Rate limit (429). Ожидание {wait_time}с...")
                    time.sleep(wait_time)
                    continue
                elif response.status_code >= 500:
                    # Серверная ошибка — retry
                    try:
                        error_body = response.text[:500]
                    except Exception:
                        error_body = "нет тела ответа"
                    logger.warning(f"Серверная ошибка ({response.status_code}): {error_body}")
                    if attempt < retries - 1:
                        time.sleep(delays[attempt] if attempt < len(delays) else delays[-1])
                    continue
                elif response.status_code >= 400:
                    # Клиентская ошибка (400, 401, 403...) — НЕ retry, сразу выход
                    try:
                        error_body = response.text[:500]
                    except Exception:
                        error_body = "нет тела ответа"
                    logger.error(f"Ошибка клиента {response.status_code}: {error_body}")
                    return None  # Сразу выход, не retry
                
                # Успешный ответ (2xx)
                data = response.json()
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                logger.debug(f"LLM ответ получен: {content[:100]}...")
                return content
                
        except httpx.TimeoutException:
            logger.warning(f"Timeout при вызове API (попытка {attempt + 1})")
            if attempt < retries - 1:
                time.sleep(delays[attempt])
        except Exception as e:
            logger.error(f"Ошибка при вызове API: {e}")
            if attempt < retries - 1:
                time.sleep(delays[attempt])
    
    logger.error("Все попытки вызова API исчерпаны")
    return None


def load_brokers() -> set:
    """
    Загрузка списка брокеров из файла и CONFIG.
    
    Returns:
        Set нормализованных имён брокеров (lowercase, stripped)
    """
    brokers = set()
    
    # Добавляем из CONFIG
    for name in BROKERS_LIST:
        brokers.add(name.strip().lower())
    
    # Загружаем из файла если существует
    try:
        with open(BROKERS_FILE, "r", encoding="utf-8") as f:
            file_brokers = json.load(f)
            for name in file_brokers:
                brokers.add(name.strip().lower())
        logger.info(f"Загружено {len(brokers)} брокеров из файла и CONFIG")
    except FileNotFoundError:
        logger.info(f"Файл {BROKERS_FILE} не найден, используем только CONFIG список ({len(brokers)} брокеров)")
    except json.JSONDecodeError as e:
        logger.warning(f"Ошибка чтения {BROKERS_FILE}: {e}")
    
    return brokers


def save_brokers(brokers_set: set) -> None:
    """
    Сохранение списка брокеров в JSON-файл.
    
    Args:
        brokers_set: Set имён брокеров для сохранения
    """
    try:
        sorted_brokers = sorted(list(brokers_set))
        with open(BROKERS_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted_brokers, f, ensure_ascii=False, indent=2)
        logger.debug(f"Список брокеров сохранён ({len(sorted_brokers)} записей)")
    except Exception as e:
        logger.error(f"Ошибка сохранения брокеров: {e}")


def validate_url(url: str) -> bool:
    """Проверяет, что URL безопасен для навигации (http/https, домен avito.ru или m.avito.ru)."""
    if not url:
        return False
    try:
        parsed = urlparse(url)
        netloc = parsed.netloc or ''
        return parsed.scheme in ('http', 'https') and ('avito.ru' in netloc or 'm.avito.ru' in netloc)
    except Exception:
        return False


def get_mobile_url(url: str) -> str:
    """Конвертирует URL из десктопной версии в мобильную (m.avito.ru)."""
    return url.replace("://www.avito.ru", "://m.avito.ru").replace("://avito.ru", "://m.avito.ru")


def detect_block(page) -> str:
    """
    Определяет, заблокирована ли страница (CAPTCHA, бан, rate-limit).
    Возвращает: "ok", "captcha", "blocked", "rate_limit", "closed", "unknown"
    """
    try:
        # Проверка доступности page
        current_url = page.url
        
        # 1. Проверяем ВИДИМЫЕ элементы CAPTCHA (не текст в JS)
        captcha_selectors = [
            'iframe[src*="captcha"]',
            'iframe[src*="recaptcha"]', 
            'iframe[src*="hcaptcha"]',
            '.g-recaptcha',
            '#recaptcha',
            '[data-sitekey]',
            '.h-captcha',
            'div[class*="captcha" i]',  # class содержит "captcha" (case-insensitive через CSS)
            'div[id*="captcha" i]',
            'div[class*="challenge"]',
        ]
        
        for selector in captcha_selectors:
            try:
                elem = page.query_selector(selector)
                if elem and elem.is_visible():
                    logger.debug(f"CAPTCHA обнаружена по селектору: {selector}")
                    return "captcha"
            except Exception:
                continue
        
        # 2. Проверяем заголовок страницы на признаки блокировки
        title = page.title().lower()
        if any(word in title for word in ["captcha", "заблокирован", "access denied", "forbidden", "ошибка"]):
            logger.debug(f"Блокировка определена по title: {title}")
            return "blocked"
        
        # 3. Получаем ВИДИМЫЙ текст страницы (не весь HTML с JS!)
        try:
            visible_text = page.evaluate("""
                () => {
                    const body = document.body;
                    if (!body) return '';
                    // Берём только видимый текст, без script/style
                    const clone = body.cloneNode(true);
                    const scripts = clone.querySelectorAll('script, style, noscript');
                    scripts.forEach(s => s.remove());
                    return (clone.innerText || clone.textContent || '').substring(0, 5000).toLowerCase();
                }
            """)
        except Exception:
            visible_text = ""
        
        # 4. Проверяем видимый текст на CAPTCHA (только явные фразы)
        captcha_phrases = [
            "подтвердите, что вы не робот",
            "i'm not a robot",
            "are you a robot",
            "пройдите проверку",
            "докажите, что вы не робот",
            "введите символы",
            "введите текст с картинки",
        ]
        for phrase in captcha_phrases:
            if phrase in visible_text:
                logger.debug(f"CAPTCHA в тексте: '{phrase}'")
                return "captcha"
        
        # 5. Проверяем блокировку (только явные фразы)
        block_phrases = [
            "доступ ограничен",
            "доступ к ресурсу ограничен",
            "access denied",
            "403 forbidden",
            "вы были заблокированы",
            "ip заблокирован",
            "доступ запрещён",
        ]
        for phrase in block_phrases:
            if phrase in visible_text:
                logger.debug(f"Блокировка: '{phrase}'")
                return "blocked"
        
        # 6. Проверяем rate-limit
        rate_phrases = [
            "слишком много запросов",
            "too many requests",
            "повторите попытку позже",
            "превышен лимит запросов",
        ]
        for phrase in rate_phrases:
            if phrase in visible_text:
                logger.debug(f"Rate limit: '{phrase}'")
                return "rate_limit"
        
        # 7. Проверяем что страница содержит хоть какой-то контент Avito
        # (не совсем пустая и не error page)
        if len(visible_text.strip()) < 100:
            # Страница почти пустая — возможно ещё загружается
            # Подождём немного и проверим ещё раз
            time.sleep(2)
            try:
                visible_text2 = page.evaluate("""
                    () => {
                        const body = document.body;
                        if (!body) return '';
                        const clone = body.cloneNode(true);
                        clone.querySelectorAll('script, style, noscript').forEach(s => s.remove());
                        return (clone.innerText || clone.textContent || '').substring(0, 5000).toLowerCase();
                    }
                """)
                if len(visible_text2.strip()) < 100:
                    logger.debug(f"Страница почти пустая ({len(visible_text2)} символов)")
                    return "blocked"
            except Exception:
                return "blocked"
        
        return "ok"
        
    except Exception as e:
        error_msg = str(e).lower()
        if "closed" in error_msg or "target" in error_msg:
            logger.warning(f"Страница/контекст закрыты: {e}")
            return "closed"
        logger.warning(f"Ошибка при проверке блокировки: {e}")
        return "unknown"


def handle_captcha(page, captcha_api_key=None, captcha_service="2captcha") -> bool:
    """
    Обрабатывает CAPTCHA.
    Если есть API ключ — пытается решить автоматически через 2Captcha/AntiCaptcha.
    Если нет — ставит паузу и ждёт ручного решения (headful mode).
    
    Возвращает True если CAPTCHA решена, False если нет.
    """
    if captcha_api_key:
        logger.info("Попытка автоматического решения CAPTCHA...")
        try:
            # Получить sitekey из страницы
            sitekey = None
            # reCAPTCHA
            recaptcha_elem = page.query_selector('[data-sitekey]')
            if recaptcha_elem:
                sitekey = recaptcha_elem.get_attribute('data-sitekey')
            
            if sitekey and captcha_service == "2captcha":
                # Вызов 2Captcha API
                # Отправить задачу
                resp = httpx.post("http://2captcha.com/in.php", params={
                    "key": captcha_api_key,
                    "method": "userrecaptcha",
                    "googlekey": sitekey,
                    "pageurl": page.url,
                    "json": 1
                }, timeout=30)
                data = resp.json()
                if data.get("status") == 1:
                    task_id = data["request"]
                    # Ждать результат (до 120 сек)
                    for _ in range(24):
                        time.sleep(5)
                        result = httpx.get("http://2captcha.com/res.php", params={
                            "key": captcha_api_key,
                            "action": "get",
                            "id": task_id,
                            "json": 1
                        }, timeout=30).json()
                        if result.get("status") == 1:
                            token = result["request"]
                            # Вставить токен в форму
                            page.evaluate(f'document.getElementById("g-recaptcha-response").innerHTML="{token}"')
                            page.evaluate("document.querySelector('form').submit()")
                            time.sleep(3)
                            return detect_block(page) == "ok"
                        elif result.get("request") != "CAPCHA_NOT_READY":
                            break
            
            logger.warning("Не удалось автоматически решить CAPTCHA")
            return False
        except Exception as e:
            logger.error(f"Ошибка при решении CAPTCHA: {e}")
            return False
    else:
        # Ручной режим: поведение зависит от headless_mode
        if not headless_mode:
            # Headful режим — браузер с окном, пользователь может решить CAPTCHA
            logger.warning("=" * 60)
            logger.warning("⚠️  ОБНАРУЖЕНА CAPTCHA! Решите её в окне браузера.")
            logger.warning("   Ожидание до 120 секунд...")
            logger.warning("=" * 60)
            for _ in range(24):  # 24 * 5 = 120 сек
                time.sleep(5)
                # Проверяем что page всё ещё доступна перед вызовом detect_block
                try:
                    current_url = page.url
                    logger.debug(f"Проверка CAPTCHA... URL: {current_url}")
                except Exception:
                    logger.warning("Page стала недоступна во время ожидания CAPTCHA")
                    return False
                
                status = detect_block(page)
                if status == "ok":
                    logger.info("✅ CAPTCHA решена вручную!")
                    return True
                elif status == "closed":
                    logger.warning("Страница была закрыта/перенаправлена после решения CAPTCHA")
                    return False
                elif status == "unknown":
                    # Возможно страница ещё грузится, подождём
                    logger.debug("Статус unknown, продолжаем ожидание...")
                    continue
                # "captcha" — ещё не решена, продолжаем ждать
            logger.warning("Время ожидания истекло")
            return False
        else:
            # Headless режим — нет окна, предупреждаем о необходимости --headful
            logger.warning("=" * 60)
            logger.warning("⚠️  ОБНАРУЖЕНА CAPTCHA! Браузер работает в headless режиме.")
            logger.warning("   Для ручного решения CAPTCHA используйте --headful")
            logger.warning("   (для автоматического решения укажите --captcha-key)")
            logger.warning("   Ожидание 60 секунд...")
            logger.warning("=" * 60)
            time.sleep(60)
            status = detect_block(page)
            return status == "ok"


def safe_goto(page, url: str, retries: int = 3) -> bool:
    """
    Безопасный переход на страницу с retry логикой, детекцией блокировок,
    адаптивным rate-limiting и fallback на мобильную версию.
    
    Args:
        page: Playwright page object
        url: URL для перехода
        retries: Количество попыток
    
    Returns:
        True если переход успешен, False иначе
    """
    if not validate_url(url):
        logger.warning(f"Невалидный URL для навигации: {url}")
        return False
    
    # Читаем настройки CAPTCHA из глобального CONFIG
    captcha_api_key = CAPTCHA_API_KEY
    captcha_service = CAPTCHA_SERVICE
    
    success = False
    
    for attempt in range(retries):
        try:
            # Адаптивная задержка перед запросом
            rate_limiter.wait()
            
            logger.debug(f"Переход на {url} (попытка {attempt + 1}/{retries})")
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            
            # Проверка статуса страницы
            status = detect_block(page)
            
            if status == "captcha":
                logger.warning(f"CAPTCHA обнаружена на {url}")
                rate_limiter.on_block()
                solved = handle_captcha(page, captcha_api_key, captcha_service)
                if not solved:
                    # Проверить, жива ли page после неудачной CAPTCHA
                    try:
                        page.url  # Простая проверка доступности
                    except Exception:
                        logger.error("Page закрыта после CAPTCHA, невозможно продолжить")
                        return False
                    # Page жива но CAPTCHA не решена — retry
                    logger.error("CAPTCHA не решена, повторяем...")
                    continue
                rate_limiter.on_success()
            elif status == "closed":
                logger.error(f"Страница/контекст закрыты при загрузке: {url}")
                return False  # Не retry, сразу выход
            elif status == "unknown":
                logger.warning(f"Неизвестный статус страницы: {url}, повторяем...")
                rate_limiter.on_block()
                human_delay(2, 4)
                continue  # retry
            elif status == "blocked":
                logger.error(f"Страница заблокирована: {url}")
                rate_limiter.on_block()
                human_delay(2, 4)
                continue  # retry
            elif status == "rate_limit":
                logger.warning(f"Rate limit на {url}, ожидание...")
                rate_limiter.on_block()
                time.sleep(random.uniform(30, 60))
                continue  # retry
            
            # Страница загружена успешно
            rate_limiter.on_success()
            
            # Human-like delay после загрузки (гауссово распределение)
            human_delay(2, 5)
            
            # Реалистичный скролл страницы
            human_scroll(page)
            
            # Движения мыши для имитации человека
            human_mouse_move(page)
            
            success = True
            return True
            
        except PlaywrightTimeout:
            logger.warning(f"Timeout при загрузке {url} (попытка {attempt + 1})")
            rate_limiter.on_block()
            if attempt < retries - 1:
                human_delay(2, 4)
        except Exception as e:
            error_msg = str(e).lower()
            # Обработка TargetClosedError — page/context закрыты
            if "closed" in error_msg or "target" in error_msg:
                logger.error(f"Page/context закрыты, невозможно навигировать: {url}")
                return False  # Не retry, сразу выход
            logger.error(f"Ошибка при переходе на {url}: {e}")
            rate_limiter.on_block()
            if attempt < retries - 1:
                human_delay(2, 4)
    
    # Fallback на мобильную версию после исчерпания попыток
    if not success and "avito.ru" in url and "m.avito.ru" not in url:
        mobile_url = get_mobile_url(url)
        logger.info(f"Пробуем мобильную версию: {mobile_url}")
        try:
            rate_limiter.wait()
            page.goto(mobile_url, wait_until="domcontentloaded", timeout=30000)
            status = detect_block(page)
            if status == "ok":
                rate_limiter.on_success()
                human_delay(2, 4)
                human_scroll(page)
                return True
            else:
                rate_limiter.on_block()
        except Exception as e:
            error_msg = str(e).lower()
            if "closed" in error_msg or "target" in error_msg:
                logger.error(f"Page/context закрыты при загрузке мобильной версии: {mobile_url}")
                return False
            logger.debug(f"Мобильная версия тоже не загрузилась: {e}")
    
    return False


def parse_price(price_text: str) -> int:
    """
    Парсинг цены из текста в число.
    
    Args:
        price_text: Строка с ценой (например "1 500 000 ₽")
    
    Returns:
        Цена как int или 0 при ошибке
    """
    if not price_text:
        return 0
    try:
        # Удаляем всё кроме цифр
        clean = re.sub(r'[^\d]', '', price_text)
        return int(clean) if clean else 0
    except (ValueError, TypeError):
        return 0


def extract_item_url_from_card(card) -> str:
    """Робастно извлекает URL карточки из контейнера выдачи."""
    # 1) Основные селекторы
    url_elem = select_first(card, "card_url")
    if url_elem and url_elem.get("href"):
        href = url_elem.get("href")
        if href.startswith("/"):
            return f"https://www.avito.ru{href}"
        return href

    # 2) Любая ссылка на карточку Avito внутри контейнера
    for a in card.select("a[href]"):
        href = (a.get("href") or "").strip()
        if not href:
            continue
        if href.startswith("/"):
            candidate = f"https://www.avito.ru{href}"
        else:
            candidate = href
        if validate_url(candidate):
            return candidate

    # 3) Fallback по html контейнера (если href в data-атрибутах)
    html_fragment = str(card)
    match = re.search(r'href=["\'](/[^"\']+)["\']', html_fragment)
    if match:
        candidate = f"https://www.avito.ru{match.group(1)}"
        if validate_url(candidate):
            return candidate

    return ""


def extract_title_from_card(card, item_url: str = "") -> str:
    """Робастно извлекает title карточки, даже если блок частично не прогрузился."""
    title_elem = select_first(card, "card_title")
    if title_elem:
        text = title_elem.get_text(" ", strip=True)
        if text:
            return text

    # fallback 1: текст любой ссылки
    for a in card.select("a[href]"):
        text = a.get_text(" ", strip=True)
        if text and len(text) > 3:
            return text
        aria = (a.get("aria-label") or "").strip()
        if aria:
            return aria

    # fallback 2: alt у изображения
    img = card.select_one("img[alt]")
    if img:
        alt = (img.get("alt") or "").strip()
        if alt:
            return alt

    # fallback 3: из URL
    if item_url:
        slug = item_url.rstrip("/").split("/")[-1]
        slug = re.sub(r"-\d+$", "", slug).replace("-", " ").strip()
        if slug:
            return slug

    return ""


def normalize_seller_preview_name(raw_name: str) -> str:
    """
    Нормализует имя автора из карточки выдачи.
    Возвращает пустую строку, если это похоже не на имя продавца.
    """
    if not raw_name:
        return ""

    cleaned = re.sub(r"\s+", " ", raw_name).strip(" \n\r\t|•·")
    cleaned_lower = cleaned.lower()

    # Отсекаем типичные не-имена
    noise_markers = (
        "сегодня", "вчера", "назад", "просмотр", "контакт",
        "доставка", "в наличии", "подпис", "объявлен",
    )
    if any(marker in cleaned_lower for marker in noise_markers):
        return ""

    # Слишком длинные/короткие строки вероятнее не имя автора
    if len(cleaned) < 2 or len(cleaned) > 60:
        return ""

    return cleaned


def normalize_listing_title(raw_title: str) -> str:
    """Нормализует title для сравнения дублей на этапе первичного сбора."""
    if not raw_title:
        return ""
    cleaned = re.sub(r"\s+", " ", raw_title).strip().lower()
    cleaned = re.sub(r"[^\w\sа-яА-Я-]", "", cleaned, flags=re.UNICODE)
    return cleaned


def listing_signature(title: str, seller_name: str) -> str:
    """Сигнатура карточки для первичного сравнения дублей (title + seller)."""
    t = normalize_listing_title(title)
    s = (seller_name or "").strip().lower()
    if not t or not s:
        return ""
    return f"{t}||{s}"


def parse_avito_publish_date(date_text: str, now_dt: Optional[datetime] = None) -> Optional[date]:
    """
    Преобразует строку даты Avito в объект date.
    Поддерживает форматы: "сегодня", "вчера", "21 марта", "21 марта 2026".
    """
    if not date_text:
        return None

    now_dt = now_dt or datetime.now()
    text = date_text.strip().lower().replace("ё", "е")
    text = re.sub(r"\s+", " ", text)

    if "сегодня" in text:
        return now_dt.date()
    if "вчера" in text:
        return (now_dt - timedelta(days=1)).date()

    months_map = {
        "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
        "мая": 5, "июня": 6, "июля": 7, "августа": 8,
        "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
    }
    # Примеры: "21 марта в 12:00", "21 марта 2026"
    match = re.search(r'(\d{1,2})\s+([а-я]+)(?:\s+(\d{4}))?', text)
    if not match:
        return None

    day = int(match.group(1))
    month = months_map.get(match.group(2))
    year = int(match.group(3)) if match.group(3) else now_dt.year
    if not month:
        return None

    try:
        parsed = date(year, month, day)
    except ValueError:
        return None

    # Если год не указан и дата "из будущего", считаем что это прошлый год
    if not match.group(3) and parsed > now_dt.date():
        try:
            parsed = date(year - 1, month, day)
        except ValueError:
            return None
    return parsed


def is_fresh_listing(date_text: str, fresh_days: int, now_dt: Optional[datetime] = None) -> bool:
    """
    Проверяет, что объявление опубликовано не позже чем fresh_days дней назад.
    """
    publish_date = parse_avito_publish_date(date_text, now_dt=now_dt)
    if publish_date is None:
        return False
    now_dt = now_dt or datetime.now()
    age_days = (now_dt.date() - publish_date).days
    return 0 <= age_days <= fresh_days


def load_ads_db(db_file: str = ADS_DB_FILE) -> dict:
    """
    Загружает базу ранее встреченных объявлений.
    Формат: {ad_number: {"first_seen_at": "...", "last_seen_at": "...", "date_text": "...", "url": "..."}}
    """
    if not os.path.exists(db_file):
        return {}
    try:
        with open(db_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        logger.warning(f"База {db_file} повреждена (ожидался объект JSON), будет создана заново")
        return {}
    except Exception as e:
        logger.warning(f"Не удалось загрузить базу {db_file}: {e}")
        return {}


def save_ads_db(ads_db: dict, db_file: str = ADS_DB_FILE) -> None:
    """Сохраняет базу ранее встреченных объявлений в JSON."""
    try:
        with open(db_file, "w", encoding="utf-8") as f:
            json.dump(ads_db, f, ensure_ascii=False, indent=2)
        logger.info(f"База объявлений сохранена: {db_file} ({len(ads_db)} записей)")
    except Exception as e:
        logger.error(f"Ошибка сохранения базы объявлений {db_file}: {e}")


def record_ad_in_db(ads_db: dict, card_data: dict) -> None:
    """Обновляет/добавляет запись объявления в базу по номеру объявления."""
    ad_number = (card_data or {}).get("ad_number", "").strip()
    if not ad_number:
        return

    now_iso = datetime.now().isoformat(timespec="seconds")
    existing = ads_db.get(ad_number, {})
    ads_db[ad_number] = {
        "first_seen_at": existing.get("first_seen_at", now_iso),
        "last_seen_at": now_iso,
        "date_text": (card_data or {}).get("date", existing.get("date_text", "")),
        "url": (card_data or {}).get("url", existing.get("url", "")),
        "title": (card_data or {}).get("title") or existing.get("title", ""),
        "price": int((card_data or {}).get("price", existing.get("price", 0)) or 0),
        "seller_name": (card_data or {}).get("seller_name") or existing.get("seller_name", ""),
    }


def build_seen_title_seller_signatures(ads_db: dict) -> set:
    """Строит set сигнатур title+seller из базы объявлений."""
    signatures = set()
    for entry in ads_db.values():
        if not isinstance(entry, dict):
            continue
        sig = listing_signature(entry.get("title", ""), entry.get("seller_name", ""))
        if sig:
            signatures.add(sig)
    return signatures


def collect_listings(page, search_url: str, price_min: int, brokers_set: set, seen_signatures: set = None) -> list:
    """
    Сбор карточек объявлений со страниц поиска с пагинацией.
    
    Args:
        page: Playwright page object
        search_url: URL страницы поиска Avito
        price_min: Минимальная цена для фильтрации
        brokers_set: Нормализованный set брокеров для первичной фильтрации
        seen_signatures: set сигнатур title+seller для первичного отсеивания дублей
    
    Returns:
        Список словарей с данными карточек
    """
    all_listings = []
    runtime_seen_signatures = set(seen_signatures or set())
    total_raw_cards = 0
    total_favorite_cards = 0
    total_with_url = 0
    total_without_url = 0
    total_price_skipped = 0
    total_broker_skipped = 0
    total_seen_skipped = 0
    current_page = 1
    base_url = search_url
    
    # Progress bar для пагинации
    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]Сбор страниц"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TextColumn("стр. {task.completed}/{task.total}"),
        TextColumn("| карточек: {task.fields[cards]}"),
        TimeRemainingColumn(),
    ) as pagination_progress:
        page_task = pagination_progress.add_task("Сбор страниц", total=MAX_PAGES, cards=0)
        
        while current_page <= MAX_PAGES:
            if shutdown_requested:
                break
            
            # Формируем URL с пагинацией
            if current_page == 1:
                page_url = base_url
            else:
                if "?" in base_url:
                    page_url = f"{base_url}&p={current_page}"
                else:
                    page_url = f"{base_url}?p={current_page}"
            
            logger.info(f"Загрузка страницы {current_page}: {page_url}")
            
            if not safe_goto(page, page_url):
                logger.error(f"Не удалось загрузить страницу {current_page}")
                break
            
            # Парсим HTML (с защитой от закрытой страницы)
            try:
                html = page.content()
            except Exception as e:
                logger.error(f"Не удалось получить HTML страницы {current_page}: {e}")
                break
            soup = BeautifulSoup(html, "lxml")
            
            # Ищем карточки объявлений
            cards = select_all(soup, "search_cards")
            favorite_marks = select_all(soup, "card_favorite")
            
            if not cards:
                logger.info(f"Страница {current_page}: карточки не найдены, завершаем")
                break
            
            raw_on_page = max(len(cards), len(favorite_marks))
            total_raw_cards += raw_on_page
            total_favorite_cards += len(favorite_marks)
            logger.debug(
                f"Найдено карточек на странице {current_page}: по контейнерам={len(cards)}, по сердцам={len(favorite_marks)}"
            )
            
            # Имитация чтения страницы перед парсингом
            human_delay(0.5, 1.5)
            
            page_listings = []
            page_with_url = 0
            for card in cards:
                try:
                    # Извлекаем URL
                    item_url = extract_item_url_from_card(card)
                    
                    # Валидация URL
                    if item_url and not validate_url(item_url):
                        logger.debug(f"Пропуск карточки с невалидным URL: {item_url}")
                        continue

                    if not item_url:
                        total_without_url += 1
                        logger.debug("Пропуск карточки: не удалось извлечь URL из контейнера")
                        continue

                    total_with_url += 1
                    page_with_url += 1

                    # Извлекаем название (после определения URL, чтобы был fallback)
                    title = extract_title_from_card(card, item_url=item_url)
                    if not title:
                        title = "Без названия"
                    
                    # Извлекаем цену
                    price = 0
                    price_meta = select_first(card, "card_price_meta")
                    if price_meta and price_meta.get("content"):
                        price = parse_price(price_meta.get("content"))
                    else:
                        price_elem = select_first(card, "card_price")
                        if price_elem:
                            price = parse_price(price_elem.get_text())
                    
                    # Пропускаем если цена ниже минимальной
                    if price < price_min:
                        logger.debug(f"Пропуск (цена {price:,} < {price_min:,}): {title[:50]}")
                        total_price_skipped += 1
                        continue

                    # Первичная фильтрация по автору (брокер/не брокер) прямо на выдаче
                    seller_name = ""
                    seller_elem = select_first(card, "card_seller_name")
                    if seller_elem:
                        seller_name = normalize_seller_preview_name(seller_elem.get_text(" ", strip=True))
                    if seller_name and seller_name.lower() in brokers_set:
                        logger.debug(f"Пропуск (брокер на выдаче): {seller_name} | {title[:50]}")
                        total_broker_skipped += 1
                        continue

                    # Первичный отсев дублей по title+seller на основе уже накопленной БД
                    if runtime_seen_signatures:
                        sig = listing_signature(title, seller_name)
                        if sig and sig in runtime_seen_signatures:
                            logger.debug(f"Пропуск (уже в БД по title+seller): {seller_name} | {title[:50]}")
                            total_seen_skipped += 1
                            continue
                    
                    if title and item_url:
                        sig = listing_signature(title, seller_name)
                        if sig:
                            runtime_seen_signatures.add(sig)
                        page_listings.append({
                            "title": title,
                            "price": price,
                            "url": item_url,
                            "seller_name_preview": seller_name,
                        })
                        
                except Exception as e:
                    logger.debug(f"Ошибка парсинга карточки: {e}")
                    continue
            
            if not page_listings:
                logger.info(
                    f"Страница {current_page}: контейнеров={len(cards)}, URL распознано={page_with_url}, после первичной фильтрации карточек нет "
                    f"(всего после фильтрации: {total_raw_cards}/{len(all_listings)}), идём дальше по пагинации"
                )
            else:
                all_listings.extend(page_listings)
                logger.info(
                    f"Страница {current_page}: контейнеров={len(cards)}, URL распознано={page_with_url}, добавлено {len(page_listings)} карточек "
                    f"(всего после фильтрации: {total_raw_cards}/{len(all_listings)})"
                )

            # Обновляем progress bar
            pagination_progress.update(page_task, completed=current_page, cards=len(all_listings))
            
            # Небольшая пауза перед переходом на следующую страницу
            human_delay(1.0, 2.5)
            
            # Проверяем наличие следующей страницы
            next_btn = select_first(soup, "pagination_next")
            if not next_btn:
                logger.info("Следующая страница не найдена, завершаем пагинацию")
                break
            
            current_page += 1
        
        # Когда закончили — установить прогресс в 100%
        pagination_progress.update(page_task, completed=MAX_PAGES, cards=len(all_listings))
    
    logger.info(
        "Итог первичного сбора: сырых карточек=%s (по сердцам=%s), распознано URL=%s, не распознано URL=%s, "
        "после фильтрации=%s (%s/%s), пропущено по цене=%s, пропущено брокеров=%s, пропущено как дубли title+seller=%s",
        total_raw_cards,
        total_favorite_cards,
        total_with_url,
        total_without_url,
        len(all_listings),
        total_raw_cards,
        len(all_listings),
        total_price_skipped,
        total_broker_skipped,
        total_seen_skipped,
    )
    return all_listings


def classify_seller(page, seller_name: str, seller_url: str, brokers_set: set, seller_cache: dict,
                    brokers_lock: threading.Lock = None, cache_lock: threading.Lock = None) -> str:
    """
    Субагент №1: Классификация продавца (BROKER/OWNER).
    
    Args:
        page: Playwright page object
        seller_name: Имя продавца
        seller_url: URL профиля продавца
        brokers_set: Set известных брокеров
        seller_cache: Кэш результатов классификации
        brokers_lock: Lock для доступа к brokers_set
        cache_lock: Lock для доступа к seller_cache
    
    Returns:
        "BROKER" или "OWNER"
    """
    normalized_name = seller_name.strip().lower()
    
    # Проверка в списке известных брокеров (thread-safe)
    if brokers_lock:
        with brokers_lock:
            if normalized_name in brokers_set:
                logger.debug(f"Продавец '{seller_name}' найден в списке брокеров")
                return "BROKER"
    else:
        if normalized_name in brokers_set:
            logger.debug(f"Продавец '{seller_name}' найден в списке брокеров")
            return "BROKER"
    
    # Проверка в кэше (thread-safe)
    if cache_lock:
        with cache_lock:
            if seller_url in seller_cache:
                logger.debug(f"Продавец '{seller_name}' найден в кэше: {seller_cache[seller_url]}")
                return seller_cache[seller_url]
    else:
        if seller_url in seller_cache:
            logger.debug(f"Продавец '{seller_name}' найден в кэше: {seller_cache[seller_url]}")
            return seller_cache[seller_url]
    
    # Загружаем страницу профиля
    if not safe_goto(page, seller_url):
        logger.warning(f"Не удалось загрузить профиль {seller_url}, считаем владельцем")
        return "OWNER"
    
    # Имитация чтения профиля
    simulate_reading(page, 1.0, 2.5)
    
    html = page.content()
    soup = BeautifulSoup(html, "lxml")
    
    # Собираем объявления продавца
    listings_text = []
    
    # Активные объявления
    active_items = select_all(soup, "profile_listings")
    for item in active_items[:20]:  # Берём до 20 объявлений
        title_elem = select_first(item, "profile_item_title")
        if title_elem:
            listings_text.append(title_elem.get_text(strip=True))
    
    # Пробуем открыть вкладку "Завершённые"
    try:
        closed_tab = page.query_selector('button:has-text("Завершённые")') or page.query_selector('[data-marker="tabs"] button:nth-child(2)')
        if closed_tab:
            closed_tab.click()
            human_delay(1, 2)
            
            html_closed = page.content()
            soup_closed = BeautifulSoup(html_closed, "lxml")
            closed_items = select_all(soup_closed, "profile_listings")[:10]
            for item in closed_items:
                title_elem = select_first(item, "profile_item_title")
                if title_elem:
                    listings_text.append(f"[завершённое] {title_elem.get_text(strip=True)}")
    except Exception as e:
        logger.debug(f"Не удалось получить завершённые объявления: {e}")
    
    # Если мало объявлений — считаем владельцем
    if len(listings_text) < 2:
        logger.debug(f"У продавца '{seller_name}' мало объявлений, считаем владельцем")
        if cache_lock:
            with cache_lock:
                seller_cache[seller_url] = "OWNER"
        else:
            seller_cache[seller_url] = "OWNER"
        return "OWNER"
    
    # Формируем промт для LLM
    listings_combined = "\n".join(f"- {item}" for item in listings_text)
    prompt = f"""Ты — эксперт по классификации продавцов на Avito.
Имя продавца: "{seller_name}"

Вот список его объявлений (активные и завершённые):
{listings_combined}

ПРАВИЛА:
1. Если среди объявлений есть НЕСКОЛЬКО в категории "Готовый бизнес" или продажа бизнеса — это БРОКЕР.
2. Если объявления разнородные (бытовые товары, электроника, животные, хобби) и только ОДНО про бизнес (текущее) — это ВЛАДЕЛЕЦ.
3. Если имя продавца похоже на название компании/агентства — скорее БРОКЕР.

Ответь ОДНИМ словом: BROKER или OWNER"""

    messages = [{"role": "user", "content": prompt}]
    response = call_proxyapi(messages)
    
    # Парсим ответ
    result = "OWNER"  # Default
    if response:
        response_upper = response.upper()
        if "BROKER" in response_upper:
            result = "BROKER"
        elif "OWNER" in response_upper:
            result = "OWNER"
    
    # Сохраняем результат (thread-safe)
    if cache_lock:
        with cache_lock:
            seller_cache[seller_url] = result
    else:
        seller_cache[seller_url] = result
    
    # Если брокер — добавляем в список (thread-safe)
    if result == "BROKER":
        if brokers_lock:
            with brokers_lock:
                brokers_set.add(normalized_name)
                save_brokers(brokers_set)
        else:
            brokers_set.add(normalized_name)
            save_brokers(brokers_set)
        logger.info(f"Новый брокер добавлен в список: {seller_name}")
    
    return result


def analyze_business_card(page, card_url: str) -> Optional[dict]:
    """
    Субагент №2: Анализ карточки бизнеса.
    
    Args:
        page: Playwright page object
        card_url: URL карточки объявления
    
    Returns:
        Словарь с данными карточки или None при ошибке
    """
    if not safe_goto(page, card_url):
        logger.error(f"Не удалось загрузить карточку {card_url}")
        return None
    
    # Имитация чтения карточки бизнеса
    simulate_reading(page, 1.5, 3.0)
    
    html = page.content()
    soup = BeautifulSoup(html, "lxml")
    
    result = {
        "url": card_url,
        "title": "",
        "price": 0,
        "ad_number": "",
        "date": "",
        "views_total": 0,
        "views_today": 0,
        "seller_name": "",
        "seller_url": "",
        "revenue": 0,
        "expenses": 0,
        "profit": 0,
        "responsibility": 0
    }
    
    try:
        # Название бизнеса
        title_elem = select_first(soup, "business_title")
        if title_elem:
            result["title"] = title_elem.get_text(strip=True)
        
        # Цена
        price_elem = select_first(soup, "business_price")
        if price_elem:
            result["price"] = parse_price(price_elem.get_text())
        
        # Номер объявления
        page_text = soup.get_text()
        ad_match = re.search(r'№\s*(\d+)', page_text)
        if ad_match:
            result["ad_number"] = ad_match.group(1)
        
        # Дата размещения
        date_elem = select_first(soup, "business_date")
        if date_elem:
            result["date"] = date_elem.get_text(strip=True)
        
        # Просмотры
        views_match = re.search(r'(\d[\d\s]*)\s*просмотр', page_text.replace('\xa0', ' '))
        if views_match:
            result["views_total"] = int(re.sub(r'\s', '', views_match.group(1)))
        
        today_match = re.search(r'\+\s*(\d+)\s*сегодня', page_text)
        if today_match:
            result["views_today"] = int(today_match.group(1))
        
        # Имя и URL продавца
        seller_elem = select_first(soup, "seller_name")
        if seller_elem:
            result["seller_name"] = seller_elem.get_text(strip=True)
            href = seller_elem.get("href")
            if href:
                if href.startswith("/"):
                    result["seller_url"] = f"https://www.avito.ru{href}"
                else:
                    result["seller_url"] = href
        
        # Альтернативный поиск продавца
        if not result["seller_name"]:
            seller_block = select_first(soup, "seller_block")
            if seller_block:
                seller_link = seller_block.select_one('a')
                if seller_link:
                    result["seller_name"] = seller_link.get_text(strip=True)
                    href = seller_link.get("href")
                    if href and not result["seller_url"]:
                        result["seller_url"] = f"https://www.avito.ru{href}" if href.startswith("/") else href
        
        # Блок описания для анализа
        description_elem = select_first(soup, "business_description")
        description_text = description_elem.get_text(strip=True) if description_elem else ""
        
        # Ищем структурированные параметры
        params_block = select_all(soup, "business_params")
        params_text = " ".join(p.get_text(strip=True) for p in params_block)
        
        card_text = f"{result['title']}\n{description_text}\n{params_text}"
        
        # Вызываем LLM для анализа финансовых данных
        if card_text.strip():
            prompt = f"""Проанализируй описание карточки бизнеса на Avito.

Текст карточки:
{card_text[:3000]}

Извлеки следующие поля:
- revenue (Выручка в месяц, число в рублях, без пробелов)
- expenses (Расходы в месяц, число в рублях)  
- profit (Чистая прибыль в месяц, число в рублях)

ПРАВИЛА определения ответственности (responsibility):
- Если revenue, expenses ИЛИ profit равно 0, 1, пустое или не указано → responsibility = 0
- Иначе → responsibility = 1

Ответь СТРОГО в формате JSON (без markdown):
{{"revenue": 150000, "expenses": 80000, "profit": 70000, "responsibility": 1}}"""

            messages = [{"role": "user", "content": prompt}]
            response = call_proxyapi(messages)
            
            if response:
                # Робастный парсинг JSON из LLM-ответа
                llm_data = parse_llm_json(response)
                result["revenue"] = int(llm_data.get("revenue", 0) or 0)
                result["expenses"] = int(llm_data.get("expenses", 0) or 0)
                result["profit"] = int(llm_data.get("profit", 0) or 0)
                result["responsibility"] = int(llm_data.get("responsibility", 0) or 0)
        
    except Exception as e:
        logger.error(f"Ошибка анализа карточки {card_url}: {e}")
        return None
    
    return result


def print_final_statistics(results: list, stats: dict, total_listings: int) -> None:
    """Выводит подробную финальную статистику в консоль."""
    console = Console()
    console.print()
    console.print("=" * 60, style="bold")
    console.print("  ИТОГОВАЯ СТАТИСТИКА", style="bold cyan")
    console.print("=" * 60, style="bold")
    console.print(f"  Всего карточек собрано:        {total_listings}")
    console.print(f"  Обработано:                    {stats.get('total_processed', 0)}")
    console.print(f"  ├── Брокеры (пропущены):       {stats.get('brokers_skipped', 0)}", style="yellow")
    console.print(f"  ├── Владельцы (в результатах):  {stats.get('owners_found', 0)}", style="green")
    console.print(f"  └── Ошибки обработки:          {stats.get('errors', 0)}", style="red" if stats.get('errors', 0) > 0 else "dim")
    
    if results:
        prices = [r.get("price", 0) for r in results if r.get("price", 0) > 0]
        responsible = sum(1 for r in results if r.get("responsibility", 0) == 1)
        
        if prices:
            console.print(f"\n  Диапазон цен:   {min(prices):,} ₽ — {max(prices):,} ₽")
            console.print(f"  Средняя цена:   {sum(prices) // len(prices):,} ₽")
        console.print(f"  С финансовыми данными: {responsible}/{len(results)}")
    
    console.print("=" * 60, style="bold")
    console.print()


def display_results_table(results: list) -> None:
    """
    Отображение результатов в виде таблицы с использованием rich.
    
    Args:
        results: Список словарей с данными карточек
    """
    if not results:
        logger.info("Нет результатов для отображения")
        return
    
    console = Console()
    table = Table(title="Готовый бизнес — Краснодар (только владельцы)", show_lines=True)
    table.add_column("Название", style="bold cyan", max_width=30)
    table.add_column("Цена", style="green", justify="right")
    table.add_column("Ссылка", style="blue", max_width=50)
    table.add_column("Дата", style="yellow")
    table.add_column("Просм.", justify="right")
    table.add_column("Сегодня", justify="right")
    table.add_column("Отв.", justify="center")  # responsibility flag
    
    for r in results:
        resp_icon = "✅" if r.get("responsibility", 0) == 1 else "⚠️"
        table.add_row(
            r.get("title", "—")[:30],
            f"{r.get('price', 0):,} ₽",
            r.get("url", "—")[:50],
            r.get("date", "—"),
            str(r.get("views_total", "—")),
            str(r.get("views_today", "—")),
            resp_icon
        )
    
    console.print(table)


def save_to_csv(results: list, filename: str) -> None:
    """
    Сохранение результатов в CSV-файл.
    
    Args:
        results: Список словарей с данными карточек
        filename: Имя выходного файла
    """
    if not results:
        logger.info("Нет данных для сохранения в CSV")
        return
    
    fieldnames = [
        "title", "price", "url", "ad_number", "date",
        "views_total", "views_today", "responsibility",
        "revenue", "expenses", "profit"
    ]
    
    try:
        with open(filename, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(results)
        logger.info(f"Результаты сохранены в {filename}")
    except Exception as e:
        logger.error(f"Ошибка сохранения CSV: {e}")


def parse_args():
    """
    Парсинг аргументов командной строки.
    
    Returns:
        Namespace с аргументами
    """
    parser = argparse.ArgumentParser(
        description="Avito Business Scraper — скрейпер объявлений 'Готовый бизнес'",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument(
        "--price-min",
        type=int,
        default=None,
        help=f"Минимальная цена (default: {PRICE_MIN:,} ₽)"
    )
    parser.add_argument(
        "--search-url",
        type=str,
        default=None,
        help="URL поиска Avito"
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help=f"Файл результатов CSV (default: {OUTPUT_CSV})"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Включить debug-логирование"
    )
    parser.add_argument(
        "--proxy",
        type=str,
        default=None,
        help="URL прокси (HTTP/SOCKS5). Пример: http://user:pass@proxy.example.com:8080"
    )
    parser.add_argument(
        "--captcha-key",
        type=str,
        default=None,
        help="API-ключ для автоматического решения CAPTCHA (2Captcha/AntiCaptcha)"
    )
    parser.add_argument(
        "--headful",
        action="store_true",
        help="Запуск браузера с окном (для ручного прохождения CAPTCHA)"
    )
    return parser.parse_args()


# Многопоточность убрана — Playwright sync API работает только в главном потоке


# ===================== ANTI-DETECT FUNCTIONS =====================
def random_viewport():
    """Генерирует случайный реалистичный viewport с небольшим jitter."""
    base_widths = [1920, 1680, 1536, 1440, 1366]
    base_heights = [1080, 1050, 864, 900, 768]
    idx = random.randint(0, len(base_widths) - 1)
    return {
        "width": base_widths[idx] + random.randint(-10, 10),
        "height": base_heights[idx] + random.randint(-10, 10),
    }


def human_delay(min_sec=1.0, max_sec=3.0):
    """Задержка с гауссовым распределением (более реалистично чем uniform)."""
    mean = (min_sec + max_sec) / 2
    std = (max_sec - min_sec) / 4
    delay = max(min_sec, min(max_sec, random.gauss(mean, std)))
    time.sleep(delay)


def human_mouse_move(page):
    """Имитация случайных движений мыши по странице."""
    try:
        viewport = page.viewport_size
        if not viewport:
            return
        # 2-4 случайных движения
        for _ in range(random.randint(2, 4)):
            x = random.randint(100, viewport["width"] - 100)
            y = random.randint(100, viewport["height"] - 100)
            page.mouse.move(x, y, steps=random.randint(5, 15))
            time.sleep(random.uniform(0.1, 0.3))
    except Exception:
        pass


def human_scroll(page):
    """Реалистичный скролл страницы (несколько шагов, разные скорости)."""
    try:
        # Скроллим в 2-4 этапа
        for _ in range(random.randint(2, 4)):
            scroll_amount = random.randint(200, 600)
            page.evaluate(f"window.scrollBy(0, {scroll_amount})")
            time.sleep(random.uniform(0.3, 0.8))
        
        # Иногда скроллим немного вверх (как человек)
        if random.random() < 0.3:
            page.evaluate(f"window.scrollBy(0, -{random.randint(50, 150)})")
            time.sleep(random.uniform(0.2, 0.5))
    except Exception:
        pass


def simulate_reading(page, min_sec=1.5, max_sec=4.0):
    """Имитация чтения страницы: задержка + мышь + скролл."""
    human_delay(min_sec, max_sec)
    human_mouse_move(page)
    human_scroll(page)


def save_cookies(context, filename=None):
    """Сохраняет cookies browser context в JSON файл."""
    filename = filename or COOKIES_FILE
    try:
        cookies = context.cookies()
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(cookies, f, ensure_ascii=False, indent=2)
        logger.debug(f"Cookies сохранены в {filename} ({len(cookies)} шт.)")
    except Exception as e:
        logger.warning(f"Не удалось сохранить cookies: {e}")


def load_cookies(context, filename=None):
    """Загружает cookies из JSON файла в browser context."""
    filename = filename or COOKIES_FILE
    try:
        if os.path.exists(filename):
            with open(filename, "r", encoding="utf-8") as f:
                cookies = json.load(f)
            context.add_cookies(cookies)
            logger.info(f"Загружено {len(cookies)} cookies из {filename}")
            return True
    except Exception as e:
        logger.warning(f"Не удалось загрузить cookies: {e}")
    return False


def rotate_session(browser, old_context, proxy_url=None):
    """
    Пересоздаёт browser context при бане (новый UA + viewport).
    
    Args:
        browser: Playwright browser object
        old_context: Старый browser context для закрытия
        proxy_url: URL прокси (не используется — proxy задан на уровне browser)
    
    Returns:
        (new_context, new_page)
    """
    try:
        old_context.close()
    except Exception:
        pass
    
    new_context = browser.new_context(
        viewport=random_viewport(),
        locale="ru-RU",
        timezone_id="Europe/Moscow",
        user_agent=random.choice(USER_AGENTS),
    )
    new_page = new_context.new_page()
    _stealth_instance.apply_stealth_sync(new_page)
    logger.info("Session rotated: новый UA + viewport")
    return new_context, new_page
# ===================== END ANTI-DETECT FUNCTIONS =====================


def main():
    """
    Главная функция — запуск пайплайна скрейпинга.
    """
    global intermediate_results, CAPTCHA_API_KEY, headless_mode
    
    args = parse_args()
    setup_logging(args.debug)
    
    price_min = args.price_min or PRICE_MIN
    search_url = args.search_url or SEARCH_URL
    output_file = args.output or OUTPUT_CSV
    proxy_url = args.proxy or PROXY_URL
    
    # Устанавливаем режим headless/headful из CLI аргумента
    headless_mode = not args.headful  # --headful переключает в режим с окном
    
    # Устанавливаем CAPTCHA API ключ из CLI аргумента
    if args.captcha_key:
        CAPTCHA_API_KEY = args.captcha_key
    
    logger.info("=" * 60)
    logger.info("Avito Business Scraper")
    logger.info("=" * 60)
    logger.info(f"URL поиска: {search_url}")
    logger.info(f"Минимальная цена: {price_min:,} ₽")
    logger.info(f"Файл результатов: {output_file}")
    logger.info(f"Режим только обновления: {ENABLE_UPDATE_ONLY}")
    if ENABLE_UPDATE_ONLY:
        logger.info(f"Свежесть объявлений (дней): {FRESH_DAYS}")
    if proxy_url:
        logger.info(f"Прокси: {proxy_url}")
    if CAPTCHA_API_KEY:
        logger.info(f"CAPTCHA API: настроен ({CAPTCHA_SERVICE})")
    logger.info("=" * 60)
    
    brokers_set = load_brokers()
    ads_db = load_ads_db(ADS_DB_FILE)
    seen_title_seller_signatures = build_seen_title_seller_signatures(ads_db)
    seller_cache = {}
    results = []
    
    # Locks для thread-safe доступа (оставлены для signal_handler)
    brokers_lock = threading.Lock()
    cache_lock = threading.Lock()
    
    try:
        with sync_playwright() as pw:
            logger.info("Запуск браузера...")
            # Настройка launch args с поддержкой прокси и headless/headful режима
            launch_args = {"headless": headless_mode}
            if proxy_url:
                launch_args["proxy"] = {"server": proxy_url}
            browser = pw.chromium.launch(**launch_args)
            try:
                # Создаём первый context и page для collect_listings
                context = browser.new_context(
                    viewport=random_viewport(),
                    locale="ru-RU",
                    timezone_id="Europe/Moscow",
                    user_agent=random.choice(USER_AGENTS)
                )
                page = context.new_page()
                _stealth_instance.apply_stealth_sync(page)
                
                # Загружаем сохранённые cookies
                load_cookies(context)
                
                # Шаг 1: Сбор карточек
                logger.info("Сбор карточек со страницы поиска...")
                listings = collect_listings(page, search_url, price_min, brokers_set, seen_title_seller_signatures)
                logger.info(f"Найдено {len(listings)} карточек с ценой >= {price_min:,} ₽")
                
                if not listings:
                    # Проверяем, жива ли page (могла закрыться после CAPTCHA)
                    try:
                        page.url
                    except Exception:
                        logger.warning("Page закрыта после collect_listings, пересоздаём...")
                        try:
                            page = context.new_page()
                            _stealth_instance.apply_stealth_sync(page)
                            # Повторная попытка сбора карточек
                            logger.info("Повторный сбор карточек после пересоздания page...")
                            listings = collect_listings(page, search_url, price_min, brokers_set, seen_title_seller_signatures)
                            logger.info(f"Найдено {len(listings)} карточек с ценой >= {price_min:,} ₽")
                        except Exception as e:
                            logger.error(f"Не удалось пересоздать page: {e}")
                    
                    if not listings:
                        logger.warning("Карточки не найдены. Проверьте URL и настройки.")
                        return
                
                # Шаг 2-3: Обработка карточек последовательно (в главном потоке)
                logger.info(f"Обработка {len(listings)} карточек...")
                
                with Progress(
                    SpinnerColumn(),
                    TextColumn("[bold blue]{task.description}"),
                    BarColumn(),
                    TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
                    TextColumn("({task.completed}/{task.total})"),
                    TimeRemainingColumn(),
                ) as progress:
                    task_id = progress.add_task("Обработка карточек", total=len(listings))
                    
                    for listing in listings:
                        if shutdown_requested:
                            break
                        
                        try:
                            # Анализ карточки бизнеса
                            card_data = analyze_business_card(page, listing["url"])
                            
                            if card_data is None:
                                # Попробовать rotate session при бане
                                try:
                                    context, page = rotate_session(browser, context, proxy_url)
                                    _stealth_instance.apply_stealth_sync(page)
                                except Exception:
                                    pass
                                with stats_lock:
                                    stats["errors"] += 1
                                    stats["total_processed"] += 1
                                progress.advance(task_id)
                                continue
                            
                            # Получить имя и URL продавца
                            seller_name = card_data.get("seller_name", "")
                            seller_url = card_data.get("seller_url", "")
                            ad_number = card_data.get("ad_number", "").strip()
                            is_known_ad = bool(ad_number and ad_number in ads_db)

                            # Режим обновлений: только новые объявления за последние FRESH_DAYS дней
                            if ENABLE_UPDATE_ONLY:
                                if not ad_number:
                                    logger.info("  → Нет номера объявления, пропускаем в режиме обновлений")
                                    with stats_lock:
                                        stats["total_processed"] += 1
                                    progress.advance(task_id)
                                    continue

                                if is_known_ad:
                                    logger.info(f"  → Уже есть в базе (№ {ad_number}), пропускаем")
                                    record_ad_in_db(ads_db, card_data)
                                    with stats_lock:
                                        stats["total_processed"] += 1
                                    progress.advance(task_id)
                                    continue

                                date_text = card_data.get("date", "")
                                if not is_fresh_listing(date_text, FRESH_DAYS):
                                    logger.info(f"  → Не свежее объявление ({date_text}), пропускаем")
                                    record_ad_in_db(ads_db, card_data)
                                    with stats_lock:
                                        stats["total_processed"] += 1
                                    progress.advance(task_id)
                                    continue
                            
                            # Классификация продавца (субагент №1)
                            if seller_name and seller_url:
                                classification = classify_seller(
                                    page, seller_name, seller_url,
                                    brokers_set, seller_cache,
                                    brokers_lock, cache_lock
                                )
                                if classification == "BROKER":
                                    logger.info(f"  → Брокер: {seller_name}, пропускаем")
                                    record_ad_in_db(ads_db, card_data)
                                    with stats_lock:
                                        stats["brokers_skipped"] += 1
                                        stats["total_processed"] += 1
                                    progress.advance(task_id)
                                    continue
                            elif seller_name:
                                # Нет URL продавца — проверяем только по имени
                                normalized = seller_name.strip().lower()
                                if normalized in brokers_set:
                                    logger.info(f"  → Брокер (по имени): {seller_name}")
                                    record_ad_in_db(ads_db, card_data)
                                    with stats_lock:
                                        stats["brokers_skipped"] += 1
                                        stats["total_processed"] += 1
                                    progress.advance(task_id)
                                    continue
                            
                            # Владелец — добавляем в результаты
                            # Используем title из card_data (с карточки), а не из listing (с поиска)
                            display_title = card_data.get('title') or listing.get('title', '?')
                            logger.info(f"  → Владелец: {display_title[:50]}")
                            results.append(card_data)
                            record_ad_in_db(ads_db, card_data)
                            with results_lock:
                                intermediate_results.append(card_data)
                            with stats_lock:
                                stats["owners_found"] += 1
                                stats["total_processed"] += 1
                            
                        except Exception as e:
                            logger.error(f"Ошибка при обработке {listing.get('url', '?')}: {e}")
                            with stats_lock:
                                stats["errors"] += 1
                                stats["total_processed"] += 1
                        
                        progress.advance(task_id)
                
            finally:
                # Сохраняем cookies перед закрытием
                save_cookies(context)
                context.close()
                browser.close()
            
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")
        with results_lock:
            if intermediate_results:
                save_to_csv(intermediate_results, "emergency_results.csv")
                logger.info("Аварийное сохранение в emergency_results.csv")
        raise
    
    # Шаг 4: Вывод результатов
    logger.info("=" * 60)
    logger.info(f"Обработка завершена. Найдено {len(results)} объявлений от владельцев.")
    logger.info("=" * 60)
    
    # Вывод статистики
    print_final_statistics(results, stats, len(listings))
    
    # Вывод таблицы
    if results:
        display_results_table(results)
    else:
        logger.warning("Нет результатов для вывода. Все карточки оказались от брокеров или произошли ошибки.")
    
    # Сохранение CSV
    save_to_csv(results, output_file)
    save_ads_db(ads_db, ADS_DB_FILE)
    
    # Финальное сообщение
    console = Console()
    console.print()
    console.print("✅ РАБОТА ЗАВЕРШЕНА", style="bold green")
    console.print(f"📊 Результаты: [bold]{output_file}[/bold] ({len(results)} записей)")
    console.print(f"📋 Брокеры: [bold]{BROKERS_FILE}[/bold]")
    console.print()


if __name__ == "__main__":
    main()
