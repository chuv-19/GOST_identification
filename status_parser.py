import asyncio
import aiohttp
from bs4 import BeautifulSoup
from urllib.parse import quote_plus
import logging
import ssl
import re
from datetime import datetime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_status_cache = {}
_semaphore = asyncio.Semaphore(5)  # Ограничение параллельных запросов

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7"
}

ssl_context = ssl.create_default_context()


async def fetch(session, url):
    try:
        async with session.get(url, headers=HEADERS, timeout=10, ssl=ssl_context) as response:
            response.raise_for_status()
            return await response.text()
    except Exception as e:
        logger.error(f"Ошибка запроса к {url}: {str(e)}")
        return None


async def check_pravo_gov_ru(session, doc_type, number, date):
    # Кодируем только тип документа и номер, заменяя "№" на "N"
    query = f"{doc_type.replace('№', 'N')} {number}"
    encoded_query = quote_plus(query)
    url = f"http://publication.pravo.gov.ru/Search/?text={encoded_query}"

    try:
        async with session.get(url, headers=HEADERS, timeout=10, ssl=ssl_context) as response:
            # Проверяем статус ответа
            if response.status == 403:
                logger.warning(f"Доступ запрещен (403) для {url}")
                return "доступ запрещен"
            elif response.status != 200:
                logger.warning(f"Сервер вернул статус {response.status} для {url}")
                return "ошибка запроса"

            html = await response.text()

            # Упрощенный анализ ответа
            if "ничего не найдено" in html.lower():
                return "не найден"
            elif "утратил силу" in html.lower():
                return "утратил силу"
            elif "действующ" in html.lower():
                return "действует"
            else:
                return "статус не определен"
    except Exception as e:
        logger.error(f"Ошибка при запросе к {url}: {str(e)}")
        return "ошибка запроса"

async def check_government_ru(session, doc_type, number, date=None):
    base_url = "https://government.ru"
    search_url = f"{base_url}/docs/"

    try:
        async with session.get(search_url, headers=HEADERS, timeout=10, ssl=ssl_context) as response:
            if response.status != 200:
                return "ошибка запроса"

            html = await response.text()
            soup = BeautifulSoup(html, 'html.parser')

            results = soup.find_all("div", class_="doc-list-item")
            for item in results:
                a = item.find("a", href=True)
                if not a:
                    continue

                title = a.get_text(strip=True).lower()
                if str(number).lower() in title and doc_type.lower() in title:
                    if "утратил силу" in title or "не действует" in title:
                        return "утратил силу"
                    elif "действующ" in title or "вступает в силу" in title:
                        return "действует"
                    else:
                        return "статус не определен"

            return "не найден"
    except Exception as e:
        logger.error(f"Ошибка при запросе к government.ru: {str(e)}")
        return "ошибка запроса"

async def check_consultant(session, doc_type, number, date=None):
    base_url = "https://www.consultant.ru/search/"
    query = f"{doc_type} {number}"
    if date:
        query += f" от {date}"

    # Убираем проблемные символы перед кодированием
    safe_query = query.replace('№', '').replace('  ', ' ')
    encoded_query = quote_plus(safe_query)
    url = f"{base_url}?q={encoded_query}"

    try:
        async with session.get(url, headers=HEADERS, timeout=10, ssl=ssl_context) as response:
            if response.status != 200:
                return "ошибка запроса"

            html = await response.text()
            soup = BeautifulSoup(html, 'html.parser')

            # Ищем блоки с результатами
            results = soup.find_all('div', class_='search-result-item')
            for item in results:
                text = item.get_text().lower()
                if str(number).lower() in text:
                    if "утратил силу" in text or "не действует" in text:
                        return "утратил силу"
                    elif "действующ" in text:
                        return "действует"

            return "не найден"
    except Exception as e:
        logger.error(f"Ошибка при запросе к Consultant.ru: {str(e)}")
        return "ошибка запроса"

    soup = BeautifulSoup(html, 'html.parser')

    # Проверяем результаты поиска в Консультант+
    results = soup.find_all('div', class_='search-result-item')
    for item in results:
        text = item.get_text().lower()
        if "постановление правительства" in text and str(number) in text:
            if "утратил силу" in text or "не действует" in text:
                return "утратил силу"
            elif "действующ" in text:
                return "действует"

    return "не найден"


async def get_status_auto(session, doc_type, number, date=None):
    cache_key = (doc_type, number, date)
    if cache_key in _status_cache:
        return _status_cache[cache_key]

    async with _semaphore:
        if "Постановление Правительства" in doc_type:
            # Проверяем на pravo.gov.ru
            status = await check_pravo_gov_ru(session, doc_type, number, date)
            if status != "не найден":
                _status_cache[cache_key] = status
                return status

            # Если не нашли, проверяем в Консультант+
            status = await check_consultant(session, doc_type, number, date)
            _status_cache[cache_key] = status
            return status
        else:
            # Для других типов документов
            status = await check_pravo_gov_ru(session, doc_type, number, date)
            _status_cache[cache_key] = status
            return status


async def enrich_single_document_async(session, doc):
    doc_type = doc.get("Тип документа", "")
    number = doc.get("Номер", "")
    date = doc.get("Дата", "")

    status = await get_status_auto(session, doc_type, number, date)

    return {
        **doc,
        "Статус": status
    }


async def enrich_documents_with_status_async(documents):
    async with aiohttp.ClientSession() as session:
        tasks = [enrich_single_document_async(session, doc) for doc in documents]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Обрабатываем возможные ошибки
        valid_results = []
        for res in results:
            if isinstance(res, Exception):
                logger.error(f"Ошибка при обработке документа: {str(res)}")
            else:
                valid_results.append(res)

        return valid_results
